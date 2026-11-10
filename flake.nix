{
  description = "studiomic: any Linux microphone, cleaned, leveled and tuned. Neural denoising, measured microphone choice, PipeWire.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # Only the checks use it, to build the Home Manager module.
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
    }:
    let
      inherit (nixpkgs) lib;
      # The plugin is tested on x86-64 only (it uses SSE flags behind a guard and
      # ONNX Runtime from nixpkgs; nothing blocks aarch64, nobody has run it).
      systems = [ "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # Use it:  imports = [ studiomic.nixosModules.default ];  services.studiomic.enable = true;
      nixosModules = {
        default = self.nixosModules.studiomic;
        studiomic = import ./nix/nixos.nix self;
      };

      # The same options for a user on a system that already runs PipeWire.
      homeManagerModules = {
        default = self.homeManagerModules.studiomic;
        studiomic = import ./nix/home-manager.nix self;
      };

      overlays.default = final: _prev: {
        dpdfnet-native = final.callPackage ./pkgs/dpdfnet-native { };
        dpdfnet-ladspa = final.callPackage ./pkgs/dpdfnet-ladspa { };
        studiomic-select = final.callPackage ./pkgs/studiomic-select { };
      };

      packages = forAllSystems (pkgs: rec {
        dpdfnet-native = pkgs.callPackage ./pkgs/dpdfnet-native { };
        dpdfnet-ladspa = pkgs.callPackage ./pkgs/dpdfnet-ladspa { inherit dpdfnet-native; };
        studiomic-select = pkgs.callPackage ./pkgs/studiomic-select { };
        # The PipeWire config with every option at its default.
        config = import ./nix/default-config.nix { inherit lib pkgs self; };
        studiomic-bench = pkgs.callPackage ./pkgs/studiomic-bench {
          inherit studiomic-select;
          defaultConfig = config;
        };
        # Reference of every option, generated from the module.
        options-doc = import ./nix/options-doc.nix { inherit lib pkgs self; };
        default = studiomic-select;
      });

      apps = forAllSystems (
        pkgs:
        let
          p = self.packages.${pkgs.stdenv.hostPlatform.system};
        in
        {
          studiomic-select = {
            type = "app";
            program = lib.getExe p.studiomic-select;
            meta.description = "Measure the microphones and print the ranking";
          };
          studiomic-bench = {
            type = "app";
            program = lib.getExe p.studiomic-bench;
            meta.description = "Benchmark and test studiomic";
          };
          default = self.apps.${pkgs.stdenv.hostPlatform.system}.studiomic-select;
        }
      );

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (p: [
              p.numpy
              p.scipy
              p.soundfile
              p.onnxruntime
            ]))
            pkgs.ladspa-sdk
            pkgs.pipewire
            pkgs.ffmpeg
            pkgs.shellcheck
            pkgs.nixfmt
            pkgs.statix
            pkgs.deadnix
          ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);

      checks = forAllSystems (
        pkgs:
        import ./nix/checks.nix {
          inherit
            lib
            pkgs
            self
            nixpkgs
            home-manager
            ;
        }
      );
    };
}
