#!/usr/bin/env python3
"""Tests for pkgs/studiomic-select, with no PipeWire.

    mic_test.py DIR        DIR is the bench directory (studiomic-bench corpus + mix)

1. Ranking. One voice and one noise are turned into the recordings that
   different microphones would make of the same speech: a headset (close, quiet
   room), a desk microphone, the laptop array, a webcam across the room, a
   call-profile headset (narrow band), a clipped one and a dead one. The scorer
   must exclude the dead one and rank the rest in the order a person would.
2. Switching rule (Chooser): margin, dwell time, a word in progress, a
   microphone that vanishes or dies, and a start with nothing measured.
"""
import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, fftconvolve, resample_poly, sosfilt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pkgs", "studiomic-select"))
import studiomic_select as vms  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
from test_select import switching  # noqa: E402

RNG = np.random.default_rng(7)


def load16(path):
    x, sr = sf.read(path, dtype="float32")
    return resample_poly(x, 1, 3).astype(np.float32) if sr == 48000 else x


def reverb(x, seconds):
    n = int(seconds * vms.SR)
    ir = RNG.standard_normal(n) * np.exp(-6.9 * np.arange(n) / n)  # -60 dB at `seconds`
    ir[0] = 1.0  # direct sound
    ir /= np.sqrt((ir ** 2).sum())
    return fftconvolve(x, ir)[: len(x)].astype(np.float32)


def lowpass(x, hz):
    return sosfilt(butter(6, hz, "low", fs=vms.SR, output="sos"), x).astype(np.float32)


def rms(x):
    return float(np.sqrt(np.mean(x * x)))


def mic(voice, noise, snr, rev=0.0, lp=None, gain=1.0):
    v = reverb(voice, rev) if rev else voice
    n = noise / rms(noise) * rms(v) * 10 ** (-snr / 20)
    y = lowpass(v + n, lp) if lp else v + n
    return np.clip(y * gain, -1, 1).astype(np.float32)


def ranking(d):
    voice = load16(os.path.join(d, "voice.wav"))
    voice = voice / rms(voice) * 0.1  # -20 dBFS
    voice = np.tile(voice, 2)[: int(11 * vms.SR)]
    noise = np.tile(load16(os.path.join(d, "fan.wav")), 2)[: len(voice)]
    cafe = np.tile(load16(os.path.join(d, "cafe.wav")), 2)[: len(voice)]
    mics = {
        "headset (close, fan)": mic(voice, noise, 35),
        "desk mic": mic(voice, noise, 22),
        "laptop array": mic(voice, noise, 14, rev=0.15),
        "webcam across the room": mic(voice, cafe, 8, rev=0.35, lp=6500),
        "call-profile headset (3.4 kHz)": mic(voice, noise, 28, lp=3400),
        "desk mic, clipped": mic(voice, noise, 25, gain=40),
        "dead (zeros)": np.zeros(len(voice), np.float32),
        # an input that hears nothing: only its own electrical noise, -82 dBFS
        "deaf (noise floor only)": (RNG.standard_normal(len(voice)) * 10 ** (-82 / 20)).astype(np.float32),
    }
    dn = vms.Dnsmos(os.path.join(d, "sig_bak_ovr.onnx"))
    res = {}
    for name, x in mics.items():
        st = vms.analyze(x)
        mos = dn(x) if st["alive"] else None
        res[name] = (st, mos, vms.score(st, mos))
    print(f"{'recording':34} {'alive':>5} {'snr':>5} {'hf dB':>6} {'clip':>6} {'SIG':>5} {'BAK':>5} {'OVR':>5} {'score':>6}")
    for name, (st, mos, sc) in sorted(res.items(), key=lambda kv: -(kv[1][2] if kv[1][2] is not None else -9)):
        m = mos or (float("nan"),) * 3
        print(f"{name:34} {str(st['alive']):>5} {st.get('snr_db', 0):>5} {str(st.get('hf_db', '')):>6} "
              f"{st.get('clip', 0):>6.3f} {m[0]:>5.2f} {m[1]:>5.2f} {m[2]:>5.2f} "
              f"{sc if sc is not None else '-':>6}")
    sc = {k: v[2] for k, v in res.items()}
    spoken, deaf = vms.hearing({k: dict(v[0], short=False) for k, v in res.items()})
    # The reason hearing() exists: DNSMOS alone rates the deaf one as a good microphone.
    print(f"\nsomebody heard the speech: {spoken}; deaf: {sorted(deaf)}")
    checks = [
        ("the dead microphone has no score", sc["dead (zeros)"] is None),
        ("DNSMOS alone would rate the deaf input above the webcam and the laptop array",
         sc["deaf (noise floor only)"] > sc["webcam across the room"]),
        ("hearing() marks the deaf input deaf, and only it (the webcam and laptop array did hear)",
         spoken and deaf == {"deaf (noise floor only)"}),
        ("the headset is the best", sc["headset (close, fan)"] == max(v for v in sc.values() if v is not None)),
        ("headset > desk", sc["headset (close, fan)"] > sc["desk mic"]),
        ("desk > laptop array", sc["desk mic"] > sc["laptop array"]),
        ("laptop array > webcam", sc["laptop array"] > sc["webcam across the room"]),
        ("clipped < the same desk mic unclipped", sc["desk mic, clipped"] < sc["desk mic"]),
        ("call-profile headset < headset", sc["call-profile headset (3.4 kHz)"] < sc["headset (close, fan)"]),
    ]
    return checks


def main(d):
    results = ranking(d) + switching()
    print()
    bad = 0
    for text, passed in results:
        print(("PASS " if passed else "FAIL ") + text)
        bad += not passed
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(sys.argv[1])
