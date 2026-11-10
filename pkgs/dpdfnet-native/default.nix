{
  lib,
  stdenv,
  fetchFromGitHub,
  cmake,
}:

# The DPDFNet authors' optimized C runtime (Ceva, Apache-2.0): the two 48 kHz
# models as W7A8 (7-bit weights, 8-bit activations) integer kernels, AVX2 with
# AVX-VNNI variants chosen at run time. It runs one 10 ms spectral hop per call
# with no allocation, on state the caller owns, so it is safe to call from the
# PipeWire real-time thread. On this project's recording it matches ONNX Runtime
# on the quality model (docs/design.md) at about a fifth of the CPU.
#
# Upstream: ceva-ip/DPDFNet, directory optimized_inference/. HushMic vendors the
# same files unmodified. $out/share/dpdfnet/models/<model>/weights.f32 holds the
# packed weights, checked against the SHA-256 in each manifest.json at load.

let
  rev = "afa8292f3b8083c3daadd06dbcfe4ff877dd3172";
in
stdenv.mkDerivation (finalAttrs: {
  pname = "dpdfnet-native";
  version = "0-unstable-${lib.substring 0 7 rev}";

  src = fetchFromGitHub {
    owner = "ceva-ip";
    repo = "DPDFNet";
    inherit rev;
    hash = "sha256-qNrxaubVT6oG+BRsvi/ImJPj5O2JeDvCAZCzhj/c1Qo=";
  };
  sourceRoot = "${finalAttrs.src.name}/optimized_inference";

  nativeBuildInputs = [ cmake ];
  cmakeFlags = [ (lib.cmakeBool "BUILD_SHARED_LIBS" true) ];
  # Only the library: the benchmarks, tools and tests are not installed.
  cmakeBuildFlags = [
    "--target"
    "dpdfnet"
  ];

  meta = {
    description = "DPDFNet optimized W7A8 C runtime (Ceva)";
    homepage = "https://github.com/ceva-ip/DPDFNet";
    license = lib.licenses.asl20;
    platforms = [ "x86_64-linux" ];
  };
})
