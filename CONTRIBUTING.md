# Contributing

`nix flake check` is the gate: it builds the packages, runs the selector's
unit tests, evaluates both modules, parses the generated configs with PipeWire's
own parser, and checks format and lint. `nix develop` gives a shell with the
tools. `nix fmt` formats the Nix files.

A change to the audio chain (`nix/chain.nix`) needs a measurement, not an
opinion. `studiomic-bench` runs the offline scores, the leveling test and the
blind listening test; `docs/testing.md` says how. A change that cannot be heard
on a number or in a blind test should say why it is worth making anyway.

When you change an option, regenerate the reference:

    nix build .#options-doc && cp result docs/options.md

Report what you ran and what you did not run. "CI is green" does not cover a
PipeWire graph, a real microphone or a Bluetooth headset; none of them runs in
CI.
