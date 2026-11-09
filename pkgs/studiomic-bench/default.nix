{
  lib,
  stdenv,
  makeWrapper,
  python3,
  ffmpeg,
  ladspa-sdk,
  pipewire,
  studiomic-select,
  # The config the module builds with every option at its default: what
  # `studiomic-bench config` prints and what the tests run.
  defaultConfig,
}:

# studiomic-bench: score a filter on speech mixed with noise, run the generated
# config offline or live, run the selector tests, and build the blind listening
# test. Nothing is downloaded at build time; `studiomic-bench corpus` fetches
# the public test audio when you ask for it.

let
  python = python3.withPackages (p: [
    p.numpy
    p.scipy
    p.soundfile
    p.onnxruntime
  ]);
in
stdenv.mkDerivation {
  pname = "studiomic-bench";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../tools
      ../../tests
    ];
  };
  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/studiomic/tools $out/bin
    cp -r tools/. $out/share/studiomic/tools
    cp -r tests $out/share/studiomic/tests
    # the selector module, imported by the selector tests
    mkdir -p $out/share/studiomic/pkgs/studiomic-select
    cp ${studiomic-select}/lib/studiomic_select.py $out/share/studiomic/pkgs/studiomic-select/
    makeWrapper ${python}/bin/python3 $out/bin/studiomic-bench \
      --add-flags "-I $out/share/studiomic/tools/studiomic_bench.py" \
      --set STUDIOMIC_DEFAULT_CONFIG ${defaultConfig} \
      --set STUDIOMIC_TOOLS $out/share/studiomic/tools \
      --prefix PATH : ${
        lib.makeBinPath [
          ffmpeg
          ladspa-sdk
          pipewire
        ]
      }
    runHook postInstall
  '';

  meta = {
    description = "Benchmark, offline and live test tools for studiomic";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "studiomic-bench";
  };
}
