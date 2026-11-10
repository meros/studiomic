{
  lib,
  stdenv,
  fetchurl,
  ladspa-sdk,
  kissfftFloat,
  onnxruntime,
  dpdfnet-native,
  # The model the plugin loads when DPDFNET_MODEL is not set. The module
  # passes its `model` option here, so the generated config names the right
  # plugin file and the benchmark tests the model that runs.
  defaultModel ? "dpdfnet8_48khz_hr",
}:

# dpdfnet_ladspa — DPDFNet speech enhancement as a mono LADSPA plugin, for a
# PipeWire filter-chain (modules/nixos/services/voice-filter).
#
# The plugin is ours (dpdfnet_ladspa.c: causal STFT, model call, overlap-add).
# The model runs on Ceva's native C runtime (pkgs/dpdfnet-native) when the CPU has
# AVX2 and FMA and the model is the quality one, and on ONNX Runtime otherwise.
# The other building blocks are kissfft, ONNX Runtime and the LADSPA header, all
# from nixpkgs. The model weights are Ceva's DPDFNet (Apache-2.0,
# arXiv:2512.16420), pinned by revision and hash. The two 48 kHz "hr" models
# are the ones that run on full-band audio; dpdfnet2 is the light one,
# dpdfnet8 the quality one.

let
  revision = "dd6818d00f50c836fed43a6243ebe49116de5964";
  model =
    name: hash:
    fetchurl {
      url = "https://huggingface.co/Ceva-IP/DPDFNet/resolve/${revision}/onnx/${name}.onnx";
      inherit hash;
    };
  models = {
    dpdfnet2_48khz_hr = model "dpdfnet2_48khz_hr" "sha256-fwV1pc7Auk/9j4vWV+BtAH5MzdlV12+quSK50ykdwUs=";
    dpdfnet8_48khz_hr = model "dpdfnet8_48khz_hr" "sha256-ezr7smCgj+mvPRbjvamSlxvh5+lR0d7nwtI19cQ/VjE=";
  };
  nativeId = if lib.hasInfix "dpdfnet8" defaultModel then 8 else 2;
in
stdenv.mkDerivation {
  pname = "dpdfnet-ladspa";
  version = "0.1.0";

  src = ./.;

  buildInputs = [
    ladspa-sdk
    kissfftFloat
    onnxruntime
    dpdfnet-native
  ];

  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    $CC -O2 -g -Wall -Wextra -std=gnu11 -shared -fPIC \
      -DDPDFNET_MODEL_PATH='"${models.${defaultModel}}"' \
      -DDPDFNET_WEIGHTS_PATH='"${dpdfnet-native}/share/dpdfnet/models/${defaultModel}/weights.f32"' \
      -DDPDFNET_NATIVE_ID=${toString nativeId} \
      -DDPDFNET_LIGHT_MODEL_PATH='"${lib.optionalString (nativeId == 8) models.dpdfnet2_48khz_hr}"' \
      -I${dpdfnet-native}/include \
      -I${lib.getDev onnxruntime}/include -I${lib.getDev onnxruntime}/include/onnxruntime \
      -I${lib.getDev kissfftFloat}/include/kissfft \
      dpdfnet_ladspa.c -o dpdfnet_ladspa.so \
      -L${lib.getLib onnxruntime}/lib -lonnxruntime \
      -L${dpdfnet-native}/lib -ldpdfnet \
      -L${lib.getLib kissfftFloat}/lib -lkissfft-float -lm -lpthread
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 dpdfnet_ladspa.so $out/lib/ladspa/dpdfnet_ladspa.so
    mkdir -p $out/share/dpdfnet/models
    ${lib.concatStringsSep "\n" (
      lib.mapAttrsToList (n: m: "ln -s ${m} $out/share/dpdfnet/models/${n}.onnx") models
    )}
    runHook postInstall
  '';

  passthru = { inherit models; };

  meta = {
    description = "DPDFNet neural speech enhancement as a mono LADSPA plugin";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
  };
}
