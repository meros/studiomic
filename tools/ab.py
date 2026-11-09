#!/usr/bin/env python3
"""ab.py: a blind listening test of microphone filter variants, as a web page.

    ab.py CONF RAW.wav OUT.html

CONF  the studiomic config (tools/studiomic-bench/chain-conf prints its path)
RAW   a mono recording of the raw microphone, 48 kHz (pw-record --target <mic>)
OUT   one HTML file with the audio inside; open it in a browser

Each variant processes the same recording. All are scaled to the same speech
loudness, because a louder one is judged better, and shown under shuffled
letters. One player switches between them at the same playback position, so
the only difference between two clicks is the filter. After rating, "Reveal"
names them and prints a table to paste back.

The variants differ in the tone shaping only; every one except the raw
recording and the "thin" anchor uses the same denoiser and the same leveling,
compressor and limiter as the service.
"""
import base64
import io
import json
import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve, resample_poly, sosfilt, tf2sos

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain_host as ch  # noqa: E402

SR = 48000
TARGET_LUFS = -23.0
LSP = "http://lsp-plug.in/plugins/ladspa/"

# A typical long-term average speech spectrum (Byrne et al. 1994, all talkers),
# in dB per third-octave band, from memory: the shape is right to a few dB, the
# absolute level does not matter because it is used relative to 1 kHz. The blind
# test is what decides whether it is a good target; it is a starting point.
BANDS = [100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000,
         2500, 3150, 4000, 5000, 6300, 8000, 10000]
TARGET = [54.4, 57.7, 56.8, 58.2, 59.8, 60.0, 62.1, 62.1, 60.5, 56.8, 53.7, 53.0, 52.3,
          48.7, 48.1, 46.8, 45.6, 44.5, 42.6, 38.6, 37.1]


def load(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        x = resample_poly(x, SR, sr).astype(np.float32)
    return x


# ---------------------------------------------------------------- measuring


def speech_frames(x, frame=960, above=15.0):
    n = len(x) // frame
    f = x[: n * frame].reshape(n, frame)
    db = 10 * np.log10(np.mean(f * f, axis=1) + 1e-14)
    return db > np.percentile(db, 10) + above, n, frame


def k_weight(x):
    """ITU-R BS.1770 K-weighting at 48 kHz."""
    s1 = tf2sos([1.53512485958697, -2.69169618940638, 1.19839281085285],
                [1.0, -1.69065929318241, 0.73248077421585])
    s2 = tf2sos([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])
    return sosfilt(s2, sosfilt(s1, x))


def speech_lufs(x, mask):
    """Loudness of the frames in `mask` (K-weighted mean square, no gating)."""
    frame = 960
    n = min(len(mask), len(x) // frame)
    kw = k_weight(x)[: n * frame].reshape(n, frame)[mask[:n]]
    return -0.691 + 10 * np.log10(np.mean(kw * kw) + 1e-14)


def ltas(x):
    """Mean power spectrum of the speech frames, third-octave bands, dB."""
    mask, n, frame = speech_frames(x)
    nfft = 4096
    acc, cnt = np.zeros(nfft // 2 + 1), 0
    for i in np.nonzero(mask)[0]:
        seg = x[i * frame:(i + 1) * frame]
        seg = np.pad(seg, (0, nfft - len(seg))) * np.hanning(nfft)
        acc += np.abs(np.fft.rfft(seg)) ** 2
        cnt += 1
    hz = np.fft.rfftfreq(nfft, 1 / SR)
    out = []
    for c in BANDS:
        sel = (hz >= c / 2 ** (1 / 6)) & (hz < c * 2 ** (1 / 6))
        out.append(10 * np.log10(acc[sel].sum() / max(cnt, 1) + 1e-20))
    return np.array(out)


# ---------------------------------------------------------------- variants


class FirEq:
    """A linear-phase FIR equalizer, applied to the whole signal."""

    latency = None

    def __init__(self, ir):
        self.ir = ir

    def process_all(self, x):
        d = len(self.ir) // 2
        return fftconvolve(x, self.ir)[d:d + len(x)].astype(np.float32)


def matched_eq(denoised):
    """Gain per band that moves the speech spectrum toward the typical one."""
    i1k = BANDS.index(1000)
    meas = ltas(denoised)
    meas = meas - meas[i1k]
    tgt = np.array(TARGET) - TARGET[i1k]
    gain = np.clip(tgt - meas, -6.0, 6.0)
    gain = np.convolve(np.pad(gain, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
    n = 4096
    hz = np.fft.rfftfreq(n, 1 / SR)
    g_db = np.interp(np.log(np.maximum(hz, 1.0)), np.log(BANDS), gain)
    g_db[hz < 80] = 0.0  # the high-pass owns everything below
    mag = 10 ** (g_db / 20)
    ir = np.fft.fftshift(np.fft.irfft(mag, n)) * np.hanning(n)
    return FirEq(ir.astype(np.float32)), dict(zip(BANDS, np.round(gain, 1)))


def bq(name, label, freq, q, gain=None):
    c = {"Freq": freq, "Q": q}
    if gain is not None:
        c["Gain"] = gain
    return {"type": "builtin", "name": name, "label": label, "control": c}


def variants(conf):
    """name -> (description, spec list or callable(denoised) -> stage list)."""
    c = json.load(open(conf))
    g = next(m for m in c["context.modules"]
             if m["name"] == "libpipewire-module-filter-chain")["args"]["filter.graph"]["nodes"]
    by = {n["name"]: n for n in g}
    head = [by["dpdf"], by["hpf"]]
    tail = [by[k] for k in ("gain", "comp", "limit", "ceiling")]
    today = head + [by[k] for k in ("mud", "presence", "air") if k in by] + tail
    deess = {"type": "ladspa", "name": "deess", "plugin": by["comp"]["plugin"], "label": LSP + "deesser_mono",
             "control": {"High-pass filter frequency (Hz)": 4500.0, "Low-pass filter frequency (Hz)": 10000.0,
                         "Threshold (G)": 0.03, "Ratio": 4.0}}
    warm = [bq("body", "bq_lowshelf", 180.0, 0.7, 3.0), bq("mud", "bq_peaking", 350.0, 1.0, -1.5),
            bq("soft", "bq_highshelf", 6000.0, 0.7, -3.0)]
    thin = [bq("thin_hp", "bq_highpass", 450.0, 0.707), bq("thin_pk", "bq_peaking", 3000.0, 1.0, 4.0)]
    return {
        "raw": ("the laptop microphone, unprocessed (hidden reference)", None),
        "thin": ("hidden anchor: denoised, thin (cut below 450 Hz, +4 dB at 3 kHz)", head + thin + tail),
        "today": ("the service as it runs now: denoise, 80 Hz high-pass, EQ -2 dB 280 Hz, +2 dB 3.5 kHz, "
                  "+1.5 dB above 9 kHz, leveling", today),
        "flat": ("denoise + high-pass + leveling, no EQ", head + tail),
        "warm": ("flat + 3 dB low shelf at 180 Hz, -1.5 dB at 350 Hz, -3 dB high shelf at 6 kHz", head + warm + tail),
        # after the leveler: before it the signal is too quiet to reach the threshold
        "warm+deess": ("warm + de-esser (4.5 to 10 kHz)", head + warm + tail[:1] + [deess] + tail[1:]),
        "matched": ("flat + automatic EQ toward a typical speech spectrum (at most +-6 dB per band)",
                    lambda dn: head),  # built below, needs the denoised signal
    }, head, tail


def render(conf, raw):
    spec, head, tail = variants(conf)
    out, info = {}, {}
    for name, (desc, stages) in spec.items():
        if stages is None:
            y, lat = raw.copy(), 0
        elif name == "matched":
            hn = ch.build_nodes(head, SR)
            first = ch.process(hn, raw)
            fir, gains = matched_eq(first)
            info["matched gains dB"] = gains
            tn = ch.build_nodes(tail, SR)
            y = ch.process(tn, fir.process_all(first))
            lat = int(sum((n.latency[0] if getattr(n, "latency", None) else 0.0) for n in hn + tn))
        else:
            nodes = ch.build_nodes(stages, SR)
            y = ch.process(nodes, raw)
            lat = int(sum((n.latency[0] if getattr(n, "latency", None) else 0.0) for n in nodes))
        out[name] = y[lat:]
    n = min(len(v) for v in out.values())
    out = {k: v[:n] for k, v in out.items()}
    # The same speech loudness for every variant, measured over the same frames:
    # the ones that are speech in the "flat" variant, where the denoiser has
    # taken the noise away. (In the raw recording the speech is hard to tell
    # from the noise, so its own frames would be a poor guide.)
    mask = speech_frames(out["flat"])[0]
    notes = {}
    for k, v in out.items():
        v = v * 10 ** ((TARGET_LUFS - speech_lufs(v, mask)) / 20)
        pk = float(np.abs(v).max())
        if pk > 0.97:
            v = v * 0.97 / pk
            notes[k] = f"scaled down {20 * np.log10(0.97 / pk):.1f} dB to avoid clipping"
        fade = int(0.02 * SR)
        v[:fade] *= np.linspace(0, 1, fade)
        v[-fade:] *= np.linspace(1, 0, fade)
        out[k] = v.astype(np.float32)
    return out, spec, info, notes


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blind mic test</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#d9d9de;--acc:#0a66d9;--card:#f5f5f7}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f4;--mut:#9a9aa2;--line:#33333a;--acc:#5aa2ff;--card:#202024}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 80px}
h1{font-size:1.4rem;margin:.2em 0}p{margin:.5em 0}.mut{color:var(--mut)}
.bar{position:sticky;top:0;background:var(--bg);padding:10px 0;border-bottom:1px solid var(--line);z-index:2}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:8px 14px;cursor:pointer}
button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.row{display:grid;grid-template-columns:3.2rem 1fr;gap:10px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.row .lab{font-size:1.3rem;font-weight:600;text-align:center}
.row.cur{background:color-mix(in srgb,var(--acc) 10%,transparent)}
input[type=range]{width:100%}input[type=text]{width:100%;box-sizing:border-box;font:inherit;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.val{font-variant-numeric:tabular-nums;color:var(--mut);float:right}
pre{white-space:pre-wrap;background:var(--card);padding:12px;border-radius:8px;overflow:auto}
#seek{width:100%}
</style></head><body><main>
<h1>Blind microphone test</h1>
<p>Same recording, different filters. Pick a letter, listen, and rate how much it sounds like a
<b>real, full studio microphone</b>: 0 is thin and tinny like a laptop, 100 is full and natural.
Compare with headphones if you can. Keys 1 to __N__ switch, space plays and stops.</p>
<div class="bar">
<button id="play">Play</button> <span class="mut" id="time"></span>
<input id="seek" type="range" min="0" max="1000" value="0">
</div>
<div id="rows"></div>
<p><button id="reveal">Reveal and show results</button></p>
<div id="out" hidden><p>Copy this and paste it to me:</p><pre id="txt"></pre></div>
</main>
<script>
const SAMPLES = __SAMPLES__;
const ctx = new (window.AudioContext||window.webkitAudioContext)();
const order = SAMPLES.map((_,i)=>i); for(let i=order.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[order[i],order[j]]=[order[j],order[i]];}
let bufs=[], gains=[], srcs=[], cur=0, playing=false, t0=0, dur=1;
const rating = order.map(()=>50), notes = order.map(()=>'');
const b64 = s => Uint8Array.from(atob(s), c=>c.charCodeAt(0)).buffer;
async function init(){
  for(const i of order){ bufs.push(await ctx.decodeAudioData(b64(SAMPLES[i].data))); }
  dur = bufs[0].duration; build();
}
function build(){
  const rows=document.getElementById('rows');
  order.forEach((i,k)=>{
    const d=document.createElement('div'); d.className='row'; d.id='r'+k;
    d.innerHTML=`<div class="lab"><button data-k="${k}">${String.fromCharCode(65+k)}</button></div>
     <div><span class="val" id="v${k}">50</span><input type="range" min="0" max="100" value="50" data-r="${k}">
     <input type="text" placeholder="notes: harsh S, muddy, boxy, hissy, natural..." data-n="${k}"></div>`;
    rows.appendChild(d);
  });
  rows.addEventListener('click',e=>{const k=e.target.dataset.k; if(k!==undefined) select(+k);});
  rows.addEventListener('input',e=>{
    if(e.target.dataset.r!==undefined){rating[+e.target.dataset.r]=+e.target.value;document.getElementById('v'+e.target.dataset.r).textContent=e.target.value;}
    if(e.target.dataset.n!==undefined){notes[+e.target.dataset.n]=e.target.value;}
  });
  mark();
}
function mark(){document.querySelectorAll('.row').forEach((r,k)=>r.classList.toggle('cur',k===cur));document.querySelectorAll('.lab button').forEach((b,k)=>b.classList.toggle('on',k===cur));}
function startAt(from){
  stop(false);
  srcs=bufs.map((b,k)=>{const s=ctx.createBufferSource();s.buffer=b;s.loop=true;const g=ctx.createGain();g.gain.value=(k===cur)?1:0;s.connect(g).connect(ctx.destination);s.start(0,from);gains[k]=g;return s;});
  t0=ctx.currentTime-from; playing=true; document.getElementById('play').textContent='Stop';
}
function stop(ui=true){srcs.forEach(s=>{try{s.stop()}catch(e){}});srcs=[];playing=false;if(ui)document.getElementById('play').textContent='Play';}
function pos(){return playing?((ctx.currentTime-t0)%dur):0}
function select(k){cur=k;const t=ctx.currentTime;gains.forEach((g,i)=>{g.gain.cancelScheduledValues(t);g.gain.setTargetAtTime(i===k?1:0,t,0.008);});mark();}
document.getElementById('play').onclick=async()=>{await ctx.resume();playing?stop():startAt(+document.getElementById('seek').value/1000*dur);};
document.getElementById('seek').oninput=e=>{if(playing)startAt(+e.target.value/1000*dur);};
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'&&e.target.type==='text')return;
  if(e.code==='Space'){e.preventDefault();document.getElementById('play').click();}
  const n=parseInt(e.key); if(n>=1&&n<=order.length)select(n-1);});
setInterval(()=>{if(playing){document.getElementById('seek').value=pos()/dur*1000;document.getElementById('time').textContent=pos().toFixed(1)+' / '+dur.toFixed(1)+' s';}},100);
document.getElementById('reveal').onclick=()=>{
  const rows=order.map((i,k)=>({l:String.fromCharCode(65+k),name:SAMPLES[i].name,desc:SAMPLES[i].desc,r:rating[k],n:notes[k]})).sort((a,b)=>b.r-a.r);
  let t='rank  letter  score  variant\n';
  rows.forEach((x,i)=>{t+=`${i+1}.    ${x.l}       ${String(x.r).padStart(3)}    ${x.name}${x.n?'   | '+x.n:''}\n`;});
  t+='\nvariants:\n'+SAMPLES.map(s=>'- '+s.name+': '+s.desc).join('\n');
  document.getElementById('txt').textContent=t; document.getElementById('out').hidden=false;
};
init();
</script></body></html>
"""


def wav_b64(x):
    buf = io.BytesIO()
    sf.write(buf, x, SR, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode()


def main(conf, raw_path, out_html):
    raw = load(raw_path)
    seg, _, _ = speech_frames(raw)
    print(f"raw: {len(raw) / SR:.1f} s, {100 * seg.mean():.0f} % of frames count as speech")
    out, spec, info, notes = render(conf, raw)
    mask = speech_frames(out["flat"])[0]
    print(f"{'variant':12} {'speech LUFS':>12} {'peak dB':>8}")
    for k, v in out.items():
        print(f"{k:12} {speech_lufs(v, mask):>12.1f} {20 * np.log10(np.abs(v).max()):>8.1f}  {notes.get(k, '')}")
    if "matched gains dB" in info:
        print("matched EQ gains (dB per third-octave band):", info["matched gains dB"])
    samples = [{"name": k, "desc": spec[k][0], "data": wav_b64(v)} for k, v in out.items()]
    html = PAGE.replace("__SAMPLES__", json.dumps(samples)).replace("__N__", str(len(samples)))
    os.makedirs(os.path.dirname(os.path.abspath(out_html)), exist_ok=True)
    open(out_html, "w").write(html)
    print("wrote", out_html, f"({len(html) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(*sys.argv[1:4])
