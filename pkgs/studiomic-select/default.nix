{
  lib,
  stdenv,
  fetchurl,
  makeWrapper,
  python3,
  pipewire,
}:

# studiomic-select: picks the best microphone by measurement and links it to
# studiomic's input. One Python file, numpy and ONNX Runtime from nixpkgs,
# PipeWire's own tools (pw-cat, pw-dump, pw-link) for everything that touches
# the audio graph.
#
# The quality model is Microsoft's DNSMOS P.835 (DNS-Challenge, CC BY 4.0),
# pinned by commit and hash: a learned estimate of how good recorded speech
# sounds, used here to compare microphones that heard the same speech.

let
  dnsmos = fetchurl {
    url = "https://raw.githubusercontent.com/microsoft/DNS-Challenge/82f1b17e7776a43eee395d0f45bae8abb700ad00/DNSMOS/DNSMOS/sig_bak_ovr.onnx";
    hash = "sha256-Jp++vbUTqiPN37tZNULsxUAoSpGEmsUFFocOGsePbt0=";
  };
  python = python3.withPackages (p: [
    p.numpy
    p.onnxruntime
  ]);
in
stdenv.mkDerivation {
  pname = "studiomic-select";
  version = "0.1.0";

  src = ./.;
  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm644 studiomic_select.py $out/lib/studiomic_select.py
    makeWrapper ${python}/bin/python3 $out/bin/studiomic-select \
      --add-flags "-I $out/lib/studiomic_select.py" \
      --prefix PATH : ${lib.makeBinPath [ pipewire ]} \
      --set-default DNSMOS_MODEL ${dnsmos}
    runHook postInstall
  '';

  passthru = { inherit dnsmos; };

  meta = {
    description = "Choose the best microphone by measurement and switch live";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "studiomic-select";
  };
}
