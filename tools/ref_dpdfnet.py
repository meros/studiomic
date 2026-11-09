#!/usr/bin/env python3
"""Independent reference for pkgs/dpdfnet-ladspa: DPDFNet streaming in numpy.

Same algorithm as ceva-ip/DPDFNet package/src/dpdfnet/stream.py (causal STFT,
Vorbis window, state from the model metadata), written without librosa. The
bench runs it next to the C plugin; the two must score the same.

    ref_dpdfnet.py MODEL.onnx IN.wav OUT.wav
"""
import sys

import numpy as np
import onnxruntime as ort
import soundfile as sf


def main(model, src, dst):
    so = ort.SessionOptions()
    so.intra_op_num_threads = so.inter_op_num_threads = 1
    s = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
    m = s.get_modelmeta().custom_metadata_map
    win = int(m["n_fft"])
    hop = win // 2
    state = np.zeros(int(m["state_size"]), np.float32)
    e = int(m["erb_norm_state_size"])
    c = int(m["spec_norm_state_size"])
    state[:e] = [float(v) for v in m["erb_norm_init"].split(",")]
    state[e:e + c] = [float(v) for v in m["spec_norm_init"].split(",")]
    i = np.arange(win)
    t = np.sin(0.5 * np.pi * (i + 0.5) / (win / 2))
    w = np.sin(0.5 * np.pi * t * t).astype(np.float32)

    x, sr = sf.read(src, dtype="float32")
    assert sr == int(m["sample_rate"]) and x.ndim == 1
    x = np.concatenate([x, np.zeros(win, np.float32)])  # flush the last window
    ola = np.zeros(win, np.float32)
    out = []
    for k in range(0, len(x) - win + 1, hop):
        spec = np.fft.rfft(x[k:k + win] * w)
        ri = np.stack([spec.real, spec.imag], -1).astype(np.float32)[None, None]
        enh, state = s.run(None, {s.get_inputs()[0].name: ri, s.get_inputs()[1].name: state})
        f = enh[0, 0]
        ola += (np.fft.irfft(f[:, 0] + 1j * f[:, 1], n=win) * w).astype(np.float32)
        out.append(ola[:hop].copy())
        ola = np.concatenate([ola[hop:], np.zeros(hop, np.float32)])
    y = np.concatenate(out)
    # The model lags its input by 4 hops; put the same silence in front as the
    # plugin does so the bench removes the delay the same way.
    y = np.concatenate([np.zeros(win, np.float32), y])[: len(x) - win]
    sf.write(dst, y, sr, subtype="FLOAT")


if __name__ == "__main__":
    main(*sys.argv[1:4])
