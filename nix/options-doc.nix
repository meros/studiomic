{
  lib,
  pkgs,
  self,
}:

# docs/options.md: every services.studiomic option, from the module itself.

let
  eval = lib.evalModules {
    modules = [ { options.services.studiomic = import ./options.nix { inherit lib pkgs self; }; } ];
  };
  doc = pkgs.nixosOptionsDoc {
    options = eval.options.services;
    warningsAreErrors = false;
  };
in
pkgs.runCommand "studiomic-options.md" { } ''
  cp ${doc.optionsCommonMark} $out
''
