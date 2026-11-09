#!/usr/bin/env python3
"""Live test of studiomic-select in the running PipeWire, with fake microphones.

    mic_live_test.py DIR [SECONDS]

Starts three fake microphones (PipeWire streams with media.class Audio/Source
and priority.session far below any real device, so none becomes the default
source) and a stand-in for the voice filter's input, then runs the daemon on
them with a short dwell time and prints which microphone is linked, second by
second:

    fake_deaf   only its own noise, -82 dBFS (an input that hears nothing, like
                a speaker's unused microphone): best ladder rank, and DNSMOS
                alone rates it highly
    fake_dead   zeros (a muted or dead source)       second
    fake_far    the voice across a noisy room        second
    fake_near   the voice close to the microphone    last; muted at 65 s

The daemon links the ladder's choice at once (the dead one, which has the best
rank), must replace it within seconds, once it has audio to judge, must move to
fake_near after measuring the other two, and must leave fake_near within 8 s of
its mute. Everything this starts is stopped on exit. No
real microphone is opened and nothing reaches the speakers.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pkgs", "studiomic-select"))
import studiomic_select as vms  # noqa: E402

SR = 48000
MUTE_AT = 65  # seconds after the start: fake_near is muted (late, so a loaded machine still switches first)


def rms(x):
    return float(np.sqrt(np.mean(x * x)))


def build(d, tmp, seconds):
    voice, _ = sf.read(os.path.join(d, "voice.wav"), dtype="float32")
    voice = voice / rms(voice) * 0.1
    # a phrase, then a pause of 1.2 s, over and over: the daemon switches only
    # between phrases, so a speaker who never pauses would keep it where it is
    voice = np.concatenate([voice, np.zeros(int(1.2 * SR), np.float32)])
    n = int((seconds + 15) * SR)
    voice = np.tile(voice, int(np.ceil(n / len(voice))))[:n]

    def noise(name):
        x, _ = sf.read(os.path.join(d, name), dtype="float32")
        return np.tile(x, int(np.ceil(n / len(x))))[:n]

    ir = np.random.default_rng(3).standard_normal(int(0.3 * SR)) * np.exp(-6.9 * np.arange(int(0.3 * SR)) / (0.3 * SR))
    ir[0] = 1.0
    ir /= np.sqrt((ir ** 2).sum())
    far = fftconvolve(voice, ir)[:n].astype(np.float32)
    cafe = noise("cafe.wav")
    far = far + cafe / rms(cafe) * rms(far) * 10 ** (-6 / 20)
    fan = noise("fan.wav")
    near = voice + fan / rms(fan) * rms(voice) * 10 ** (-30 / 20)
    near[int(MUTE_AT * SR):] = 0.0  # somebody hits the mute key on the best microphone
    files = {}
    deaf = (np.random.default_rng(5).standard_normal(n) * 10 ** (-82 / 20)).astype(np.float32)
    for name, x in (("fake_dead", np.zeros(n, np.float32)), ("fake_deaf", deaf),
                    ("fake_far", far), ("fake_near", near)):
        files[name] = os.path.join(tmp, name + ".wav")
        sf.write(files[name], np.clip(x, -1, 1).astype(np.float32), SR, subtype="FLOAT")
    return files


def linked_source(sink):
    """Name of the node whose output is linked to the node called `sink`."""
    objs = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True).stdout)
    names = {o["id"]: o["info"]["props"].get("node.name") for o in objs
             if o.get("type") == "PipeWire:Interface:Node" and "info" in o}
    me = next((i for i, n in names.items() if n == sink), None)
    return sorted({names.get(o["info"].get("output-node-id")) for o in objs
                   if o.get("type") == "PipeWire:Interface:Link" and o["info"].get("input-node-id") == me
                   and me is not None}, key=str)


def main(d, seconds=85):
    procs = []
    tmp = tempfile.mkdtemp(prefix="mic-live.")
    try:
        files = build(d, tmp, seconds)
        prio = {"fake_deaf": 30, "fake_dead": 25, "fake_far": 20, "fake_near": 10}
        for name, f in files.items():
            procs.append(subprocess.Popen(
                ["pw-cat", "-p", "-P", "{ node.name=%s media.class=Audio/Source node.autoconnect=false "
                 "priority.session=%d priority.driver=0 }" % (name, prio[name]), f]))
        procs.append(subprocess.Popen(
            ["pw-cat", "-r", "--rate", "48000", "--channels", "1", "--format", "f32", "-P",
             "{ node.name=selftest.capture node.autoconnect=false }", os.path.join(tmp, "out.wav")]))
        daemon = subprocess.Popen(
            [sys.executable, "-I", os.path.join(os.path.dirname(vms.__file__), "studiomic_select.py"), "run",
             "--model", os.path.join(d, "sig_bak_ovr.onnx"), "--pattern", "^fake_",
             "--chain", "selftest.capture", "--dwell", "10", "--interval", "3",
             "--status", os.path.join(tmp, "status.json")] + (["--debug"] if os.environ.get("MIC_DEBUG") else []))
        procs.append(daemon)
        t0 = time.monotonic()
        last = None
        timeline = []
        while time.monotonic() - t0 < seconds:
            cur = linked_source("selftest.capture")
            if cur != last:
                timeline.append((round(time.monotonic() - t0), cur))
                print(f"t={timeline[-1][0]:3d}s  linked: {cur}", flush=True)
                last = cur
            time.sleep(1)
        print(open(os.path.join(tmp, "status.json")).read() if os.path.exists(os.path.join(tmp, "status.json")) else "no status")
        seq = [c[0] if c else None for _, c in timeline]
        dead_until = max((t for t, c in timeline if c and "fake_dead" in c), default=0)
        next_after_dead = next((t for t, c in timeline if t > dead_until and c), None)
        deaf_times = [t for t, c in timeline if c and "fake_deaf" in c]
        checks = [
            ("the ladder's first choice, the deaf microphone, is linked at once",
             bool(deaf_times) and min(deaf_times) <= 5),
            ("the deaf microphone is left within 25 s and never linked again",
             bool(deaf_times) and max(deaf_times) < 25 and seq.count("fake_deaf") == 1),
            ("the dead microphone is never linked", "fake_dead" not in seq),
            ("it moved to the live, better-ranked one (fake_far) first", "fake_far" in seq),
            ("it moved to the measured best (fake_near) before the mute",
             any(c and c[0] == "fake_near" and t < MUTE_AT for t, c in timeline)),
            ("it left the muted microphone within 8 s, for the next best",
             bool(seq) and seq[-1] == "fake_far"
             and next((t for t, c in timeline if t > MUTE_AT and c and c[0] == "fake_far"), 999) <= MUTE_AT + 8),
            ("the chain input never had two microphones at once",
             all(len(c or []) <= 1 for _, c in timeline)),
        ]
        print()
        bad = 0
        for text, ok in checks:
            print(("PASS " if ok else "FAIL ") + text)
            bad += not ok
        return bad
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        subprocess.run(["rm", "-rf", tmp])


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 85) else 0)
