{
  lib,
  pkgs,
  cfg,
}:

# What both modules need, derived from the options once: the plugin with the
# chosen model, the PipeWire config, the WirePlumber ladder, the script that
# makes the virtual microphone the default, and the two services in a neutral
# shape that each module turns into its own unit syntax.

let
  plugin = cfg.package.override { defaultModel = cfg.model; };
  selector = cfg.selectorPackage;

  chain = pkgs.callPackage ./chain.nix {
    dpdfnet-ladspa = plugin;
    inherit cfg;
  };

  # Wait for the virtual microphone, then make it the remembered default source
  # (what `wpctl set-default` does). Gives up after 15 s.
  makeDefault = pkgs.writeShellScript "studiomic-make-default" ''
    export PATH=${
      lib.makeBinPath [
        pkgs.pipewire
        pkgs.coreutils
        pkgs.gnugrep
      ]
    }
    for _ in $(seq 150); do
      if pw-link -o | grep -q '^${cfg.nodeName}:'; then
        exec pw-metadata -n default 0 default.configured.audio.source \
          '{ "name": "${cfg.nodeName}" }' Spa:String:JSON
      fi
      sleep 0.1
    done
    echo "studiomic: ${cfg.nodeName} did not appear" >&2
    exit 1
  '';

  # WirePlumber picks the default source by priority.session. The config file
  # is SPA-JSON, where a backslash in a string must be doubled; the options hold
  # the plain regular expression.
  escape = lib.replaceStrings [ "\\" ] [ "\\\\" ];
  rulesFor =
    monitor:
    lib.concatMapStrings (r: ''
      {
        matches = [ { ${r.key} = "${escape r.match}" } ]
        actions = { update-props = { priority.session = ${toString r.priority} } }
      }
    '') (lib.filter (r: r.monitor == monitor) cfg.sourcePriorities);

  wireplumberRules = ''
    monitor.alsa.rules = [
      ${rulesFor "alsa"}
    ]
    monitor.bluez.rules = [
      ${rulesFor "bluez"}
    ]
  '';

  # One set of sandbox settings for both services. "read-only" keeps
  # /run/user reachable: connecting to the PipeWire socket works on it, and
  # ProtectHome=true would hide the socket. rtkit (real-time priority) is on
  # the system bus; nothing else is needed.
  sandbox = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    ProtectSystem = "strict";
    ProtectHome = "read-only";
    ProtectKernelTunables = true;
    ProtectControlGroups = true;
    RestrictAddressFamilies = [ "AF_UNIX" ];
    LockPersonality = true;
  };

  services = {
    studiomic = {
      description = "Studiomic (PipeWire filter-chain with DPDFNet)";
      wantedBy = [ "pipewire.service" ];
      after = [
        "pipewire.service"
        "wireplumber.service"
      ];
      partOf = [ "pipewire.service" ];
      unit.StartLimitIntervalSec = 0; # keep retrying until PipeWire is up
      service =
        sandbox
        // {
          ExecStart = "${pkgs.pipewire}/bin/pipewire -c ${chain}";
          # A client that loses the PipeWire socket may exit with status 0, which
          # "on-failure" would not restart.
          Restart = "always";
          RestartSec = 2;
        }
        # The leading "-" lets the unit start even when this fails.
        // lib.optionalAttrs cfg.makeDefault { ExecStartPost = "-${makeDefault}"; };
    };
  }
  // lib.optionalAttrs cfg.autoSelect.enable {
    studiomic-select = {
      description = "Studiomic: choose the best microphone by measurement";
      wantedBy = [ "studiomic.service" ];
      after = [
        "studiomic.service"
        "wireplumber.service"
      ];
      partOf = [ "studiomic.service" ];
      unit.StartLimitIntervalSec = 0;
      service = sandbox // {
        ExecStart = lib.escapeShellArgs (
          [
            "${selector}/bin/studiomic-select"
            "run"
            "--chain"
            "${cfg.nodeName}.capture"
            "--pattern"
            cfg.autoSelect.pattern
            "--dwell"
            (toString cfg.autoSelect.dwellSeconds)
            "--interval"
            (toString cfg.autoSelect.intervalSeconds)
          ]
          ++ lib.optional cfg.autoSelect.onDemand "--on-demand"
        );
        Restart = "always";
        RestartSec = 2;
        # It writes its status file to $XDG_RUNTIME_DIR/studiomic.
        RuntimeDirectory = "studiomic";
      };
    };
  };
in
{
  inherit
    plugin
    selector
    chain
    wireplumberRules
    services
    ;
}
