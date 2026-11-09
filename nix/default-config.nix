{
  lib,
  pkgs,
  self,
}:

# The PipeWire config the module builds when every option is at its default.
# `nix build .#config` prints it; studiomic-bench and the checks test this file.

let
  evaluated = lib.evalModules {
    modules = [ { options.services.studiomic = import ./options.nix { inherit lib pkgs self; }; } ];
  };
in
(import ./core.nix {
  inherit lib pkgs;
  cfg = evaluated.config.services.studiomic;
}).chain
