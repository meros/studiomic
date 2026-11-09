self:
{
  config,
  lib,
  pkgs,
  ...
}:

# Home Manager module: services.studiomic, for a user on a system that already
# runs PipeWire with WirePlumber (NixOS or not). Everything lives in the user's
# home: systemd --user units and the WirePlumber ladder in
# ~/.config/wireplumber. The options are the same as the NixOS module's.

let
  cfg = config.services.studiomic;
  core = import ./core.nix { inherit lib pkgs cfg; };
in
{
  options.services.studiomic = import ./options.nix { inherit lib pkgs self; };

  config = lib.mkMerge [
    { services.studiomic.configFile = core.chain; }

    (lib.mkIf cfg.enable {
      home.packages = [ core.selector ];

      xdg.configFile."wireplumber/wireplumber.conf.d/53-studiomic-source-ladder.conf".text =
        core.wireplumberRules;

      systemd.user.services = lib.mapAttrs (_: s: {
        Unit = {
          Description = s.description;
          After = s.after;
          PartOf = s.partOf;
        }
        // s.unit;
        Service = s.service;
        Install.WantedBy = s.wantedBy;
      }) core.services;
    })
  ];
}
