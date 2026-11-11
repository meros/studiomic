# studiomic

Any Linux microphone, cleaned, leveled and tuned: a virtual PipeWire microphone
that removes keyboard noise, fans and chatter with a neural denoiser, shapes the
voice, and picks the best physical microphone **by measuring them**, switching
live. A Nix flake with NixOS and Home Manager modules.

```nix
inputs.studiomic.url = "github:meros/studiomic";

# NixOS
imports = [ studiomic.nixosModules.default ];
services.studiomic.enable = true;
```

After a rebuild, "Studiomic" is the default microphone. Nothing else to set.

## What you get

- **A neural denoiser in a PipeWire filter-chain.** [DPDFNet](https://arxiv.org/abs/2512.16420)
  (Ceva, Apache-2.0) runs in a small LADSPA plugin written for this project.
  On a mix of speech and typing at 3 dB signal-to-noise, it removes about 63 dB
  of keystrokes from the pauses and keeps the voice (DNSMOS 3.23, unprocessed 1.79).
- **A voice chain tuned by listening, not by habit.** High-pass, a warm EQ,
  auto gain to a target loudness, de-esser, compressor, limiter, hard ceiling.
  The EQ came out of a blind test; the first version was tinny.
- **Measured microphone choice, switched live.** `studiomic-select` opens every
  microphone at once, scores what each recorded while someone spoke, and links the
  best to the filter. Muted, unplugged and *deaf* inputs are never chosen. It
  switches only on a clear margin and only between phrases.
- **Nix native.** Packages, an overlay, a NixOS module, a Home Manager module, a
  generated options reference, a dev shell, and `nix flake check` that evaluates
  both modules and parses the generated PipeWire config with PipeWire's own parser.

## How it works

```
 every microphone ─┬─ probe (16 kHz) ─▶ studiomic-select ─ scores ─▶ link
                   │                                                   │
                   └────────────── the chosen one ─────────────────────┤
                                                                       ▼
   capture ▶ DPDFNet ▶ high-pass ▶ EQ ▶ auto gain ▶ de-ess ▶ comp ▶ limiter ▶ clamp ▶ "Studiomic"
             (own LADSPA plugin)         └──────── LSP Plugins (LADSPA) ────────┘     (Audio/Source)
```

The filter runs as its own PipeWire client process (a user service), not inside
the PipeWire daemon, so a crash in the plugin cannot take audio down. The
design, the measurements and the bugs the tests found are in
[docs/design.md](docs/design.md).

## Use it

**NixOS** (needs `services.pipewire.enable` and `services.pipewire.wireplumber.enable`):

```nix
{
  inputs.studiomic.url = "github:meros/studiomic";
  inputs.studiomic.inputs.nixpkgs.follows = "nixpkgs";

  outputs = { nixpkgs, studiomic, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [
        studiomic.nixosModules.default
        { services.studiomic.enable = true; }
      ];
    };
  };
}
```

**Home Manager**, on a system that already runs PipeWire with WirePlumber:
`studiomic.homeManagerModules.default`, same options. It writes the user units and
`~/.config/wireplumber/wireplumber.conf.d/53-studiomic-source-ladder.conf`.

**Try the selector without installing anything:**

```sh
nix run github:meros/studiomic -- once --seconds 30   # talk while it runs
```

It prints each microphone's level, whether it heard you, and its score. It
changes nothing.

Every option is in [docs/options.md](docs/options.md). The ones people change:

| Option | Default | What it does |
|---|---|---|
| `model` | `dpdfnet2_48khz_hr` | `dpdfnet8_48khz_hr` is the quality model: within 0.05 DNSMOS, 2.1 times the CPU on ONNX Runtime, and too slow for the real-time thread there (docs/design.md) |
| `attenuationLimitDb` | `100` | the most the denoiser may remove; lower mixes the original back in |
| `targetLufs` | `-23` | speech loudness after the auto gain |
| `eq`, `dynamics` | `true` | the EQ (`eqBands`, fitted to a reference), and auto gain + de-esser + compressor + limiter |
| `autoSelect.enable` | `true` | measured microphone choice; off leaves the default source to WirePlumber |
| `autoSelect.onDemand` | `true` | open no microphone until an application reads the voice filter; a Bluetooth headset then stays in music quality outside calls |
| `makeDefault` | `true` | make Studiomic the default microphone when the service starts |

## Measured

Offline, 3 dB signal-to-noise, a public-domain voice with CC-licensed noise,
DNSMOS P.835 overall (1 to 5, higher is better):

| | keyboard | fan | chatter | latency |
|---|---|---|---|---|
| unprocessed | 1.79 | 1.19 | 1.11 | 0 ms |
| DeepFilterNet3 | 3.10 | 3.02 | 2.69 | 20 ms |
| **DPDFNet, light** | **3.23** | **3.30** | **3.19** | 60 ms |
| DPDFNet, quality | 3.28 | 3.29 | 3.22 | 60 ms |
| whole chain (default options) | 3.13 | 3.21 | 3.16 | 65 ms |

The light model needs about 2.5 ms of CPU per 10 ms of audio, about a quarter of
one core. Leveling: the same clip at -45, -35 and -25 dBFS comes out at -20, -19
and -19 dBFS speech level with peaks under -1 dBFS.

**What these numbers do not say.** Three clips with one voice. DNSMOS is a
learned estimate; it rates near silence as good and cannot judge whether a voice
sounds like a studio microphone. The tone was chosen by one listener on one short
recording. Live runs on a loaded machine move DNSMOS by up to 0.4. Details and the
test that found each limit are in [docs/design.md](docs/design.md).

## Limits

- **Tested on one laptop** (an Intel Lunar Lake ThinkPad, NixOS unstable, PipeWire
  1.6, x86-64). aarch64 is untested. Other distributions have no package yet.
- **Real-time safety is not proven.** The model runs in PipeWire's real-time
  thread. A frame costs up to 5 ms of a 10 ms budget, so a client that forces a
  quantum under 512 samples makes the graph xrun. The config asks for 1024.
- **Every candidate microphone is open while an application reads Studiomic**, so a desktop
  may show the microphone as in use (all the time with `autoSelect.onDemand = false`). Audio stays in memory (9 s per microphone).
- **No echo cancellation** for speakers yet, and Bluetooth headsets: opening the headset's microphone switches it to call quality
  (HFP), so `autoSelect.onDemand` keeps it closed outside calls. Seen on one WH-1000XM6.
- **A mic that never pauses keeps the choice.** The selector switches between phrases.
- **Not a studio microphone.** Research found no real-time method that turns a thin
  laptop microphone into a studio one on this kind of hardware ([docs/research.md](docs/research.md)).
  Moving the microphone closer beats any filter.

## Develop and test

```sh
nix develop        # python, ladspa-sdk, pipewire, ffmpeg, nixfmt, statix, shellcheck
nix flake check    # builds, unit tests, module evaluation, config parsing, lint
nix run .#studiomic-bench -- --help
```

[docs/testing.md](docs/testing.md) covers the corpus tests, the live test with fake
microphones, and the blind listening test.

## Licence

MIT. The DPDFNet weights are Apache-2.0 (Ceva), the DNSMOS model is CC BY 4.0
(Microsoft); see [NOTICE](NOTICE).
