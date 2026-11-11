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
| `eq1` to `eq6` | -2.5 dB low shelf 150 Hz, +3.5 dB at 500 Hz, +2.5 dB at 2 kHz, +4 dB at 3.5 kHz, +1.5 dB at 5.5 kHz, -3 dB high shelf 9 kHz | Fitted to a reference ("Tone against a reference" below). The first EQ came from a blind test on another recording. |
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
With `--on-demand` (the module default) it opens them only while an application
reads the voice filter's output (a link from that node exists), and closes them
15 s after the last reader leaves, because opening a Bluetooth headset's
microphone switches the headset from A2DP to HFP.

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

## Tone against a reference

The blind test above was one listener, one recording and one microphone. A second round used
an offline reference instead: the same 60 s of a laptop microphone (Pro 10 input, speech,
typing, soft and loud speech, a Swedish sentence), processed offline by DPDFNet8 and then
Resemble Enhance (MIT, nfe 32, 401 s of CPU for 60 s of audio, so not real time). One listener
rated it clearly best of four in a blind, loudness-matched, shuffled test; a second round
rated the real-time chain with the fitted EQ and the quality model best of the real-time
variants.

The chain was run offline on the raw recording (`tools/chain_host.py`) and compared with the
reference on the reference's speech frames, after matching speech level. The measure is the
difference of the average power in third-octave bands from 100 Hz to 13 kHz. Each iteration
moved the EQ gains by the difference; it converged in three. Result, same recording:

| | tone error (RMS over 22 bands) | pause level | DNSMOS OVR |
|---|---|---|---|
| EQ before (180 Hz shelf, 350 Hz, 6 kHz shelf), light model | 3.5 dB | -79 dBFS | 3.07 |
| fitted EQ, light model | 1.2 dB | -77 dBFS | 3.10 |
| fitted EQ, quality model | 1.2 dB | -84 dBFS | 3.11 |
| the reference | 0 | -86 dBFS | 3.39 |

What did not help, each measured on the same recording:

- **A gate after the auto gain** (threshold -58 to -46 dBFS, 10 or 20 dB of reduction): the
  long silences drop by 20 dB (-99 to -119 dBFS), which nobody hears, and the pause measure
  moves by 0.2 dB. Short gaps between words at -50 to -63 dBFS and typing between words keep
  the gate open.
- **A second denoiser pass** (8 then 8, or 8 then 2): tone 1.26 against 1.21 dB, DNSMOS 3.07
  against 3.09.
- **Lower attenuation limit** (30 dB): DNSMOS 3.13 against 3.11, pause level -68 against
  -84 dBFS. At 15 dB the pause level is -46 dBFS.
- **No de-esser, or a threshold of 0.02 or 0.05**: tone error 2.0, 1.44 and 1.43 dB against
  1.21 dB with 0.03. The de-esser accounts for a large part of the match to the reference.
- **A harmonic post-filter**: not built. The autocorrelation peak in voiced frames is 0.63 for
  the real-time chain, 0.66 for the reference and 0.61 for the raw microphone, about 0.7 dB of
  harmonic-to-noise ratio.
- **Log-spectral distance** stays near 6.7 dB whatever the EQ does. The reference has sharper
  harmonics (peaks about 2.5 dB higher), which is generative detail, so the distance is not
  a target.

Limits. One voice, one microphone, 60 s, one listener, and the EQ is global: a close
microphone with its own proximity bass may want less cut at 150 Hz. The facit is a
generative model's output, so the fit matches its tone, not a studio recording. DNSMOS stays
0.3 below the reference (3.11 against 3.39); nothing tried closes that gap.

CPU time per call of the plugin (thread CPU time, 60 s of audio, a loaded machine, load
average about 12; ONNX Runtime, one thread):

| model | block | p50 | p99 | max | calls over the budget |
|---|---|---|---|---|---|
| light | 480 (10 ms) | 31 % | 52 % | 84 % | 0 of 5989 |
| light | 1024 (21 ms) | 22 % | 60 % | 74 % | 0 of 2796 |
| quality | 480 (10 ms) | 57 % | 117 % | 159 % | 633 of 5989 |
| quality | 1024 (21 ms) | 52 % | 131 % | 156 % | 124 of 2796 |

The quality model overruns its budget in 4 to 11 % of the calls on ONNX Runtime at every
quantum tried, so on ONNX Runtime it cannot run in the real-time thread.

Ceva's own C runtime for the same models (W7A8: 7-bit weights, 8-bit activations, AVX2
kernels with AVX-VNNI variants chosen at run time; `pkgs/dpdfnet-native`) runs one 10 ms hop
per call without allocation. Same recording, same method, thread CPU time:

| model and engine | block | p50 | p99 | max | calls over the budget |
|---|---|---|---|---|---|
| quality, native | 480 (10 ms) | 11 % | 22 % | 25 % | 0 of 5989 |
| quality, native | 1024 (21 ms) | 10 % | 26 % | 31 % | 0 of 2796 |

The output matches ONNX Runtime on the quality model: tone error 1.21 against 1.20 dB, pause
level -83.8 against -84.3 dBFS, DNSMOS OVR 3.09 against 3.11. On the light model the native
engine is worse in the pauses (-72 against -77 dBFS), so the plugin uses the native engine for
the quality model only (`DPDFNET_ENGINE=native` forces it for the light one). The whole
default chain now needs 6.4 s of CPU for 60 s of audio, against 20.8 s for the old default
(light model on ONNX Runtime). The plugin checks the weights file against the SHA-256 in the
model metadata before it uses it. On a CPU without AVX2 and FMA, or with a weights file that
does not check out, it runs the light model on ONNX Runtime and says so in the log.

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
