# Design

How studiomic is built, what was measured, and what the tests found wrong. Every
number here comes from `studiomic-bench` (see [testing.md](testing.md)) unless it
says otherwise.

## Architecture

Two user services and a WirePlumber rule file:

| Piece | What it is |
|---|---|
| `studiomic.service` | `pipewire -c <config>`: a filter-chain in its own client process. Creates the virtual source and its capture input. |
| `studiomic-select.service` | `studiomic-select run`: measures the microphones, links the best to the filter's input. |
| `53-studiomic-source-ladder.conf` | WirePlumber priorities by kind of device: what decides before anything is measured. |

The filter runs outside the PipeWire daemon, so a crash in the plugin cannot take
the audio server down, and its CPU use shows under its own name. The capture
input has `node.autoconnect = false` when the selector is on: the selector links
the microphone itself, and WirePlumber must not also link the default source.

WirePlumber remembers a microphone picked by hand, and that choice beats any
`priority.session`. So the service also sets the remembered default source
(`pw-metadata ... default.configured.audio.source`), which is what
`wpctl set-default` does. Without it, a user who once picked a microphone never
gets the virtual one.

## The chain

In signal order, with the reason for each position (also in `nix/chain.nix`):

| Node | Setting | Why here |
|---|---|---|
| `dpdf` | DPDFNet, 48 kHz | First, on the raw signal. It scored lower when a high-pass came before it (DNSMOS 3.18 against 3.23 on keystrokes, 3.02 against 3.19 on chatter), and everything after it would lift the noise that is left. |
| `hpf` | 80 Hz high-pass | Desk thumps and mains hum are below the voice. |
| `body`, `mud`, `soft` | +3 dB low shelf 180 Hz, -1.5 dB at 350 Hz, -3 dB high shelf 6 kHz | Chosen by a blind test (below). |
| `gain` | LSP auto gain, -23 LUFS, at most +24 dB | Before the compressor, because the compressor's threshold means something only once the level is known. Pauses below -55 LUFS count as silence and are left alone. |
| `deess` | LSP de-esser on 4.5 to 10 kHz | After the leveler: before it, the signal is too quiet to reach the threshold. |
| `comp` | 3:1 RMS from -18 dB | Evens out loud and soft words. |
| `limit`, `ceiling` | limiter -2 dB, clamp +-0.95 | The limiter let samples through when the level jumped; the clamp is the last guard. |

The auto gain's fast ("short") path is off: it moved the gain 20 dB in 20 ms on a
keystroke and pumped. Its controls have no default in the plugin, so a host picks
one (PipeWire takes the middle of the range, another host takes 0, and 0 stops the
gain from moving). The config sets all of them.

## The plugin

`pkgs/dpdfnet-ladspa/dpdfnet_ladspa.c`, about 600 lines of C. DPDFNet takes one
STFT frame per call plus a recurrent state vector. The plugin does the STFT
(kissfft, Vorbis window, n_fft 960, hop 480), the ONNX Runtime call, the inverse
STFT with overlap-add, and the LADSPA glue, following Ceva's reference streaming
code. It matches an independent numpy implementation to within 0.1 dB on the scores.

- **Latency is 60 ms**, reported on the `latency` port (PipeWire uses it): one
  window (20 ms) plus the model's own look-ahead of 4 frames (40 ms). PipeWire
  adds about 40 ms of buffering at quantum 1024.
- **It runs inline, in `run()`, on PipeWire's real-time thread.** A first version
  used a worker thread. A normal-priority thread starves when the machine is busy
  (load average 40 to 50 made frames take 10 times longer), while PipeWire's data
  thread runs at real-time priority through rtkit. The price: a frame that takes
  longer than the period is an xrun. A frame costs about 2.5 ms of CPU per 10 ms.
- **The model's state is guarded.** Input NaN and Inf are zeroed; a non-finite
  output resets the stream. A model failure passes the input through and clears
  the buffers.
- **Numbers are parsed in the C locale.** The model's metadata writes `0.5`; a host
  in a Swedish locale reads `0,5`. `strtof` stopped at the dot and the plugin
  failed to load, which left the microphone silent. Offline tests ran in the C
  locale and passed.
- **FFT:** kissfft (BSD-3). An earlier version linked FFTW, which is GPL and would
  have made the built plugin GPL.

## Choosing the microphone

`studiomic-select` opens every candidate at once, so all hear the same speech.

- **Liveness.** A source that delivers exact zeros or one bit of flicker over the
  last 1.5 s is dead (muted, unplugged).
- **Hearing.** A microphone that stays below 6 dB over its noise floor while
  another has 12 dB or more heard none of the speech and is *deaf*, until it hears
  speech again. This is not optional. DNSMOS rates a recording of near silence as
  good, so a deaf input outscored the microphone that heard the speaker: 2.21
  against 1.24 on the first real laptop.
- **Quality.** DNSMOS P.835 overall of the last 9 s (Microsoft DNS-Challenge,
  CC BY 4.0, pinned by hash): it falls with noise, reverb, distant pickup, clipping
  and narrow bandwidth. Clipping takes 0.5 off.
- **Scoring** happens only while somebody speaks (at least 5 % of the window is
  speech, 12 dB over some microphone's floor), on windows that do not overlap, so
  one sentence counts once.
- **Switching** needs a margin of 0.25 points, two evaluations, no speech on any
  microphone in the last 300 ms, and 60 s since the last switch. A dead or vanished
  current microphone is replaced at once, but never by one that has not been judged
  yet while a judged one exists.
- **Before anything is measured**, the WirePlumber ladder decides (USB, then
  Bluetooth, then webcam, then laptop array), so the filter has an input at once.

Tests: 18 switching, hearing and liveness checks (pure functions, run in CI), a ranking test
of seven synthetic microphones (headset, call-profile headset, desk, laptop array,
clipped, webcam across a room, dead; plus a deaf one), and a live test with fake
sources in a running PipeWire. In the live test a deaf source at the best ladder
rank is linked at once and left within 6 s, a dead one is never linked, the close
microphone takes over at 25 to 30 s, and a mute on it is noticed within 8 s on a
loaded machine.

The ranking test uses recordings built to differ; the order it expects is the one a
person expects (headset 2.90, call-profile headset 2.59, desk 2.53, laptop array
1.61, clipped 1.32, webcam 1.13 by DNSMOS), not a measurement of real rooms.

## Measured

See the table in the README. The scores are DNSMOS P.835 overall on clips the bench
builds: a public-domain voice at -20 dBFS with CC-licensed noise at 3 dB SNR, 10.75 s.
Noise left in the pauses (core of each pause, 100 ms from speech edges excluded)
drops by 63 dB for keystrokes, 70 dB for the fan and 47 dB for chatter with the
light model; the voice level drops about 2 dB.

What the numbers do not say: three clips with one voice; DNSMOS is an estimate and
cannot tell a studio sound from a laptop sound (the EQ moved it by 0 to +0.05, the
dynamics cost 0.06 to 0.10); SI-SDR is valid only for the bare plugin, because a
high-pass, EQ or leveler changes phase or gain over time and SI-SDR then reports
damage that is not there; live runs of the PipeWire graph are not sample-accurate
(the test streams start and drift by a few ms), so they are scored by DNSMOS and the
level test only.

## Tone: a blind listening test

The first EQ (+2 dB at 3.5 kHz, +1.5 dB above 9 kHz) made a laptop microphone sound
tinny. `studiomic-bench ab` renders one recording of the raw microphone through
seven variants at the same speech loudness, under shuffled letters, with one
player that switches at the same playback position. One listener rated:

| Variant | |
|---|---|
| raw | the microphone, unprocessed (hidden reference) |
| thin | denoised, cut below 450 Hz, +4 dB at 3 kHz (hidden anchor) |
| today | the first chain, with the bright EQ |
| flat | denoise, high-pass, leveling, no EQ |
| warm | flat + low shelf, -1.5 dB at 350 Hz, high shelf -3 dB at 6 kHz |
| **warm+deess** | warm + de-esser after the leveler (**rated best**) |
| matched | flat + automatic EQ toward a typical speech spectrum |

Only the winner was reported. One listener, one 10.6 s recording that had typing in
it, headphones unknown. The matched EQ used a target spectrum recalled from a
published average, so its loss says only that this target did not beat a fixed warm
curve. A second round on a longer recording would tell whether `warm` alone sounds as
good.

## Bugs the tests found

- The locale bug above.
- The compressor sat before the leveler and never triggered (threshold -24 dB,
  signal at -38 dB).
- A worker thread starved under load (see the plugin).
- The WirePlumber ladder file did not parse (a single backslash in a regular
  expression), and the Bluetooth rule was under the wrong monitor.
- `ProtectHome=true` hides `/run/user`, so a sandboxed service would not have found
  the PipeWire socket. It is `read-only`.
- A NaN in the input stayed in the model's state for good.
- A muted microphone took up to 9 s to notice, because liveness was judged on the
  whole window.
- A deaf input scored higher than the microphone that heard the speaker.
- A switch waited for a gap in speech that never came in test audio with no pauses:
  by design the selector switches between phrases, so the live test's speaker pauses.

## Open

- **Real-time safety is not proven.** ONNX Runtime may allocate or take a lock inside
  a call. A frame costs up to 5 ms of a 10 ms budget, so a client that forces a quantum
  under 512 samples makes the graph xrun. `pw-top` during a call with a low-latency
  client open would settle it.
- **The systemd units are tested by evaluation and by running the same config as a
  client, not by a full login.** CI has no PipeWire.
- **Which real microphone wins** is measured only on hardware where one microphone
  hears the speaker; a headset plugged in later is measured against it.
- **Bluetooth.** Whether a probe on a call-profile headset keeps WirePlumber from
  switching it back to music mode is not checked.
- **CPU.** A quarter of a core while the microphone is open. Skipping the model during
  silence would cut most of it. Ceva's native C runtime (used by HushMic) is reported
  at a third of ONNX Runtime's cost; not measured here.
- **Echo cancellation** for speakers (PipeWire's WebRTC module) is not in the chain.
- **A per-microphone EQ**, or one that adapts to the measured spectrum of the voice, is
  not built.
