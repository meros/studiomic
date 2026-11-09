# Testing

Three levels, from what CI runs to what needs a person.

## 1. `nix flake check` (CI, no audio, no network)

Builds the plugin, the selector and the bench; runs the selector's unit tests
(`tests/test_select.py`: switching rule, hearing, dead sources); evaluates the NixOS
and the Home Manager module; parses the generated PipeWire config and the WirePlumber
rules with PipeWire's own `spa-json-dump`; checks format (nixfmt), lint (statix,
deadnix), shell scripts (shellcheck) and that the Python files parse.

## 2. With test audio: `studiomic-bench`

`studiomic-bench` is `nix run .#studiomic-bench --`. It downloads public test audio on
request (see [NOTICE](../NOTICE)):

```sh
D=~/studiomic-bench
studiomic-bench corpus $D          # voice, keyboard, fan, chatter, and the DNSMOS model
studiomic-bench mix $D --snr 3     # noisy clips at 3 dB signal-to-noise
studiomic-bench run $D none none   # the unprocessed baseline
studiomic-bench run $D chain chain:-          # the default config, offline, deterministic
studiomic-bench levels $D chain chain:-       # the same clip at three input levels
studiomic-bench run $D plugin ladspa:dpdfnet_ladspa.so:dpdfnet_mono:100   # the bare plugin
studiomic-bench report $D          # a table of every run
studiomic-bench selftest $D        # selector ranking of synthetic microphones, plus the unit tests
```

`chain:-` runs the generated config offline through the same LADSPA plugins
(`tools/chain_host.py`; the biquads use the RBJ formulas). It is deterministic, so it
is the right place to tune. For a config of your own, `nix build` the module's
`configFile` option and pass `chain:<path>`.

What the columns mean is in `studiomic-bench --help` and in
[design.md](design.md#measured): `pause_db` and `speech_db` are level changes, `SIG`,
`BAK` and `OVR` are DNSMOS. SI-SDR is meaningful for the bare plugin only.

## 3. Live, in a running PipeWire (needs a session)

```sh
studiomic-bench pw-run - in.wav out.wav       # the real config as a client of your PipeWire
studiomic-bench live-test $D                  # selector with fake microphones, 85 s
```

`pw-run` starts the config under a throw-away node name with `priority.session` 0 (the
default microphone does not change) and no autoconnect (nothing reaches the speakers).
`live-test` creates four fake sources (`media.class Audio/Source`, priorities far below
any real device) and a stand-in for the filter's input, and prints which one is linked
second by second. Everything started is stopped on exit. Run them on a quiet machine:
a loaded one delays the selector (a switch that took 25 s took 50 s at load average 40).

Both can be slow or flaky under load; the timeline, not a single number, is the result.

## 4. By ear: the blind test

```sh
pw-record --target <your microphone> raw.wav       # 30 s of steady speech, Ctrl+C
studiomic-bench ab - raw.wav blind.html            # open blind.html in a browser
```

Seven variants of the tone chain at the same speech loudness, under shuffled letters,
one player that switches at the same position. Rate each 0 to 100, press "Reveal",
and keep the table. The page contains your recording; do not publish it.
