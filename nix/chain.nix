{
  lib,
  writeText,
  dpdfnet-ladspa,
  lsp-plugins,
  cfg,
}:

# The PipeWire config of studiomic, as a pure function of the options. The
# service runs it as `pipewire -c <file>` (a filter-chain in its own client
# process); tools/pw-run runs the same file against a live PipeWire to test it,
# and tools/chain_host.py runs it offline. PipeWire reads JSON as config.
#
# The chain, in signal order, and why each step sits where it does:
#
#   dpdf      DPDFNet: removes keystrokes, fans, chatter. First, on the raw
#             signal: it scored lower when a high-pass came before it (DNSMOS
#             OVR 3.18 against 3.23 on the keyboard clip, 3.02 against 3.19 on
#             the chatter clip), and everything after it (EQ gain, auto gain)
#             would lift the noise that is still there.
#   hpf       80 Hz high-pass: desk thumps and mains hum are below the voice.
#   eq1 .. eqN
#             The EQ (option eqBands). The default is fitted to a reference:
#             the chain's output was matched, third-octave band by band, to an
#             offline DPDFNet8 + Resemble Enhance rendering that a listener rated
#             best (docs/design.md, "Tone against a reference"). It cuts the
#             low end (-2.5 dB shelf at 150 Hz), lifts the presence (+3.5 dB at
#             500 Hz, +2.5 dB at 2 kHz, +4 dB at 3.5 kHz, +1.5 dB at 5.5 kHz) and
#             trims the air (-3 dB shelf at 9 kHz). The earlier EQ (+3 dB shelf
#             at 180 Hz, -1.5 dB at 350 Hz, -3 dB shelf at 6 kHz) came from a
#             blind test on another recording and scored 3.4 dB RMS away from
#             the reference where this one scores 1.2 dB.
#   gain      LSP auto gain: moves the level of speech toward targetLufs and
#             leaves pauses alone (anything under -55 LUFS counts as silence),
#             so a quiet mic and a loud mic end up at the same level. It comes
#             before the compressor, because the compressor's threshold means
#             something only once the level is known.
#   deess     LSP de-esser on 4.5 to 10 kHz, after the leveler so that the
#             signal reaches its threshold.
#   comp      3:1 RMS compressor from -18 dB: evens out loud and soft words.
#   limit     Limiter at -2 dB: a laugh or a cough cannot clip.
#   ceiling   Hard clamp at +-0.95, the last guard.

let
  lsp = "${lsp-plugins}/lib/ladspa/lsp-plugins-ladspa.so";
  lspLabel = name: "http://lsp-plug.in/plugins/ladspa/${name}";
  bq = name: label: freq: q: gain: {
    type = "builtin";
    inherit name label;
    control = {
      Freq = freq;
      Q = q;
    }
    // lib.optionalAttrs (gain != null) { Gain = gain; };
  };

  nodes = [
    {
      type = "ladspa";
      name = "dpdf";
      plugin = "${dpdfnet-ladspa}/lib/ladspa/dpdfnet_ladspa.so";
      label = "dpdfnet_mono";
      control."Attenuation limit (dB)" = cfg.attenuationLimitDb;
    }
    (bq "hpf" "bq_highpass" 80.0 0.707 null)
  ]
  ++ lib.optionals cfg.eq (
    lib.imap1 (i: b: bq "eq${toString i}" "bq_${b.type}" b.freq b.q b.gain) cfg.eqBands
  )
  ++ lib.optionals cfg.dynamics [
    {
      type = "ladspa";
      name = "gain";
      plugin = lsp;
      label = lspLabel "autogain_mono";
      control = {
        "Desired loudness level (LUFS)" = cfg.targetLufs;
        "The level of silence (LUFS)" = -55.0;
        "Enable maximum amplification gain limitation" = 1;
        "The maximum amplification gain (dB)" = cfg.maxGainDb;
        # These have no default in the plugin, so a host picks one: PipeWire
        # takes the middle of the range, another host takes 0, and 0 means "do
        # not move the gain". Set every one, so the result does not depend on
        # the host. Only the slow (long) path moves the gain; the fast (short)
        # path jumped 20 dB inside 20 ms on a keystroke and pumped.
        "Level drift (dB)" = 12.0;
        "Long gain grow amount" = 6;
        "Long gain fall amount" = 6;
        "Short gain grow amount" = 0;
        "Short gain fall amount" = 0;
        "Long gain grow time (ms)" = 316.0;
        "Long gain fall time (ms)" = 316.0;
        "Short gain grow time (ms)" = 22.0;
        "Short gain fall time (ms)" = 8.9;
      };
    }
    {
      type = "ladspa";
      name = "deess";
      plugin = lsp;
      label = lspLabel "deesser_mono";
      control = {
        # Only the 4.5 to 10 kHz band listens, and the sibilants it hears are
        # compressed 4:1 above -30 dB. After the leveler on purpose: before it
        # the signal is too quiet to reach the threshold.
        "High-pass filter frequency (Hz)" = 4500.0;
        "Low-pass filter frequency (Hz)" = 10000.0;
        "Threshold (G)" = 0.03;
        "Ratio" = 4.0;
        # No default in the plugin: PipeWire takes the middle of 0 to 3, which
        # is not a whole number. 1 is what the blind test heard.
        "Enable frequency split" = 1;
      };
    }
    {
      type = "ladspa";
      name = "comp";
      plugin = lsp;
      label = lspLabel "compressor_mono";
      control = {
        "Compression mode" = 0; # downward
        "Sidechain mode" = 1; # RMS
        "Attack threshold (G)" = 0.126; # -18 dB, 2 dB above the speech level
        "Attack time (ms)" = 8.0;
        "Release time (ms)" = 140.0;
        "Ratio" = 3.0;
        "Knee (G)" = 0.5;
        "Makeup gain (G)" = 1.0;
      };
    }
    {
      type = "ladspa";
      name = "limit";
      plugin = lsp;
      label = lspLabel "limiter_mono";
      control."Threshold (G)" = 0.794; # -2 dB
    }
    # Last, a hard ceiling. The limiter lets a few samples through when the
    # level jumps; this makes sure nothing reaches full scale.
    {
      type = "builtin";
      name = "ceiling";
      label = "clamp";
      control = {
        Min = -0.95;
        Max = 0.95;
      };
    }
  ];

  # Ports differ between the builtin filters (In/Out) and the LADSPA plugins
  # (Input/Output), so each node names its own.
  portIn = n: if n.type == "builtin" then "In" else "Input";
  portOut = n: if n.type == "builtin" then "Out" else "Output";
  links = lib.zipListsWith (a: b: {
    output = "${a.name}:${portOut a}";
    input = "${b.name}:${portIn b}";
  }) (lib.init nodes) (lib.tail nodes);
  first = lib.head nodes;
  last = lib.last nodes;

  conf = {
    "context.properties"."log.level" = 2;
    "context.spa-libs" = {
      "audio.convert.*" = "audioconvert/libspa-audioconvert";
      "support.*" = "support/libspa-support";
    };
    "context.modules" = [
      {
        name = "libpipewire-module-rt";
        args."nice.level" = -11;
        flags = [
          "ifexists"
          "nofail"
        ];
      }
      { name = "libpipewire-module-protocol-native"; }
      { name = "libpipewire-module-client-node"; }
      { name = "libpipewire-module-adapter"; }
      {
        name = "libpipewire-module-filter-chain";
        args = {
          "node.description" = cfg.description;
          "media.name" = cfg.description;
          "filter.graph" = {
            inherit nodes links;
            inputs = [ "${first.name}:${portIn first}" ];
            outputs = [ "${last.name}:${portOut last}" ];
          };
          "audio.rate" = 48000;
          "audio.position" = [ "MONO" ];
          "capture.props" = {
            "node.name" = "${cfg.nodeName}.capture";
            "node.passive" = true;
            # With the selector on, it links the best microphone to this input
            # itself, and WirePlumber must not also link the default source.
            "node.autoconnect" = !cfg.autoSelect.enable;
            # The model needs about 3 ms of CPU per 10 ms of audio in the
            # real-time thread, so ask for 1024 samples (21 ms) per cycle.
            "node.latency" = "1024/48000";
          };
          "playback.props" = {
            "node.name" = cfg.nodeName;
            "media.class" = "Audio/Source";
            "priority.session" = cfg.priority;
          };
        };
      }
    ];
  };
in
writeText "studiomic.conf" (builtins.toJSON conf)
