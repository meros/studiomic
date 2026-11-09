#!/usr/bin/env python3
"""studiomic-bench: score microphone filters on speech mixed with keyboard noise.

The question it answers: does a filter remove keystrokes, fan hum and chatter
without hurting the voice? Every number comes from a mix we build ourselves, so
the clean voice and the noise are known exactly.

    studiomic-bench corpus   DIR            download the public sources, cut clips
    studiomic-bench mix      DIR --snr 3    mix voice and noise into noisy clips
    studiomic-bench run      DIR NAME CMD   run a filter on every noisy clip, score it
    studiomic-bench levels   DIR NAME CMD   the same clip at three input levels
    studiomic-bench report   DIR            table of every scored run

A filter is a command that reads a mono 48 kHz float32 WAV and writes one:
    CMD = "sh -c 'my-filter {in} {out}'"      ({in} and {out} are filled in)
Built-in names: none, ladspa:<lib.so>:<label>[:controls] (needs applyplugin from
ladspa-sdk), chain:<studiomic.conf> (the generated service config, run offline
by chain_host.py: deterministic, the same plugins and settings as the service).

Metrics (all on the 48 kHz output after the filter's own delay is removed):
  pause_db    level change in the speech pauses. More negative removes more noise.
  speech_db   level change while the voice speaks. Near 0 means the voice is kept.
  sisdr       scale-invariant SDR against the clean voice, dB. Higher is better.
  SIG/BAK/OVR DNSMOS P.835 (Microsoft), a learned estimate of speech, noise
              and overall quality on a 1..5 scale. An estimate, not a listening
              test, and it can reward a filter that over-suppresses.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

import numpy as np
import onnxruntime as ort
import soundfile as sf
from scipy.signal import fftconvolve, resample_poly

SR = 48000

# Public sources, same ones as the HushMic demo so the numbers are comparable
# in spirit. Licences: voice public domain, keyboard CC BY 4.0, fan CC BY 4.0,
# cafe public domain.
SOURCES = {
    "voice": "https://archive.org/download/spc266_2508_librivox/spc266_afterlove_pac_128kb.mp3",
    "keys": "https://upload.wikimedia.org/wikipedia/commons/2/27/Typing_on_Keychron_V1_Ultra_%28Red_Linear_Switch%29.wav",
    "fan": "https://upload.wikimedia.org/wikipedia/commons/9/99/Air_conditioner_hum_%28Gravity_Sound%29.wav",
    "cafe": "https://upload.wikimedia.org/wikipedia/commons/b/b5/Restaurant_ambience.ogg",
}
DNSMOS_URL = "https://raw.githubusercontent.com/microsoft/DNS-Challenge/master/DNSMOS/DNSMOS/sig_bak_ovr.onnx"
VOICE_SPAN = (17.55, 10.75)  # seconds: start, length (three phrases, two pauses)


def fetch(url, dst):
    # Wikimedia refuses the default Python user agent.
    req = urllib.request.Request(url, headers={"User-Agent": "studiomic-bench/0.1 (audio filter benchmark)"})
    with urllib.request.urlopen(req) as r, open(dst, "wb") as f:
        f.write(r.read())


def ffmpeg_to_48k(src, dst, start=None, length=None):
    cmd = ["ffmpeg", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", src]
    if length is not None:
        cmd += ["-t", str(length)]
    cmd += ["-ac", "1", "-ar", str(SR), "-c:a", "pcm_f32le", dst]
    subprocess.run(cmd, check=True)


def cmd_corpus(a):
    os.makedirs(a.dir, exist_ok=True)
    for name, url in SOURCES.items():
        raw = os.path.join(a.dir, f"raw_{name}" + os.path.splitext(url)[1])
        if not os.path.exists(raw):
            print("fetch", name)
            fetch(url, raw)
        span = VOICE_SPAN if name == "voice" else (None, None)
        ffmpeg_to_48k(raw, os.path.join(a.dir, f"{name}.wav"), *span)
    mos = os.path.join(a.dir, "sig_bak_ovr.onnx")
    if not os.path.exists(mos):
        fetch(DNSMOS_URL, mos)
    print("corpus ready in", a.dir)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x)) + 1e-20))


def db(x):
    return 20 * np.log10(x + 1e-12)


def load(path):
    x, sr = sf.read(path, dtype="float32")
    assert sr == SR and x.ndim == 1, f"{path}: need mono {SR} Hz"
    return x


def loop_to(x, n):
    return np.tile(x, int(np.ceil(n / len(x))))[:n]


def speech_mask(clean, frame=960):
    """True per frame where the voice speaks: within 40 dB of the loudest frame."""
    n = len(clean) // frame
    e = np.array([rms(clean[i * frame:(i + 1) * frame]) for i in range(n)])
    return e > e.max() * 10 ** (-40 / 20)


def cmd_mix(a):
    voice = load(os.path.join(a.dir, "voice.wav"))
    voice = voice / rms(voice) * 10 ** (-20 / 20)  # -20 dBFS RMS, like the demo
    out = os.path.join(a.dir, "mix")
    os.makedirs(out, exist_ok=True)
    for name in ("keys", "fan", "cafe"):
        noise = loop_to(load(os.path.join(a.dir, f"{name}.wav")), len(voice))
        noise = noise / rms(noise) * rms(voice) * 10 ** (-a.snr / 20)
        sf.write(os.path.join(out, f"{name}.noisy.wav"), voice + noise, SR, subtype="FLOAT")
        sf.write(os.path.join(out, f"{name}.noise.wav"), noise, SR, subtype="FLOAT")
    sf.write(os.path.join(out, "clean.wav"), voice, SR, subtype="FLOAT")
    json.dump({"snr": a.snr}, open(os.path.join(out, "mix.json"), "w"))
    print("mixed at", a.snr, "dB SNR into", out)


def envelope(x, frame=240):
    n = len(x) // frame
    return np.sqrt(np.mean(np.square(x[: n * frame].reshape(n, frame)), axis=1) + 1e-12)


def delay_of(ref, test, maxlag=96000):
    """Delay of `test` behind `ref`, in samples.

    Two steps, because a live test has jitter and clock drift of some
    milliseconds: the level envelope (5 ms frames) finds the delay to within
    5 ms and cannot lock onto the wrong waveform peak; the waveform then refines
    it within one frame."""
    coarse = delay_of_raw(envelope(ref), envelope(test), maxlag // 240) * 240
    lo = max(coarse - 240, -maxlag)
    n = min(len(ref), len(test))
    c = fftconvolve(test[:n], ref[:n][::-1], mode="full")
    mid = n - 1
    seg = c[mid + lo: mid + coarse + 241]
    return lo + int(np.argmax(seg)) if len(seg) else coarse


def delay_of_raw(ref, test, maxlag):
    """Samples by which `test` lags `ref`, from the cross-correlation peak."""
    n = min(len(ref), len(test))
    a = ref[:n] - ref[:n].mean()
    b = test[:n] - test[:n].mean()
    c = fftconvolve(b, a[::-1], mode="full")
    mid = n - 1
    seg = c[max(mid - maxlag // 4, 0): mid + maxlag + 1]
    return int(np.argmax(seg)) - min(maxlag // 4, mid)


def drift_ms(ref, out, lag):
    """How much the delay changes from the first half of the clip to the second.

    A filter with a fixed delay gives 0. Clock drift between the test streams
    gives a few ms per clip, which ruins sample-exact scores (SI-SDR) without
    any audible effect, so a non-zero value means: trust DNSMOS, not SI-SDR."""
    n = min(len(ref), len(out) - max(lag, 0)) if lag >= 0 else min(len(ref), len(out))
    h = n // 2
    a = out[lag:lag + h] if lag >= 0 else out[:h]
    b = out[lag + h:lag + n] if lag >= 0 else out[h:n]
    da = delay_of(ref[:len(a)], a, maxlag=2400)
    db_ = delay_of(ref[h:h + len(b)], b, maxlag=2400)
    return 1000.0 * (db_ - da) / SR


def sisdr(ref, est):
    ref = ref - ref.mean()
    est = est - est.mean()
    s = (est @ ref) / (ref @ ref + 1e-12) * ref
    return float(10 * np.log10((s @ s + 1e-12) / ((est - s) @ (est - s) + 1e-12)))


class Dnsmos:
    # polyfit coefficients from Microsoft's dnsmos_local.py (non-personalised)
    P_SIG = np.poly1d([-0.08397278, 1.22083953, 0.0052439])
    P_BAK = np.poly1d([-0.13166888, 1.60915514, -0.39604546])
    P_OVR = np.poly1d([-0.06766283, 1.11546468, 0.04602535])

    def __init__(self, path):
        self.s = ort.InferenceSession(path)

    def __call__(self, x48):
        x = resample_poly(x48, 1, 3).astype(np.float32)  # 48k -> 16k
        seg = int(9.01 * 16000)
        while len(x) < seg:
            x = np.concatenate([x, x])
        hops = int(np.floor(len(x) / 16000 - 9.01)) + 1
        r = []
        for i in range(hops):
            w = x[i * 16000: i * 16000 + seg]
            if len(w) == seg:
                r.append(self.s.run(None, {"input_1": w[None]})[0][0])
        sig, bak, ovr = np.mean(r, axis=0)
        return float(self.P_SIG(sig)), float(self.P_BAK(bak)), float(self.P_OVR(ovr))


def apply_filter(spec, src, dst):
    if spec == "none":
        sf.write(dst, load(src), SR, subtype="FLOAT")
        return
    if spec.startswith("chain:"):
        # the generated studiomic config, run offline by chain_host.py
        host = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chain_host.py")
        subprocess.run([sys.executable, "-I", host, spec[len("chain:"):], src, dst], check=True)
        return
    if spec.startswith("ladspa:"):
        _, lib, label, *params = spec.split(":", 3)
        # applyplugin reads 16-bit or float WAV; ours are float.
        cmd = ["applyplugin", src, dst, lib, label] + (params[0].split() if params else [])
    else:
        cmd = spec.replace("{in}", src).replace("{out}", dst)
        cmd = ["sh", "-c", cmd]
    subprocess.run(cmd, check=True)


def cmd_run(a):
    mix = os.path.join(a.dir, "mix")
    clean = load(os.path.join(mix, "clean.wav"))
    fm = speech_mask(clean)
    # Level metrics ignore 100 ms either side of every speech edge: a few ms of
    # jitter in a live test must not move speech energy into a pause.
    k = np.ones(11)
    core_speech = fm & (np.convolve(fm.astype(float), k, "same") > 10.5)
    core_pause = ~fm & (np.convolve((~fm).astype(float), k, "same") > 10.5)
    expand = lambda m: np.pad(np.repeat(m, 960), (0, len(clean) - len(m) * 960))
    mask_s, mask_p = expand(core_speech).astype(bool), expand(core_pause).astype(bool)
    dn = Dnsmos(os.path.join(a.dir, "sig_bak_ovr.onnx"))
    rows = []
    for noise in ("keys", "fan", "cafe"):
        noisy = load(os.path.join(mix, f"{noise}.noisy.wav"))
        with tempfile.TemporaryDirectory() as t:
            dst = os.path.join(t, "out.wav")
            apply_filter(a.cmd, os.path.join(mix, f"{noise}.noisy.wav"), dst)
            out = load(dst)
        # align against the clean voice: it is what the output must contain
        lag = delay_of(clean, out) if a.cmd != "none" else 0
        drift = drift_ms(clean, out, lag) if a.cmd != "none" else 0.0
        # remove the filter's own delay; a negative lag means the recording
        # started late, so the missing start becomes silence
        out = out[lag:] if lag >= 0 else np.concatenate([np.zeros(-lag, np.float32), out])
        n = min(len(out), len(noisy), len(clean))
        out, nz, cl = out[:n], noisy[:n], clean[:n]
        ms, mp = mask_s[:n], mask_p[:n]
        sig, bak, ovr = dn(out)
        rows.append({
            "name": a.name, "noise": noise, "lag_ms": round(1000 * lag / SR, 1),
            "drift_ms": round(drift, 1),
            "pause_db": round(db(rms(out[mp])) - db(rms(nz[mp])), 1),
            "speech_db": round(db(rms(out[ms])) - db(rms(nz[ms])), 1),
            "sisdr": round(sisdr(cl, out), 1),
            "SIG": round(sig, 2), "BAK": round(bak, 2), "OVR": round(ovr, 2),
        })
    path = os.path.join(a.dir, "results.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)
    print_rows(rows)


def cmd_levels(a):
    """Feed one clip at three input levels. A leveler gives the same output
    level for all three; a limiter keeps the peak under -1 dBFS. Needs no
    alignment, so it is the right check for a live graph."""
    noisy = load(os.path.join(a.dir, "mix", f"{a.noise}.noisy.wav"))
    rows = []
    for target in (-45.0, -35.0, -25.0):
        x = noisy / rms(noisy) * 10 ** (target / 20)
        with tempfile.TemporaryDirectory() as t:
            src, dst = os.path.join(t, "in.wav"), os.path.join(t, "out.wav")
            sf.write(src, x.astype(np.float32), SR, subtype="FLOAT")
            apply_filter(a.cmd, src, dst)
            y = load(dst)
        w = 4800  # 100 ms
        n = len(y) // w
        lv = db(np.array([rms(y[i * w:(i + 1) * w]) for i in range(n)]))
        lv = lv[lv > -90]  # drop digital silence around the recording
        rows.append({
            "name": a.name, "input_rms_db": target,
            "speech_level_db": round(float(np.percentile(lv, 95)), 1),  # loud 100 ms blocks
            "pause_level_db": round(float(np.percentile(lv, 10)), 1),
            "peak_db": round(float(db(np.abs(y).max())), 1),
        })
    cols = list(rows[0])
    print("  ".join(f"{c:>16}" for c in cols))
    for r in rows:
        print("  ".join(f"{str(r[c]):>16}" for c in cols))


def print_rows(rows):
    cols = ["name", "noise", "lag_ms", "drift_ms", "pause_db", "speech_db", "sisdr", "SIG", "BAK", "OVR"]
    print("  ".join(f"{c:>10}" for c in cols))
    for r in rows:
        print("  ".join(f"{str(r[c]):>10}" for c in cols))


def cmd_report(a):
    rows = list(csv.DictReader(open(os.path.join(a.dir, "results.csv"))))
    print_rows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="c", required=True)
    for name, fn in (("corpus", cmd_corpus), ("mix", cmd_mix), ("run", cmd_run),
                     ("levels", cmd_levels), ("report", cmd_report)):
        q = s.add_parser(name)
        q.add_argument("dir")
        if name == "mix":
            q.add_argument("--snr", type=float, default=3.0)
        if name in ("run", "levels"):
            q.add_argument("name")
            q.add_argument("cmd")
        if name == "levels":
            q.add_argument("--noise", default="keys")
        q.set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
