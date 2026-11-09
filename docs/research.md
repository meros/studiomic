# Research notes: a thin microphone to a studio microphone

Written in November 2026 to answer "why does it sound tinny, and what would make it
sound like a studio microphone?". The sources are the papers' own claims and vocal
advice from blogs; no independent benchmark exists for this question. Nothing here
was reproduced.

## Why it sounded tinny

The first EQ brightened the voice (+2 dB at 3.5 kHz, +1.5 dB above 9 kHz). Common
advice in vocal EQ guides for a thin source says the opposite: cut the 200 to 500 Hz
mud before boosting anything, add a low shelf or some saturation for body, use a 2 dB
"air" boost only on dull microphones, put a de-esser after anything that adds highs,
and move closer to the microphone for proximity-effect body, which beats any EQ.
Sources of that kind: [an EQ guide](https://ledgernote.com/columns/mixing-mastering/how-to-eq-vocals/),
[a forum thread on airy vocals](https://www.kvraudio.com/forum/viewtopic.php?p=8867029).
The search did not tie each claim to one page, so treat them as folk practice.

## Approaches

| Approach | Evidence | Fit |
|---|---|---|
| Match the long-term spectrum to a target curve | A [France Telecom study](https://core.ac.uk/download/52200573.pdf) restored timbre for most speakers; a [speech conformer paper](https://www.ioa.org.uk/system/files/publications/X%20MEYNIAL%20THE%20SPEECH%20CONFORMER%20FOR%20ACHIEVING%20GOOD%20SPEECH%20INTELLIGIBILITY%20FOR%20ALL%20SPEAKERS.pdf) describes an adaptive real-time version with little CPU. | Tried as the `matched` variant of the blind test; a fixed warm curve beat it. |
| Studio-voice neural models | [NVIDIA Studio Voice](https://docs.nvidia.com/maxine/afx/latest/AboutTheEffects/AboutStudioVoiceEffect.html): a time-domain encoder-decoder conditioned on a studio-quality embedding; Tensor-core GPUs; up to 110 ms of lag in the low-latency variant. | Not on Intel iGPUs. |
| Streaming generative restoration | [Stream.FM](https://arxiv.org/abs/2512.19442): 32 ms algorithmic, 48 ms total latency on consumer GPUs. [StreamSE](https://arxiv.org/abs/2610.37024): 20 M parameters in a codec's latent space, real time on one phone CPU core. | Code release not confirmed for either (Stream.FM's examples page said "coming soon"). Not tried. |
| Offline restoration | [Resemble Enhance and VoiceFixer](https://arxiv.org/pdf/2109.13731) are the open baselines; comparisons are secondhand. | Would give a quality ceiling; not in nixpkgs. Not tried. |
| Microphone emulation | [MicAugment](https://arxiv.org/abs/2010.09658) needs recordings of the target microphone; commercial "virtual microphones" are hardware arrays plus DSP. | Needs paired data. Not tried. |
| Bandwidth extension | Recent work generates *high* frequencies ([8 to 48 kHz](https://web3.arxiv.org/pdf/2508.02974)). Nothing found for restoring the low end. | Does not address missing body. |

A hybrid system in the ICASSP 2026 URGENT challenge noted that its [generative branch
is limited to 16 kHz and has high latency](https://arxiv.org/pdf/2601.19113): full-band
real-time generative output is open.

## Gaps

No source on psychoacoustic bass for speech. No peer-reviewed work found on adaptive
real-time voice EQ beyond the two telephony-era papers above. DNSMOS cannot judge
"studio"; only listening can, which is why the repository has a blind test.
