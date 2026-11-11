{
  lib,
  pkgs,
  self,
}:

# The options of services.studiomic, shared by the NixOS and the Home Manager
# module so that both say the same thing.

let
  system = pkgs.stdenv.hostPlatform.system;
  packages = self.packages.${system};
in
{
  enable = lib.mkEnableOption "studiomic, a clean, leveled virtual microphone with measured microphone choice";

  package = lib.mkOption {
    type = lib.types.package;
    default = packages.dpdfnet-ladspa;
    defaultText = lib.literalExpression "studiomic.packages.\${system}.dpdfnet-ladspa";
    description = ''
      The DPDFNet LADSPA plugin. Its `defaultModel` is overridden by `model`.
    '';
  };

  selectorPackage = lib.mkOption {
    type = lib.types.package;
    default = packages.studiomic-select;
    defaultText = lib.literalExpression "studiomic.packages.\${system}.studiomic-select";
    description = "The microphone selector (`studiomic-select`).";
  };

  configFile = lib.mkOption {
    type = lib.types.path;
    readOnly = true;
    internal = true;
    description = "The PipeWire config the service runs. tools/ tests this file.";
  };

  model = lib.mkOption {
    type = lib.types.enum (builtins.attrNames packages.dpdfnet-ladspa.models);
    default = "dpdfnet8_48khz_hr";
    description = ''
      DPDFNet model. The light one scored within 0.05 DNSMOS of the quality one
      on the keyboard, fan and chatter test. On ONNX Runtime the quality one costs
      2.1 times the CPU (31.8 s against 14.8 s per 60 s of audio, Lunar Lake laptop,
      one core). That is 53 % of a core on average, and single calls take longer than
      the 10 ms hop in about 1 call in 20 (thread CPU time, quantum 480), so it can
      overrun inside the real-time thread. The light model never did (maximum 84 %
      of the budget). The quality model therefore runs on Ceva's native engine (about a
      fifth of that CPU), which needs AVX2 and FMA. Without them, or when the weights
      file does not check out, the plugin runs the light model instead and says so in
      the PipeWire client's log.
    '';
  };

  attenuationLimitDb = lib.mkOption {
    type = lib.types.numbers.between 0 100;
    default = 100;
    description = ''
      The most the model may remove, in dB. 100 is the model's full effect.
      A lower value mixes the original back in, which sounds more natural and
      leaves more noise.
    '';
  };

  targetLufs = lib.mkOption {
    type = lib.types.numbers.between (-60) 0;
    default = -23;
    description = "Loudness of speech after the auto gain, in LUFS.";
  };

  maxGainDb = lib.mkOption {
    type = lib.types.numbers.between 0 60;
    default = 24;
    description = "The most the auto gain may raise a quiet microphone, in dB.";
  };

  eq = lib.mkOption {
    type = lib.types.bool;
    default = true;
    description = "Voice EQ after the high-pass (the bands in `eqBands`).";
  };

  eqBands = lib.mkOption {
    type = lib.types.listOf (
      lib.types.submodule {
        options = {
          type = lib.mkOption {
            type = lib.types.enum [
              "lowshelf"
              "peaking"
              "highshelf"
            ];
            description = "Filter shape (RBJ audio-EQ cookbook biquad).";
          };
          freq = lib.mkOption {
            type = lib.types.numbers.between 20 20000;
            description = "Centre or corner frequency in Hz.";
          };
          q = lib.mkOption {
            type = lib.types.numbers.positive;
            description = "Quality factor.";
          };
          gain = lib.mkOption {
            type = lib.types.numbers.between (-24) 24;
            description = "Gain in dB.";
          };
        };
      }
    );
    default = [
      {
        type = "lowshelf";
        freq = 150.0;
        q = 0.7;
        gain = -2.5;
      }
      {
        type = "peaking";
        freq = 500.0;
        q = 1.0;
        gain = 3.5;
      }
      {
        type = "peaking";
        freq = 2000.0;
        q = 1.0;
        gain = 2.5;
      }
      {
        type = "peaking";
        freq = 3500.0;
        q = 1.0;
        gain = 4.0;
      }
      {
        type = "peaking";
        freq = 5500.0;
        q = 1.0;
        gain = 1.5;
      }
      {
        type = "highshelf";
        freq = 9000.0;
        q = 0.7;
        gain = -3.0;
      }
    ];
    description = ''
      The EQ bands, applied in order when `eq` is on. The default is fitted, third-octave
      band by band, to an offline reference that a listener rated best (docs/design.md).
      It was fitted on one voice and one microphone, so a close microphone that already
      has proximity bass may want a smaller cut at 150 Hz.
    '';
  };

  dynamics = lib.mkOption {
    type = lib.types.bool;
    default = true;
    description = "Auto gain, de-esser, compressor and limiter after the denoiser.";
  };

  autoSelect = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Choose the microphone by measurement. `studiomic-select` opens every
        microphone at once, scores what each recorded while somebody spoke,
        and links the best one to the filter's input. It switches live, only
        when another microphone is clearly better, only between phrases, and
        not more often than `dwellSeconds`. A microphone that delivers digital
        silence (muted, unplugged) or that heard none of the speech another one
        heard is never chosen. Every candidate microphone is open while it runs,
        so a desktop may show the microphone as in use. Off: WirePlumber links
        the default source.
      '';
    };
    onDemand = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Open no microphone until an application reads the voice filter's output
        (a call, a recorder), and close every microphone again 15 s after the
        last reader leaves. Opening the microphone of a Bluetooth headset makes
        WirePlumber switch the headset from music (A2DP) to call (HFP) quality,
        so without this the headset drops to call quality for as long as the
        selector runs, call or no call. The first seconds of a call take the
        choice from the last call, or the ladder. Off: every candidate is open
        all the time.
      '';
    };
    dwellSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 60;
      description = "Least time between two switches.";
    };
    intervalSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 5;
      description = "Time between two measurements.";
    };
    pattern = lib.mkOption {
      type = lib.types.str;
      # A character class, not \\.: systemd reads backslashes in ExecStart as escapes.
      default = "^(alsa_input|bluez_input)[.]";
      description = "Regular expression of the PipeWire node names that may be chosen.";
    };
  };

  nodeName = lib.mkOption {
    type = lib.types.str;
    default = "studiomic";
    description = "PipeWire node name of the virtual microphone.";
  };

  description = lib.mkOption {
    type = lib.types.str;
    default = "Studiomic";
    description = "Name that apps show for the virtual microphone.";
  };

  makeDefault = lib.mkOption {
    type = lib.types.bool;
    default = true;
    description = ''
      Make the virtual microphone the default microphone when the service
      starts. WirePlumber remembers a microphone picked by hand and that choice
      beats any priority, so the service sets the remembered default itself.
      Off: pick it in a sound panel, or run `wpctl set-default <id>`.
    '';
  };

  priority = lib.mkOption {
    type = lib.types.int;
    default = 3000;
    description = ''
      `priority.session` of the virtual microphone. Above every physical
      microphone (the ladder ends at 2000), so it is preferred as the default.
      0 keeps the default on the hardware.
    '';
  };

  sourcePriorities = lib.mkOption {
    type = lib.types.listOf (
      lib.types.submodule {
        options = {
          monitor = lib.mkOption {
            type = lib.types.enum [
              "alsa"
              "bluez"
            ];
            default = "alsa";
            description = "Which WirePlumber device monitor the node comes from.";
          };
          key = lib.mkOption {
            type = lib.types.str;
            default = "node.name";
            description = "The node property to match.";
          };
          match = lib.mkOption {
            type = lib.types.str;
            description = "PipeWire match value; a leading `~` makes it a regular expression.";
          };
          priority = lib.mkOption {
            type = lib.types.int;
            description = "`priority.session` for the matching nodes.";
          };
        };
      }
    );
    description = ''
      The ladder of physical microphones by kind of device, highest priority
      first. It decides before anything is measured, and when nothing can be
      measured, because the distance from the mouth sets the signal-to-noise
      ratio more than any other factor: a headset or desk microphone beats a
      webcam, and a webcam beats a laptop array. Later rules win when two match.
    '';
    default = [
      # Raw SoundWire DSP endpoints of recent laptops (no UCM, so no ports).
      {
        match = "~alsa_input\\..*sof_sdw\\.pro-input-.*";
        priority = 500;
      }
      # Bluetooth headset microphone: close to the mouth, but narrowband
      # (HFP is 16 kHz at best), so below a wired one.
      {
        monitor = "bluez";
        match = "~bluez_input\\..*";
        priority = 1500;
      }
      # Any other USB audio device: headsets, desk microphones, docks.
      {
        match = "~alsa_input\\.usb-.*";
        priority = 1900;
      }
      # Webcams: the microphone is far from the mouth. After the general USB
      # rule, so it wins for webcams.
      {
        match = "~alsa_input\\.usb-.*(OBSBOT|Camera|Webcam|C920|C922|Brio).*";
        priority = 800;
      }
    ];
  };
}
