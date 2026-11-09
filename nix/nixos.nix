self:
{
  config,
  lib,
  pkgs,
  ...
}:

# NixOS module: services.studiomic. The services are user units (systemd
# --user), because PipeWire runs in the user session; the WirePlumber ladder is
# system wide.

let
  cfg = config.services.studiomic;
  core = import ./core.nix { inherit lib pkgs cfg; };
in
{
  options.services.studiomic = import ./options.nix { inherit lib pkgs self; };

  config = lib.mkMerge [
    { services.studiomic.configFile = core.chain; }

    (lib.mkIf cfg.enable {
      assertions = [
        {
          assertion = config.services.pipewire.enable && config.services.pipewire.wireplumber.enable;
          message = "services.studiomic needs services.pipewire.enable and services.pipewire.wireplumber.enable.";
        }
      ];

      # `studiomic-select once` prints what each microphone scores; `status`
      # shows what the running service knows.
      environment.systemPackages = [ core.selector ];

      services.pipewire.wireplumber.configPackages = [
        (pkgs.writeTextDir "share/wireplumber/wireplumber.conf.d/53-studiomic-source-ladder.conf" core.wireplumberRules)
      ];

      systemd.user.services = lib.mapAttrs (_: s: {
        inherit (s)
          description
          wantedBy
          after
          partOf
          ;
        unitConfig = s.unit;
        serviceConfig = s.service;
      }) core.services;
    })
  ];
}
