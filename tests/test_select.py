#!/usr/bin/env python3
"""Tests of the selector's decisions, with no PipeWire and no audio files.

    python3 tests/test_select.py

The rule that picks and switches microphones (Chooser), who heard the speech
(hearing) and what counts as a dead source (is_alive) are plain functions, so
they are tested directly. tools/mic_test.py adds the ranking test, which needs
the public test audio.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pkgs", "studiomic-select"))
import studiomic_select as vms  # noqa: E402


def switching():
    C = vms.Chooser
    ok = []

    def fresh(scores, evals=3, current=None, alive=None):
        c = C()
        c.score, c.evals = dict(scores), {n: evals for n in scores}
        c.alive = alive or {n: True for n in scores}
        c.priority = {n: 0 for n in scores}
        c.current = current
        c.last_switch = -1e9
        return c

    names = ["a", "b"]
    c = fresh({"a": 3.0, "b": 3.1}, current="a")
    ok.append(("a better score inside the margin does not switch", c.decide(0, names, False) == "a"))
    c = fresh({"a": 3.0, "b": 3.4}, current="a")
    ok.append(("a clearly better microphone wins", c.decide(0, names, False) == "b"))
    c = fresh({"a": 3.0, "b": 3.4}, current="a")
    ok.append(("not in the middle of a word", c.decide(0, names, True) == "a"))
    c = fresh({"a": 3.0, "b": 3.4}, current="a")
    c.last_switch = 100.0
    ok.append(("not again within the dwell time",
               c.decide(120.0, names, False) == "a" and c.decide(161.0, names, False) == "b"))
    c = fresh({"a": 3.0, "b": 3.4}, evals=1, current="a")
    ok.append(("a microphone measured once cannot win", c.decide(0, names, False) == "a"))
    c = fresh({"a": 3.0, "b": 3.4}, current="a", alive={"a": False, "b": True})
    c.last_switch = 0.0
    ok.append(("a dead current microphone is replaced at once, mid-word and inside the dwell time",
               c.decide(1.0, names, True) == "b"))
    c = fresh({"a": 3.0, "b": 3.4}, current="a")
    ok.append(("a vanished microphone is replaced", c.decide(0, ["b"], False) == "b"))
    c = C()
    c.priority = {"a": 1000, "b": 1900}
    c.alive = {"a": True, "b": True}
    ok.append(("with nothing measured the ladder decides", c.decide(0, names, False) == "b"))
    c = fresh({"a": 3.0, "b": 3.4, "c": 3.2}, current="a", alive={"a": False, "b": True, "c": True})
    c.evals, c.score = {}, {}
    c.priority = {"a": 1000, "b": 3000, "c": 100}
    c.judged = {"a": True, "b": False, "c": True}
    ok.append(("an unjudged microphone is not taken while a judged live one exists",
               c.decide(0, ["a", "b", "c"], False) == "c"))
    c = C()
    c.priority = {"a": 1000, "b": 1900}
    c.alive = {"a": True, "b": False}
    ok.append(("with nothing measured a dead microphone is skipped", c.decide(0, names, False) == "a"))
    c = C()
    c.priority = {"a": 1000, "b": 1900}
    ok.append(("the ladder links something at once, before any audio is judged",
               c.initial(0, names) == "b" and c.initial(1, names) == "b" and c.last_switch == 0))
    flicker = np.array([0, 1, -1, 0, 1] * 400, np.float32) / 32768.0
    quiet = (np.random.default_rng(1).standard_normal(2000) * 3 / 32768.0).astype(np.float32)
    ok.append(("exact zeros and one bit of flicker are dead, a quiet room is alive",
               not vms.is_alive(np.zeros(2000, np.float32)) and not vms.is_alive(flicker)
               and vms.is_alive(quiet)))
    mk = lambda snr, speech, alive=True: {"alive": alive, "snr_db": snr, "speech": speech}
    ok.append(("nobody speaking: nothing is deaf, nothing is scored",
               vms.hearing({"a": mk(3, 0.0), "b": mk(2, 0.0)}) == (False, set())))
    ok.append(("one hears, one hears nothing: the second is deaf (the Pebble X case, 14.5 dB against 2.2)",
               vms.hearing({"dmic": mk(14.5, 0.14), "pebble": mk(2.2, 0.0)}) == (True, {"pebble"})))
    ok.append(("a distant microphone at 8 dB is not deaf",
               vms.hearing({"near": mk(30, 0.6), "far": mk(8, 0.2)}) == (True, set())))
    ok.append(("a dead microphone is not counted as deaf or as hearing",
               vms.hearing({"near": mk(30, 0.6), "dead": mk(0, 0.0, alive=False)}) == (True, set())))
    ok.append(("a quiet talker (below 12 dB on every microphone) scores nothing",
               vms.hearing({"a": mk(9, 0.3), "b": mk(4, 0.1)}) == (False, set())))
    node = lambda i, name: {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": name}}}
    link = lambda i, out, inp: {"id": i, "type": "PipeWire:Interface:Link",
                                "info": {"output-node-id": out, "input-node-id": inp}}
    graph = [node(1, "voice_clean"), node(2, "voice_clean.capture"), node(3, "teams"), node(4, "mic")]
    ok.append(("nobody reads the voice filter: not in use",
               not vms.in_use(graph + [link(10, 4, 2)], "voice_clean")))
    ok.append(("the filter's own input link is not a reader",
               not vms.in_use(graph + [link(10, 4, 2), link(11, 2, 2)], "voice_clean")))
    ok.append(("an application linked to the filter's output is a reader",
               vms.in_use(graph + [link(10, 4, 2), link(11, 1, 3)], "voice_clean")))
    ok.append(("a graph without the filter is not in use", not vms.in_use([node(4, "mic")], "voice_clean")))
    c = fresh({"a": 3.0, "b": 3.4}, current="a")
    c.update("b", None)
    ok.append(("a window without a score changes nothing", c.score["b"] == 3.4 and c.evals["b"] == 3))
    return ok


def main():
    bad = 0
    for text, passed in switching():
        print(("PASS " if passed else "FAIL ") + text)
        bad += not passed
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
