{
  lib,
  pkgs,
  self,
  nixpkgs,
  home-manager,
}:

# `nix flake check` builds all of these. None needs audio hardware or a network.

let
  system = pkgs.stdenv.hostPlatform.system;
  p = self.packages.${system};

  python = pkgs.python3.withPackages (ps: [ ps.numpy ]);

  # A NixOS configuration with the module on, only evaluated: the services and
  # the WirePlumber ladder it produces are written to a file, which forces the
  # whole module and builds every package it names.
  nixos = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [
      self.nixosModules.default
      {
        boot.isContainer = true;
        system.stateVersion = "25.11";
        services.pipewire = {
          enable = true;
          wireplumber.enable = true;
        };
        services.studiomic.enable = true;
      }
    ];
  };

  hm = home-manager.lib.homeManagerConfiguration {
    inherit pkgs;
    modules = [
      self.homeManagerModules.default
      {
        home = {
          username = "test";
          homeDirectory = "/home/test";
          stateVersion = "25.11";
        };
        services.studiomic.enable = true;
      }
    ];
  };
in
{
  plugin = p.dpdfnet-ladspa;
  select = p.studiomic-select;
  bench = p.studiomic-bench;
  inherit (p) options-doc;

  # The decisions of the selector: margin, dwell, hearing, dead sources.
  unit-tests = pkgs.runCommand "studiomic-unit-tests" { nativeBuildInputs = [ python ]; } ''
    cp -r ${../pkgs} pkgs; cp -r ${../tests} tests
    python3 -I tests/test_select.py | tee $out
    ! grep -q '^FAIL' $out
  '';

  # PipeWire's own parser reads the generated config and the WirePlumber rules.
  config-parses =
    pkgs.runCommand "studiomic-config-parses" { nativeBuildInputs = [ pkgs.pipewire ]; }
      ''
        spa-json-dump ${p.config} > /dev/null
        spa-json-dump -s ${
          (pkgs.writeText "ladder"
            (import ./core.nix {
              inherit lib pkgs;
              cfg = nixos.config.services.studiomic;
            }).wireplumberRules
          )
        } > /dev/null
        touch $out
      '';

  nixos-module =
    let
      units = lib.filterAttrs (n: _: lib.hasPrefix "studiomic" n) nixos.config.systemd.user.services;
    in
    pkgs.writeText "studiomic-nixos-module.txt" (
      lib.concatStringsSep "\n" (
        lib.mapAttrsToList (n: u: "${n}: ${u.serviceConfig.ExecStart}") units
        ++ [
          (import ./core.nix {
            inherit lib pkgs;
            cfg = nixos.config.services.studiomic;
          }).wireplumberRules
        ]
      )
    );

  home-manager-module = hm.config.home-files;

  format = pkgs.runCommand "studiomic-format" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
    nixfmt --check ${../flake.nix} ${../nix}/*.nix ${../pkgs}/*/default.nix
    touch $out
  '';

  lint =
    pkgs.runCommand "studiomic-lint"
      {
        nativeBuildInputs = [
          pkgs.statix
          pkgs.deadnix
        ];
      }
      ''
        cd ${../.}
        statix check .
        deadnix --fail .
        touch $out
      '';

  scripts =
    pkgs.runCommand "studiomic-scripts"
      {
        nativeBuildInputs = [
          pkgs.shellcheck
          pkgs.python3
        ];
      }
      ''
            shellcheck ${../tools/pw-run}
            python3 - ${../tools}/*.py ${../pkgs/studiomic-select/studiomic_select.py} ${../tests}/*.py <<'E'
        import ast, sys
        for f in sys.argv[1:]:
            ast.parse(open(f).read(), f)
        E
            touch $out
      '';
}
