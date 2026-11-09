#!/usr/bin/env python3
"""Run the studiomic graph offline, deterministically, from its own config.

    chain_host.py CONF IN.wav OUT.wav

CONF is the file modules/nixos/services/studiomic/chain.nix builds (the one
the PipeWire service loads). The graph is a straight line of nodes; each is
either a LADSPA plugin, loaded by path and label with its controls set by port
name, or one of PipeWire's builtin biquads (bq_highpass, bq_peaking,
bq_highshelf, bq_lowshelf), which follow the RBJ audio-EQ cookbook.

Audio goes through in blocks of 1024 samples, like the PipeWire quantum, so
stateful plugins and their latency behave as they do live. The only things
this does not reproduce are PipeWire's own buffering and resampling; for
those, pw-run runs the real service.
"""
import ctypes as C
import json
import sys

import numpy as np
import soundfile as sf
from scipy.signal import lfilter

BLOCK = 1024


class Descriptor(C.Structure):
    pass


P_F = C.POINTER(C.c_float)
Descriptor._fields_ = [
    ("UniqueID", C.c_ulong), ("Label", C.c_char_p), ("Properties", C.c_int),
    ("Name", C.c_char_p), ("Maker", C.c_char_p), ("Copyright", C.c_char_p),
    ("PortCount", C.c_ulong), ("PortDescriptors", C.POINTER(C.c_int)),
    ("PortNames", C.POINTER(C.c_char_p)), ("PortRangeHints", C.c_void_p),
    ("ImplementationData", C.c_void_p),
    ("instantiate", C.CFUNCTYPE(C.c_void_p, C.POINTER(Descriptor), C.c_ulong)),
    ("connect_port", C.CFUNCTYPE(None, C.c_void_p, C.c_ulong, P_F)),
    ("activate", C.CFUNCTYPE(None, C.c_void_p)),
    ("run", C.CFUNCTYPE(None, C.c_void_p, C.c_ulong)),
    ("run_adding", C.c_void_p), ("set_run_adding_gain", C.c_void_p),
    ("deactivate", C.CFUNCTYPE(None, C.c_void_p)),
    ("cleanup", C.CFUNCTYPE(None, C.c_void_p)),
]
AUDIO, CONTROL, INPUT, OUTPUT = 8, 4, 1, 2  # LADSPA_PORT_* bit values


class Ladspa:
    def __init__(self, path, label, controls, rate):
        lib = C.CDLL(path)
        lib.ladspa_descriptor.restype = C.POINTER(Descriptor)
        lib.ladspa_descriptor.argtypes = [C.c_ulong]
        i = 0
        while True:
            d = lib.ladspa_descriptor(i)
            if not d:
                raise SystemExit(f"{path}: no plugin {label}")
            if d.contents.Label.decode() == label:
                break
            i += 1
        self.lib, self.d = lib, d.contents
        self.h = self.d.instantiate(d, rate)
        if not self.h:
            raise SystemExit(f"{label}: instantiate failed")
        names = [self.d.PortNames[k].decode() for k in range(self.d.PortCount)]
        unknown = set(controls) - set(names)
        if unknown:
            raise SystemExit(f"{label}: no such port {sorted(unknown)}")
        self.in_buf = (C.c_float * BLOCK)()
        self.out_buf = (C.c_float * BLOCK)()
        self.ctl = {}
        self.latency = None
        seen_in = seen_out = False
        for k, name in enumerate(names):
            desc = self.d.PortDescriptors[k]
            if desc & AUDIO:
                if desc & INPUT and not seen_in:
                    self.d.connect_port(self.h, k, C.cast(self.in_buf, P_F))
                    seen_in = True
                elif desc & OUTPUT and not seen_out:
                    self.d.connect_port(self.h, k, C.cast(self.out_buf, P_F))
                    seen_out = True
            else:
                v = (C.c_float * 1)()
                # a control the config leaves out keeps the plugin's own default
                v[0] = controls.get(name, DEFAULTS.get((label, name), 0.0))
                self.d.connect_port(self.h, k, C.cast(v, P_F))
                self.ctl[name] = v
                if name == "latency":
                    self.latency = v
        if self.d.activate:
            self.d.activate(self.h)

    def process(self, x):
        n = len(x)
        C.memmove(self.in_buf, x.ctypes.data, 4 * n)
        self.d.run(self.h, n)
        return np.ctypeslib.as_array(self.out_buf)[:n].copy()

    def __del__(self):
        try:
            self.d.cleanup(self.h)
        except Exception:
            pass


# (label, port name) -> LADSPA default, filled by read_defaults() before a
# plugin is instantiated.
DEFAULTS = {}


def read_defaults(path, label):
    """Default of every control port, from the range hints (LADSPA 1.1 rules)."""
    lib = C.CDLL(path)
    lib.ladspa_descriptor.restype = C.POINTER(Descriptor)
    lib.ladspa_descriptor.argtypes = [C.c_ulong]
    i = 0
    while True:
        d = lib.ladspa_descriptor(i)
        if not d:
            return
        if d.contents.Label.decode() == label:
            break
        i += 1

    class Hint(C.Structure):
        _fields_ = [("HintDescriptor", C.c_int), ("LowerBound", C.c_float), ("UpperBound", C.c_float)]

    hints = C.cast(d.contents.PortRangeHints, C.POINTER(Hint))
    for k in range(d.contents.PortCount):
        if d.contents.PortDescriptors[k] & AUDIO:
            continue
        h = hints[k]
        lo, hi, fl = h.LowerBound, h.UpperBound, h.HintDescriptor
        log = bool(fl & 0x10)
        mix = lambda t: float(np.exp(np.log(lo) * (1 - t) + np.log(hi) * t)) if log else lo * (1 - t) + hi * t
        sel = fl & 0x3C0
        val = 0.0
        if sel == 0x40: val = lo                  # DEFAULT_MINIMUM
        elif sel == 0x80: val = mix(0.25)         # DEFAULT_LOW
        elif sel == 0xC0: val = mix(0.5)          # DEFAULT_MIDDLE
        elif sel == 0x100: val = mix(0.75)        # DEFAULT_HIGH
        elif sel == 0x140: val = hi               # DEFAULT_MAXIMUM
        elif sel == 0x200: val = 0.0              # DEFAULT_0
        elif sel == 0x240: val = 1.0              # DEFAULT_1
        elif sel == 0x280: val = 100.0            # DEFAULT_100
        elif sel == 0x2C0: val = 440.0            # DEFAULT_440
        elif not fl & 0x4 and fl & 0x3 == 0x3:
            # no default hint: PipeWire takes the middle of the range
            val = (lo + hi) / 2
        name = d.contents.PortNames[k].decode()
        DEFAULTS[(label, name)] = val


class Biquad:
    """RBJ cookbook biquad, like PipeWire's builtin bq_* filters."""

    def __init__(self, kind, rate, freq, q, gain=0.0):
        w = 2 * np.pi * freq / rate
        cw, sw = np.cos(w), np.sin(w)
        alpha = sw / (2 * q)
        A = 10 ** (gain / 40)
        if kind == "bq_highpass":
            b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
            a = [1 + alpha, -2 * cw, 1 - alpha]
        elif kind == "bq_peaking":
            b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
            a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
        elif kind in ("bq_highshelf", "bq_lowshelf"):
            s = 2 * np.sqrt(A) * alpha
            if kind == "bq_highshelf":
                b = [A * ((A + 1) + (A - 1) * cw + s), -2 * A * ((A - 1) + (A + 1) * cw),
                     A * ((A + 1) + (A - 1) * cw - s)]
                a = [(A + 1) - (A - 1) * cw + s, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - s]
            else:
                b = [A * ((A + 1) - (A - 1) * cw + s), 2 * A * ((A - 1) - (A + 1) * cw),
                     A * ((A + 1) - (A - 1) * cw - s)]
                a = [(A + 1) + (A - 1) * cw + s, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - s]
        else:
            raise SystemExit(f"builtin {kind} not implemented here")
        self.b = np.array(b) / a[0]
        self.a = np.array(a) / a[0]
        self.zi = np.zeros(2)
        self.latency = None

    def process(self, x):
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y.astype(np.float32)


class Clamp:
    """PipeWire's builtin clamp: samples limited to [Min, Max]."""

    latency = None

    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi

    def process(self, x):
        return np.clip(x, self.lo, self.hi).astype(np.float32)


def build(conf, rate):
    c = json.load(open(conf))
    fc = next(m for m in c["context.modules"] if m["name"] == "libpipewire-module-filter-chain")
    return build_nodes(fc["args"]["filter.graph"]["nodes"], rate)


def build_nodes(specs, rate):
    """Nodes from a list of graph node dicts (the format of the config file)."""
    nodes = []
    for n in specs:
        ctl = n.get("control", {})
        if n["type"] == "ladspa":
            read_defaults(n["plugin"], n["label"])
            nodes.append(Ladspa(n["plugin"], n["label"], ctl, rate))
        elif n["label"] == "clamp":
            nodes.append(Clamp(ctl["Min"], ctl["Max"]))
        else:
            nodes.append(Biquad(n["label"], rate, ctl["Freq"], ctl["Q"], ctl.get("Gain", 0.0)))
    return nodes


def process(nodes, x):
    """Run x (float32 mono) through the nodes in blocks of 1024, one stage at a
    time. A stage with its own whole-signal method (an FIR) is called once."""
    x = np.ascontiguousarray(x, dtype=np.float32)
    for n in nodes:
        if hasattr(n, "process_all"):
            x = n.process_all(x)
            continue
        pad = np.zeros((-len(x)) % BLOCK, np.float32)
        xp = np.concatenate([x, pad])
        x = np.concatenate([n.process(np.ascontiguousarray(xp[i:i + BLOCK])) for i in range(0, len(xp), BLOCK)])[: len(x)]
    return x


def main(conf, src, dst):
    x, rate = sf.read(src, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)  # a stereo recording: the chain is mono
    if rate != 48000:
        sys.exit(f"{src}: the plugin needs 48 kHz audio, this is {rate} Hz")
    nodes = build(conf, rate)
    pad = np.zeros((-len(x)) % BLOCK, np.float32)
    x = np.concatenate([x, pad])
    out = []
    for i in range(0, len(x), BLOCK):
        b = x[i:i + BLOCK]
        for n in nodes:
            b = n.process(np.ascontiguousarray(b, dtype=np.float32))
        out.append(b)
    sf.write(dst, np.concatenate(out), rate, subtype="FLOAT")
    lat = sum((n.latency[0] if n.latency else 0.0) for n in nodes)
    print(f"graph latency reported by the plugins: {lat:.0f} samples = {1000 * lat / rate:.1f} ms",
          file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
