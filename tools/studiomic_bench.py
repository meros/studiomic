#!/usr/bin/env python3
"""studiomic-bench: measure and test studiomic.

  studiomic-bench config                       the config the module builds with defaults
  studiomic-bench corpus DIR                   download the public test audio
  studiomic-bench mix DIR [--snr 3]            mix voice and noise into noisy clips
  studiomic-bench run DIR NAME FILTER          score a filter (none, ladspa:LIB:LABEL[:CONTROLS],
                                               chain:CONFIG, or a command with {in} {out})
  studiomic-bench levels DIR NAME FILTER       the same clip at three input levels
  studiomic-bench report DIR                   table of every scored run
  studiomic-bench offline CONFIG IN OUT        run a generated config offline on a mono 48 kHz WAV
  studiomic-bench ab CONFIG RAW.wav OUT.html   blind listening test of tone variants
  studiomic-bench selftest DIR                 selector ranking and switching tests (needs corpus)
  studiomic-bench live-test DIR [SECONDS]      selector test with fake microphones in the running PipeWire
  studiomic-bench pw-run CONFIG IN OUT         run a config as a client of the running PipeWire

CONFIG may be `-` for the default config. Details: docs/testing.md.
"""
import os
import runpy
import subprocess
import sys

TOOLS = os.environ.get("STUDIOMIC_TOOLS", os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.environ.get("STUDIOMIC_DEFAULT_CONFIG", "")


def config_arg(args, i):
    if len(args) > i and args[i] == "-":
        if not DEFAULT:
            sys.exit("studiomic-bench: no default config (run from the flake package)")
        args[i] = DEFAULT


def script(name, argv):
    sys.argv = [name] + argv
    sys.path.insert(0, TOOLS)
    runpy.run_path(os.path.join(TOOLS, name), run_name="__main__")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    need = {"corpus": 1, "mix": 1, "run": 3, "levels": 3, "report": 1, "offline": 3, "ab": 3,
            "selftest": 1, "live-test": 1, "pw-run": 3}
    if len(args) < need.get(cmd, 0):
        sys.exit(f"studiomic-bench {cmd}: needs {need[cmd]} argument(s); see `studiomic-bench --help`")
    if cmd == "config":
        print(DEFAULT or "no default config")
    elif cmd in ("corpus", "mix", "run", "levels", "report"):
        if cmd in ("run", "levels") and len(args) > 2 and args[2] == "chain:-":
            args[2] = "chain:" + DEFAULT
        script("bench.py", [cmd] + args)
    elif cmd == "offline":
        config_arg(args, 0)
        script("chain_host.py", args)
    elif cmd == "ab":
        config_arg(args, 0)
        script("ab.py", args)
    elif cmd == "selftest":
        script("mic_test.py", args)
    elif cmd == "live-test":
        script("mic_live_test.py", args)
    elif cmd == "pw-run":
        config_arg(args, 0)
        env = dict(os.environ, PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
        sys.exit(subprocess.call([os.path.join(TOOLS, "pw-run")] + args, env=env))
    else:
        sys.exit(f"studiomic-bench: unknown command {cmd!r} (try --help)")


if __name__ == "__main__":
    main()
