#!/usr/bin/env python3
"""studiomic-select: choose the best microphone by measurement, switch live.

Every candidate microphone is opened at the same time, so all of them hear the
same speech. Each one is scored on what it recorded:

  * liveness   digital silence (a muted or dead source) is never chosen;
  * hearing    a microphone that heard none of the speech another one heard
               (a speaker's unused input, gain at zero) is deaf and not chosen:
               DNSMOS would otherwise rate its near-silence as good;
  * quality    DNSMOS P.835 overall score (Microsoft, 1 to 5) of the last 9 s,
               a learned estimate of how good the speech sounds, which falls
               with noise, reverb, distant pickup, clipping and narrow bandwidth;
  * guard      clipping takes points off: the damage is done before any filter.

The best microphone is linked to the input of the voice filter. The link is
moved only when another microphone is better by a clear margin, has been
measured more than once, and the current one is not in the middle of a word.
Scores only change while somebody speaks; in a quiet room the choice stays.

    studiomic-select run     keep the voice filter's input on the best microphone
    studiomic-select once    measure for a while, print the table, change nothing
    studiomic-select status  print what the running daemon knows

Privacy: audio stays in memory (9 s per microphone) and is never written.
Side effect: every candidate microphone is open while this runs, so a desktop
may show the microphone as in use. A Bluetooth headset only counts when it
already has a microphone node, which exists only in its call profile, so
probing never switches a headset out of music mode.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import numpy as np

SR = 16000
FRAME = 320  # 20 ms
WINDOW_S = 9.01  # the input length DNSMOS wants
PROBE_BUFFER = int(10 * SR)


# ---------------------------------------------------------------- scoring


class Dnsmos:
    """DNSMOS P.835 (Microsoft DNS-Challenge, sig_bak_ovr.onnx), 16 kHz input.

    The polynomial maps the network's raw outputs onto the human-rated scale;
    the coefficients are the non-personalised ones from dnsmos_local.py."""

    P_SIG = np.poly1d([-0.08397278, 1.22083953, 0.0052439])
    P_BAK = np.poly1d([-0.13166888, 1.60915514, -0.39604546])
    P_OVR = np.poly1d([-0.06766283, 1.11546468, 0.04602535])
    LENGTH = int(WINDOW_S * SR)

    def __init__(self, path):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])

    def __call__(self, x):
        """(SIG, BAK, OVR) of the last 9.01 s of x (float32, 16 kHz)."""
        x = np.asarray(x, np.float32)[-self.LENGTH:]
        if len(x) < self.LENGTH:
            x = np.concatenate([x, np.zeros(self.LENGTH - len(x), np.float32)])
        sig, bak, ovr = self.session.run(None, {"input_1": x[None]})[0][0]
        return float(self.P_SIG(sig)), float(self.P_BAK(bak)), float(self.P_OVR(ovr))


def frame_db(x):
    n = len(x) // FRAME
    f = x[: n * FRAME].reshape(n, FRAME)
    return 20 * np.log10(np.sqrt(np.mean(f * f, axis=1)) + 1e-9)


def is_alive(x):
    """A muted, unplugged or dead source delivers exact zeros, or one bit of
    flicker; a live microphone has more noise than that, even in a quiet room."""
    return bool(len(x) and np.abs(x).max() > 1.5 / 32768)


def analyze(x):
    """What one microphone recorded in a window, without the quality model."""
    x = np.asarray(x, np.float32)
    db = frame_db(x)
    if len(db) == 0:
        return {"alive": False}
    floor, top = float(np.percentile(db, 10)), float(np.percentile(db, 95))
    speech = (db > floor + 12) & (db > -55)
    # energy of the speech above 4 kHz against 0.3 to 3.4 kHz: a wide-band
    # microphone keeps some, a call-profile headset keeps almost none
    hf_db = None
    if speech.sum() >= 5:
        n = len(db) * FRAME
        frames = x[:n].reshape(-1, FRAME)[speech]
        spec = np.mean(np.abs(np.fft.rfft(frames * np.hanning(FRAME), axis=1)) ** 2, axis=0)
        hz = np.fft.rfftfreq(FRAME, 1 / SR)
        lo = spec[(hz >= 300) & (hz <= 3400)].sum()
        hi = spec[(hz >= 4000) & (hz <= 7500)].sum()
        hf_db = round(float(10 * np.log10((hi + 1e-20) / (lo + 1e-20))), 1)
    return {
        "alive": is_alive(x),
        "floor_db": round(floor, 1),
        "top_db": round(top, 1),
        "snr_db": round(top - floor, 1),
        "speech": float(speech.mean()),
        "clip": float(np.mean(np.abs(x) > 0.985)),
        "hf_db": hf_db,
    }


def score(stats, mos):
    """One number, higher is better. mos is (SIG, BAK, OVR) or None."""
    if not stats.get("alive") or mos is None:
        return None
    s = mos[2]
    if stats["clip"] > 0.005:
        s -= 0.5  # clipped: the damage is done before any filter
    return round(s, 3)


SPEECH_MIN = 0.05   # share of a window that must be speech for it to count
HEARD_SNR = 12.0    # a microphone this far above its floor heard the speech
DEAF_SNR = 6.0      # one below this, while another heard it, heard nothing


def hearing(stats):
    """Who heard the speech in this round: (somebody did, the deaf ones).

    DNSMOS rates a recording of near silence as good: nothing noisy, nothing
    distorted. A microphone that did not pick up the voice (a speaker's unused
    microphone input, a gain at zero, a jack with nothing in it) would therefore
    outscore every microphone that did. So a microphone is judged only against
    speech that another one heard, and one that heard nothing of it is deaf."""
    live = {n: st for n, st in stats.items() if st.get("alive") and not st.get("short")}
    ref = max((st["snr_db"] for st in live.values() if st.get("speech", 0) >= SPEECH_MIN),
              default=0.0)
    if ref < HEARD_SNR:
        return False, set()
    return True, {n for n, st in live.items() if st["snr_db"] < DEAF_SNR}


# ---------------------------------------------------------------- PipeWire


def run(cmd, timeout=5):
    """Run a PipeWire tool; a stalled PipeWire must not stall the daemon."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def dump():
    """The graph as a list of objects, or None when pw-dump failed."""
    r = run(["pw-dump"], timeout=10)
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def candidates(objs, pattern):
    """Audio/Source nodes whose name matches, with the ladder priority."""
    rx = re.compile(pattern)
    out = {}
    for o in objs:
        p = o.get("info", {}).get("props", {})
        if o.get("type") == "PipeWire:Interface:Node" and p.get("media.class") == "Audio/Source" \
                and rx.search(p.get("node.name", "")):
            out[p["node.name"]] = {
                "id": o["id"],
                "priority": p.get("priority.session", 0),
                "description": p.get("node.description", p["node.name"]),
            }
    return out


class Probe(threading.Thread):
    """Reads one microphone at 16 kHz mono through pw-cat into a ring buffer."""

    def __init__(self, name):
        super().__init__(daemon=True)
        self.name_ = name
        self.ring = np.zeros(PROBE_BUFFER, np.float32)
        self.filled = 0  # samples in the ring, capped at the ring size
        self.total = 0   # samples received in all
        self.lock = threading.Lock()
        self.proc = None
        self.stop_flag = False
        self.last_data = 0.0

    def run(self):
        cmd = ["pw-cat", "-r", "--raw", "--target", self.name_, "--rate", str(SR),
               "--channels", "1", "--format", "s16",
               "-P", "{ node.name=studiomic-probe media.role=Production node.passive=false "
               "node.dont-fallback=true node.dont-reconnect=true }", "-"]
        while not self.stop_flag:
            with self.lock:  # a restarted stream is not joined to the old audio
                self.filled = 0
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            while not self.stop_flag:
                b = self.proc.stdout.read(3200)
                if not b:
                    break
                x = np.frombuffer(b[: len(b) // 2 * 2], np.int16).astype(np.float32) / 32768.0
                with self.lock:
                    self.ring = np.concatenate([self.ring[len(x):], x])
                    self.filled = min(self.filled + len(x), PROBE_BUFFER)
                    self.total += len(x)
                self.last_data = time.monotonic()
            if self.proc.poll() is None:
                self.proc.kill()
            if not self.stop_flag:
                time.sleep(2)  # the source went away or restarted

    def stop(self):
        self.stop_flag = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def window(self, seconds):
        with self.lock:
            n = min(int(seconds * SR), self.filled)
            return self.ring[-n:].copy() if n else np.zeros(0, np.float32)

    def fresh(self):
        return time.monotonic() - self.last_data < 2.0


# ---------------------------------------------------------------- choosing


class Chooser:
    """Scores and the switching rule, with no PipeWire in it, so it can be tested."""

    MARGIN = 0.25      # how much better a microphone must be, in DNSMOS points
    MIN_DWELL = 60.0   # seconds between two switches
    MIN_EVALS = 2      # evaluations before a microphone may win
    ALPHA = 0.5        # weight of the newest evaluation in the running score

    def __init__(self, dwell=MIN_DWELL):
        self.dwell = dwell
        self.score = {}    # name -> running score
        self.evals = {}    # name -> number of evaluations
        self.alive = {}    # name -> bool, from the latest window
        self.judged = {}   # name -> True once it has had enough audio to be judged
        self.priority = {}
        self.current = None
        self.last_switch = -1e9

    def update(self, name, s):
        """Add one evaluation. The caller passes windows that do not overlap,
        so one sentence never counts twice."""
        if s is None:
            return
        old = self.score.get(name)
        self.score[name] = s if old is None else round(self.ALPHA * s + (1 - self.ALPHA) * old, 3)
        self.evals[name] = self.evals.get(name, 0) + 1

    def initial(self, now, names):
        """Before any audio is judged: the ladder's choice, so the filter has
        an input at once. A muted one is replaced as soon as it shows."""
        if self.current is None and names:
            return self._take(max(names, key=lambda n: self.priority.get(n, 0)), now)
        return self.current

    def decide(self, now, names, speaking_now):
        """The name that should be linked, or None to keep the current link."""
        live = [n for n in names if self.alive.get(n, True)]
        if not live:
            return self.current
        if self.current not in live:
            # nothing chosen yet, or the chosen one vanished or died. A
            # microphone that is not judged yet is only a last resort: it may be
            # as dead as the one just left.
            live = [n for n in live if self.judged.get(n)] or live
            measured = [n for n in live if self.evals.get(n, 0) >= 1]
            pick = max(measured, key=lambda n: self.score[n]) if measured else \
                max(live, key=lambda n: self.priority.get(n, 0))
            return self._take(pick, now)
        ready = [n for n in live if self.evals.get(n, 0) >= self.MIN_EVALS and n in self.score]
        if self.current not in self.score or not ready:
            return self.current
        best = max(ready, key=lambda n: self.score[n])
        if best != self.current and self.score[best] - self.score[self.current] >= self.MARGIN \
                and now - self.last_switch >= self.dwell and not speaking_now:
            return self._take(best, now)
        return self.current

    def _take(self, name, now):
        if name != self.current:
            self.last_switch = now
        self.current = name
        return name


# ---------------------------------------------------------------- the daemon


def link_to_chain(objs, chain_capture, source_name):
    """Make the chain's input take exactly one microphone."""
    nodes = {o["id"]: o["info"]["props"] for o in objs
             if o.get("type") == "PipeWire:Interface:Node" and "info" in o}
    cap = next((i for i, p in nodes.items() if p.get("node.name") == chain_capture), None)
    src = next((i for i, p in nodes.items() if p.get("node.name") == source_name), None)
    if cap is None or src is None:
        return False
    ports = [o for o in objs if o.get("type") == "PipeWire:Interface:Port"]
    cap_in = [o for o in ports if o["info"]["props"].get("node.id") == cap
              and o["info"]["direction"] == "input"]
    src_out = sorted((o for o in ports if o["info"]["props"].get("node.id") == src
                      and o["info"]["direction"] == "output"),
                     key=lambda o: (not o["info"]["props"].get("port.name", "").endswith(("_MONO", "_FL")),
                                    o["info"]["props"].get("port.name", "")))
    if not cap_in or not src_out:
        return False
    want = (src_out[0]["id"], cap_in[0]["id"])
    have = False
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Link":
            continue
        i = o["info"]
        if i.get("input-node-id") == cap:
            if i.get("output-node-id") == src and (i["output-port-id"], i["input-port-id"]) == want:
                have = True
            else:
                run(["pw-link", "-d", str(o["id"])])
    if not have:
        r = run(["pw-link", str(want[0]), str(want[1])])
        return r.returncode == 0
    return True


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


class Daemon:
    def __init__(self, args):
        self.a = args
        self.dnsmos = Dnsmos(args.model)
        self.chooser = Chooser(args.dwell)
        self.probes = {}
        self.scored_at = {}  # name -> samples received when it was last scored
        self.deaf = {}       # name -> True while it heard nothing of speech others heard
        self.stats = {}
        self.stop = False

    def sync_probes(self, cands):
        for n in list(self.probes):
            if n not in cands:
                self.probes.pop(n).stop()
                self.chooser.alive.pop(n, None)
        for n in cands:
            if n not in self.probes:
                self.probes[n] = Probe(n)
                self.probes[n].start()
            self.chooser.priority[n] = cands[n]["priority"]

    def evaluate(self):
        """One round of measurement over the windows all probes hold now."""
        stats = {}
        for n, p in self.probes.items():
            w = p.window(WINDOW_S)
            if p.filled < SR * 3:
                st = {"alive": True, "short": True}  # not enough audio to judge yet
            else:
                st = analyze(w)
                # A mute or an unplugged jack must show at once, not after the
                # 9 s window has emptied: liveness is the last 1.5 s.
                st["alive"] = is_alive(p.window(1.5)) and p.fresh()
            st["samples"] = len(w)
            stats[n] = st
            self.chooser.alive[n] = bool(st["alive"])
        # Scores move only while somebody speaks, and a microphone is judged only
        # when it heard that speech. A deaf one stays out until it hears again.
        spoken, deaf = hearing(stats)
        if spoken:
            for n in stats:
                if stats[n].get("alive") and not stats[n].get("short"):
                    self.deaf[n] = n in deaf
        for n, st in stats.items():
            st["deaf"] = bool(self.deaf.get(n, False))
            self.chooser.alive[n] = bool(st["alive"]) and not st["deaf"]
            self.chooser.judged[n] = not st.get("short", False)
        if spoken:
            for n, p in self.probes.items():
                st = stats[n]
                if st.get("alive") and not st["deaf"] and p.filled >= Dnsmos.LENGTH \
                        and p.total - self.scored_at.get(n, -PROBE_BUFFER * 10) >= Dnsmos.LENGTH:
                    mos = self.dnsmos(p.window(WINDOW_S))
                    st["mos"] = [round(v, 2) for v in mos]
                    st["score"] = score(st, mos)
                    self.chooser.update(n, st["score"])
                    self.scored_at[n] = p.total
        self.stats = stats

    def speaking_now(self):
        """Is anyone speaking, as heard by any microphone? The current one may be
        far from the speaker while another is close."""
        for n, p in self.probes.items():
            w = p.window(0.3)  # a pause between phrases, not a gap between words
            if len(w) < FRAME * 3:
                continue
            db = frame_db(w)
            floor = self.stats.get(n, {}).get("floor_db", -90)
            if db.max() > floor + 12 and db.max() > -55:
                return True
        return False

    def write_status(self, cands):
        data = {"time": time.time(), "current": self.chooser.current,
                "mics": {n: {"description": cands.get(n, {}).get("description", n),
                             "running_score": self.chooser.score.get(n),
                             "evals": self.chooser.evals.get(n, 0),
                             **{k: v for k, v in self.stats.get(n, {}).items()}}
                         for n in self.probes}}
        path = self.a.status
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def loop(self, link=True, seconds=None):
        t0 = time.monotonic()
        last_eval = 0.0
        last_disc = -1e9
        cands = {}
        while not self.stop and (seconds is None or time.monotonic() - t0 < seconds):
            try:
                now = time.monotonic()
                relink = False
                if now - last_disc >= 3:
                    last_disc = now
                    objs = dump()
                    if objs is not None:  # a failed dump keeps what is known
                        cands = candidates(objs, self.a.pattern)
                        self.sync_probes(cands)
                        relink = True  # also restores the link after a restart
                chosen = self.chooser.current
                if self.probes and chosen is None:
                    chosen = self.chooser.initial(now, list(self.probes))
                    relink = True
                evaluated = False
                if now - last_eval >= self.a.interval and self.probes:
                    self.evaluate()
                    last_eval = now
                    evaluated = True
                # No judgement until some microphone has 3 s of audio: a muted
                # one looks alive before that. The decision is cheap and runs
                # every second, so a pending switch waits for the first pause in
                # the talk instead of for the next measurement.
                if any(not st.get("short") for st in self.stats.values()):
                    chosen = self.chooser.decide(now, list(self.probes), self.speaking_now())
                    relink = relink or chosen != self.prev_logged
                if evaluated:
                    self.write_status(cands)
                    if self.a.debug:
                        c = self.chooser
                        log("debug:", {n: (c.score.get(n), c.evals.get(n, 0), c.alive.get(n),
                                           round(self.stats.get(n, {}).get("speech", 0), 2))
                                       for n in self.probes},
                            "since switch", round(now - c.last_switch), "speaking", self.speaking_now())
                if chosen != self.prev_logged:
                    log("chosen:", chosen, {n: self.chooser.score.get(n) for n in self.probes})
                    self.prev_logged = chosen
                if link and chosen and relink:
                    objs = dump()
                    if objs is not None:
                        link_to_chain(objs, self.a.chain, chosen)
            except Exception as e:  # one bad round must not end the service
                log("error:", repr(e))
            time.sleep(1.0)
        for p in self.probes.values():
            p.stop()

    prev_logged = None


def table(stats, scores):
    rows = [f"{'microphone':58} {'alive':>5} {'deaf':>5} {'floor':>6} {'snr':>5} {'speech':>6} {'hf dB':>6} "
            f"{'SIG':>4} {'BAK':>4} {'OVR':>4} {'score':>6}"]
    for n, st in sorted(stats.items(), key=lambda kv: -(scores.get(kv[0]) or -9)):
        m = st.get("mos") or ["", "", ""]
        rows.append(f"{n[:58]:58} {str(st.get('alive')):>5} {str(st.get('deaf', '')):>5} {st.get('floor_db', ''):>6} "
                    f"{st.get('snr_db', ''):>5} {st.get('speech', 0):>6.2f} {str(st.get('hf_db', '')):>6} "
                    f"{m[0]:>4} {m[1]:>4} {m[2]:>4} {scores.get(n) if scores.get(n) is not None else '-':>6}")
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "once", "status"])
    ap.add_argument("--model", default=os.environ.get("DNSMOS_MODEL", ""),
                    help="path of sig_bak_ovr.onnx (or $DNSMOS_MODEL)")
    ap.add_argument("--pattern", default=r"^(alsa_input|bluez_input)\.",
                    help="regular expression of the node names that may be chosen")
    ap.add_argument("--chain", default="studiomic.capture",
                    help="node whose input follows the chosen microphone")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between measurements")
    ap.add_argument("--dwell", type=float, default=Chooser.MIN_DWELL,
                    help="least seconds between two switches")
    ap.add_argument("--seconds", type=float, default=30.0, help="how long `once` measures")
    ap.add_argument("--debug", action="store_true", help="log the inputs of every decision")
    ap.add_argument("--status", default=os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "studiomic-select", "status.json"))
    a = ap.parse_args()

    if a.cmd == "status":
        print(open(a.status).read())
        return
    if not a.model:
        sys.exit("studiomic-select: no DNSMOS model (--model or $DNSMOS_MODEL)")
    d = Daemon(a)
    signal.signal(signal.SIGTERM, lambda *_: setattr(d, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(d, "stop", True))
    if a.cmd == "run":
        d.loop(link=True)
    else:
        d.loop(link=False, seconds=a.seconds)
        print(table(d.stats, d.chooser.score))


if __name__ == "__main__":
    main()
