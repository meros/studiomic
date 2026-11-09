/*
 * dpdfnet_ladspa: DPDFNet speech enhancement as a mono LADSPA plugin.
 *
 * DPDFNet (Ceva, Apache-2.0, arXiv:2512.16420) is a streaming model that
 * takes one STFT frame per call plus a recurrent state vector and returns the
 * enhanced frame plus the next state. This file holds everything around the
 * model: the causal STFT, the call into ONNX Runtime, the inverse STFT with
 * overlap-add, and the LADSPA glue. It follows the reference streaming code in
 * ceva-ip/DPDFNet (package/src/dpdfnet/stream.py): causal analysis (no centre
 * padding), Vorbis window, 50 % overlap, state initialised from the model
 * metadata.
 *
 * Threading. Everything runs inside run(), on the host's audio thread. That
 * thread is the one PipeWire runs at real-time priority through rtkit, so a
 * busy machine (a build, a browser) does not delay the model, which a
 * normal-priority worker thread of ours would. The price is that a frame that
 * takes longer than the period causes an xrun. A frame costs about 3 ms of CPU
 * per 10 ms of audio for the light 48 kHz model (tools/voice-bench measures
 * it), so use a quantum of 512 samples or more. The model runs on buffers
 * allocated at start-up, one ONNX Runtime thread, no spinning.
 *
 * Latency, reported on the "latency" port, in samples:
 *   win                one analysis window
 *   + LOOKAHEAD * hop  the model's own look-ahead (measured: 60 ms in total
 *                      for the 48 kHz models, see tools/voice-bench)
 *
 * Model file: $DPDFNET_MODEL if set, else the path compiled in with
 * -DDPDFNET_MODEL_PATH. Set DPDFNET_DEBUG=1 to print frame timing (thread CPU
 * time, which a busy machine does not distort) when the instance is destroyed.
 *
 * The sample rate of the host must equal the sample rate in the model
 * metadata; the instance fails to load otherwise (resample outside).
 */

#define _GNU_SOURCE
#include <kiss_fftr.h>
#include <ladspa.h>
#include <locale.h>
#include <math.h>
#include <onnxruntime_c_api.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#if defined(__SSE__)
#include <xmmintrin.h>
#endif

#ifndef DPDFNET_MODEL_PATH
#define DPDFNET_MODEL_PATH ""
#endif

#define PORT_IN 0
#define PORT_OUT 1
#define PORT_ATTEN 2
#define PORT_LATENCY 3

/* The model's output frame t belongs to the input frame t - LOOKAHEAD. The
 * reference code aligns the noisy frame for the attenuation limit the same way
 * (ATTN_LIMIT_NOISY_FRAME_OFFSET = 4). */
#define LOOKAHEAD 4
/* Largest chunk handled per inner loop: bounds the output FIFO. */
#define CHUNK 4096

static const OrtApi *ort;
static OrtEnv *ort_env;
static pthread_mutex_t init_lock = PTHREAD_MUTEX_INITIALIZER;
static int instances;
/* The host sets the user's locale (sv_SE writes 0,5), the model metadata writes
 * 0.5. Parse numbers with the C locale, whatever the host does. */
static locale_t c_locale;

typedef struct {
    /* host */
    unsigned long rate;
    LADSPA_Data *in, *out, *atten, *latency;

    /* model geometry */
    int win, hop, bins;
    float *window;

    /* STFT */
    kiss_fftr_cfg fwd, inv;
    float *fft_time;
    kiss_fft_cpx *fft_freq;

    /* streaming buffers */
    float *inbuf;  /* win */
    int incount;
    float *olabuf; /* win */
    float *hist;   /* LOOKAHEAD + 1 noisy spectra, bins * 2 floats each */
    unsigned long frame_no;
    float *fifo; /* output ring, power-of-two size */
    unsigned long fifo_mask, fifo_rd, fifo_wr;

    /* ONNX Runtime */
    OrtSession *session;
    OrtMemoryInfo *meminfo;
    char *name_spec_in, *name_state_in, *name_spec_out, *name_state_out;
    float *spec_in, *spec_out;
    float *state[2];
    int state_len;
    OrtValue *v_spec_in, *v_spec_out;
    OrtValue *v_state[2];
    int cur; /* index of the state buffer fed to the next run */
    float *init_state; /* the state a stream starts from, kept to recover from NaN */
    int counted;       /* this instance holds a reference on ort_env */
    unsigned logged;   /* error lines printed so far; limits log spam in the audio thread */

    /* debug */
    int debug;
    double *times, *times_wall;
    unsigned long ntimes, times_cap;
} Plugin;

static int check(OrtStatus *st, const char *what)
{
    if (!st)
        return 0;
    fprintf(stderr, "dpdfnet_ladspa: %s: %s\n", what, ort->GetErrorMessage(st));
    ort->ReleaseStatus(st);
    return -1;
}

static double clock_ms(clockid_t id)
{
    struct timespec ts;
    clock_gettime(id, &ts);
    return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

/* Metadata value as a malloc'd string, or NULL. */
static char *meta(OrtModelMetadata *md, OrtAllocator *al, const char *key)
{
    char *v = NULL;
    if (ort->ModelMetadataLookupCustomMetadataMap(md, al, key, &v) || !v)
        return NULL;
    char *copy = strdup(v);
    al->Free(al, v);
    return copy;
}

static int parse_floats(const char *csv, float *dst, int n)
{
    char *end;
    for (int i = 0; i < n; i++) {
        dst[i] = strtof_l(csv, &end, c_locale);
        if (end == csv)
            return -1;
        csv = end;
        if (*csv == ',')
            csv++;
    }
    return 0;
}

static void destroy(Plugin *p)
{
    if (!p)
        return;
    for (int i = 0; i < 2; i++)
        if (p->v_state[i])
            ort->ReleaseValue(p->v_state[i]);
    if (p->v_spec_in)
        ort->ReleaseValue(p->v_spec_in);
    if (p->v_spec_out)
        ort->ReleaseValue(p->v_spec_out);
    if (p->session)
        ort->ReleaseSession(p->session);
    if (p->meminfo)
        ort->ReleaseMemoryInfo(p->meminfo);
    free(p->fwd);
    free(p->inv);
    pthread_mutex_lock(&init_lock);
    if (p->counted && --instances == 0 && ort_env) {
        ort->ReleaseEnv(ort_env);
        ort_env = NULL;
    }
    pthread_mutex_unlock(&init_lock);
    free(p->name_spec_in);
    free(p->name_state_in);
    free(p->name_spec_out);
    free(p->name_state_out);
    free(p->window);
    free(p->inbuf);
    free(p->olabuf);
    free(p->hist);
    free(p->fifo);
    free(p->times);
    free(p->times_wall);
    free(p->init_state);
    free(p->spec_in);
    free(p->spec_out);
    free(p->state[0]);
    free(p->state[1]);
    free(p->fft_time);
    free(p->fft_freq);
    free(p);
}

static LADSPA_Handle instantiate(const LADSPA_Descriptor *d, unsigned long rate)
{
    (void)d;
    Plugin *p = calloc(1, sizeof *p);
    if (!p)
        return NULL;
    p->rate = rate;
    p->debug = getenv("DPDFNET_DEBUG") != NULL;

    const char *path = getenv("DPDFNET_MODEL");
    if (!path || !*path)
        path = DPDFNET_MODEL_PATH;
    if (!*path) {
        fprintf(stderr, "dpdfnet_ladspa: no model path (set DPDFNET_MODEL)\n");
        goto fail;
    }

    pthread_mutex_lock(&init_lock);
    if (!ort)
        ort = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    if (!c_locale)
        c_locale = newlocale(LC_NUMERIC_MASK, "C", (locale_t)0);
    int env_ok = c_locale != (locale_t)0;
    if (env_ok && !ort_env)
        env_ok = !check(ort->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "dpdfnet", &ort_env),
                        "CreateEnv");
    if (env_ok) { /* take the reference before anyone can release the env */
        instances++;
        p->counted = 1;
    }
    pthread_mutex_unlock(&init_lock);
    if (!env_ok)
        goto fail;

    OrtSessionOptions *so;
    if (check(ort->CreateSessionOptions(&so), "CreateSessionOptions"))
        goto fail;
    int opts_bad =
        check(ort->SetIntraOpNumThreads(so, 1), "intra threads") ||
        check(ort->SetInterOpNumThreads(so, 1), "inter threads") ||
        check(ort->SetSessionExecutionMode(so, ORT_SEQUENTIAL), "execution mode") ||
        check(ort->SetSessionGraphOptimizationLevel(so, ORT_ENABLE_ALL), "graph level") ||
        check(ort->AddSessionConfigEntry(so, "session.intra_op.allow_spinning", "0"), "spin") ||
        check(ort->AddSessionConfigEntry(so, "session.inter_op.allow_spinning", "0"), "spin");
    OrtStatus *st = opts_bad ? NULL : ort->CreateSession(ort_env, path, so, &p->session);
    ort->ReleaseSessionOptions(so);
    if (opts_bad || check(st, path))
        goto fail;

    OrtAllocator *al;
    if (check(ort->GetAllocatorWithDefaultOptions(&al), "allocator"))
        goto fail;

    /* names: input 0 = spec, 1 = state; output 0 = enhanced spec, 1 = state */
    char *n;
    if (check(ort->SessionGetInputName(p->session, 0, al, &n), "input 0"))
        goto fail;
    p->name_spec_in = strdup(n), al->Free(al, n);
    if (check(ort->SessionGetInputName(p->session, 1, al, &n), "input 1"))
        goto fail;
    p->name_state_in = strdup(n), al->Free(al, n);
    if (check(ort->SessionGetOutputName(p->session, 0, al, &n), "output 0"))
        goto fail;
    p->name_spec_out = strdup(n), al->Free(al, n);
    if (check(ort->SessionGetOutputName(p->session, 1, al, &n), "output 1"))
        goto fail;
    p->name_state_out = strdup(n), al->Free(al, n);

    /* geometry and initial state from the model metadata */
    OrtModelMetadata *md;
    if (check(ort->SessionGetModelMetadata(p->session, &md), "metadata"))
        goto fail;
    char *m_sr = meta(md, al, "sample_rate");
    char *m_fft = meta(md, al, "n_fft");
    char *m_state = meta(md, al, "state_size");
    char *m_erb_n = meta(md, al, "erb_norm_state_size");
    char *m_spec_n = meta(md, al, "spec_norm_state_size");
    char *m_erb = meta(md, al, "erb_norm_init");
    char *m_spec = meta(md, al, "spec_norm_init");
    int ok = m_sr && m_fft && m_state && m_erb_n && m_spec_n && m_erb && m_spec;
    int erb_n = 0, spec_n = 0;
    if (ok) {
        p->win = atoi(m_fft);
        p->hop = p->win / 2;
        p->bins = p->win / 2 + 1;
        p->state_len = atoi(m_state);
        erb_n = atoi(m_erb_n);
        spec_n = atoi(m_spec_n);
        ok = p->win > 0 && p->win <= 8192 && p->state_len >= erb_n + spec_n && erb_n > 0 &&
             spec_n > 0;
    }
    if (!ok) {
        fprintf(stderr, "dpdfnet_ladspa: %s lacks the streaming metadata\n", path);
    } else if ((unsigned long)atol(m_sr) != rate) {
        fprintf(stderr, "dpdfnet_ladspa: model is %s Hz, host is %lu Hz\n", m_sr, rate);
        ok = 0;
    }
    if (ok) {
        p->state[0] = calloc(p->state_len, sizeof(float));
        p->state[1] = calloc(p->state_len, sizeof(float));
        if (!p->state[0] || !p->state[1] ||
            parse_floats(m_erb, p->state[0], erb_n) ||
            parse_floats(m_spec, p->state[0] + erb_n, spec_n)) {
            fprintf(stderr, "dpdfnet_ladspa: bad state metadata\n");
            ok = 0;
        }
    }
    free(m_sr); free(m_fft); free(m_state); free(m_erb_n); free(m_spec_n);
    free(m_erb); free(m_spec);
    ort->ReleaseModelMetadata(md);
    if (!ok)
        goto fail;

    /* Vorbis window, as in the reference */
    p->window = malloc(p->win * sizeof(float));
    for (int i = 0; i < p->win; i++) {
        double s = sin(0.5 * M_PI * (i + 0.5) / (p->win / 2.0));
        p->window[i] = (float)sin(0.5 * M_PI * s * s);
    }

    p->fft_time = calloc(p->win, sizeof(float));
    p->fft_freq = calloc(p->bins, sizeof(kiss_fft_cpx));
    p->inbuf = calloc(p->win, sizeof(float));
    p->olabuf = calloc(p->win, sizeof(float));
    p->hist = calloc((size_t)(LOOKAHEAD + 1) * p->bins * 2, sizeof(float));
    p->spec_in = calloc(p->bins * 2, sizeof(float));
    p->spec_out = calloc(p->bins * 2, sizeof(float));
    unsigned long cap = 1;
    while (cap < (unsigned long)(p->win + CHUNK + p->hop) * 2)
        cap <<= 1;
    p->fifo = calloc(cap, sizeof(float));
    p->fifo_mask = cap - 1;
    p->fifo_wr = p->win; /* start-up silence: a constant delay of one window */
    if (p->debug) {
        p->times_cap = 1000000;
        p->times = malloc(p->times_cap * sizeof(double));
        p->times_wall = malloc(p->times_cap * sizeof(double));
    }
    if (!p->fft_time || !p->fft_freq || !p->inbuf || !p->olabuf || !p->hist ||
        !p->spec_in || !p->spec_out || !p->fifo || !p->window ||
        (p->debug && (!p->times || !p->times_wall)))
        goto fail;
    /* kissfft (BSD-3): one configuration per instance, because it keeps scratch
     * space inside. It needs an even length; the model's n_fft is. */
    p->fwd = kiss_fftr_alloc(p->win, 0, NULL, NULL);
    p->inv = kiss_fftr_alloc(p->win, 1, NULL, NULL);
    if (!p->fwd || !p->inv)
        goto fail;

    /* tensors over our own buffers; no allocation per frame */
    if (check(ort->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &p->meminfo),
              "meminfo"))
        goto fail;
    int64_t spec_shape[4] = {1, 1, p->bins, 2};
    int64_t state_shape[1] = {p->state_len};
    size_t spec_bytes = (size_t)p->bins * 2 * sizeof(float);
    size_t state_bytes = (size_t)p->state_len * sizeof(float);
    if (check(ort->CreateTensorWithDataAsOrtValue(p->meminfo, p->spec_in, spec_bytes,
                  spec_shape, 4, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &p->v_spec_in), "spec in") ||
        check(ort->CreateTensorWithDataAsOrtValue(p->meminfo, p->spec_out, spec_bytes,
                  spec_shape, 4, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &p->v_spec_out), "spec out") ||
        check(ort->CreateTensorWithDataAsOrtValue(p->meminfo, p->state[0], state_bytes,
                  state_shape, 1, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &p->v_state[0]), "state 0") ||
        check(ort->CreateTensorWithDataAsOrtValue(p->meminfo, p->state[1], state_bytes,
                  state_shape, 1, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &p->v_state[1]), "state 1"))
        goto fail;
    p->cur = 0;

    /* Two throw-away runs allocate ONNX Runtime's working memory now, outside
     * the audio thread; then the initial state is restored. */
    {
        p->init_state = malloc(state_bytes);
        if (!p->init_state)
            goto fail;
        memcpy(p->init_state, p->state[0], state_bytes);
        const char *in_names[2] = {p->name_spec_in, p->name_state_in};
        const char *out_names[2] = {p->name_spec_out, p->name_state_out};
        for (int i = 0; i < 2; i++) {
            const OrtValue *ins[2] = {p->v_spec_in, p->v_state[p->cur]};
            OrtValue *outs[2] = {p->v_spec_out, p->v_state[p->cur ^ 1]};
            int bad = check(ort->Run(p->session, NULL, in_names, ins, 2, out_names, 2, outs),
                            "warm-up");
            p->cur ^= 1;
            if (bad)
                goto fail;
        }
        memcpy(p->state[0], p->init_state, state_bytes);
        p->cur = 0;
    }
    memset(p->spec_in, 0, spec_bytes);
    return p;

fail:
    destroy(p);
    return NULL;
}

static void connect_port(LADSPA_Handle h, unsigned long port, LADSPA_Data *loc)
{
    Plugin *p = h;
    switch (port) {
    case PORT_IN: p->in = loc; break;
    case PORT_OUT: p->out = loc; break;
    case PORT_ATTEN: p->atten = loc; break;
    case PORT_LATENCY: p->latency = loc; break;
    }
}

/* One model frame: p->inbuf holds win samples. Pushes hop samples to the FIFO.
 * Returns 0, or -1 when the model failed. */
static int run_frame(Plugin *p, float atten_lin)
{
    for (int i = 0; i < p->win; i++)
        p->fft_time[i] = p->inbuf[i] * p->window[i];
    kiss_fftr(p->fwd, p->fft_time, p->fft_freq);
    memcpy(p->spec_in, p->fft_freq, (size_t)p->bins * 2 * sizeof(float));
    memcpy(p->hist + (size_t)(p->frame_no % (LOOKAHEAD + 1)) * p->bins * 2, p->spec_in,
           (size_t)p->bins * 2 * sizeof(float));

    const char *in_names[2] = {p->name_spec_in, p->name_state_in};
    const char *out_names[2] = {p->name_spec_out, p->name_state_out};
    const OrtValue *ins[2] = {p->v_spec_in, p->v_state[p->cur]};
    OrtValue *outs[2] = {p->v_spec_out, p->v_state[p->cur ^ 1]};
    double t0 = 0, w0 = 0;
    if (p->debug) {
        t0 = clock_ms(CLOCK_THREAD_CPUTIME_ID);
        w0 = clock_ms(CLOCK_MONOTONIC);
    }
    OrtStatus *st = ort->Run(p->session, NULL, in_names, ins, 2, out_names, 2, outs);
    if (st) {
        if (p->logged++ < 5)
            check(st, "Run");
        else
            ort->ReleaseStatus(st);
        return -1;
    }
    if (p->debug && p->ntimes < p->times_cap) {
        p->times[p->ntimes] = clock_ms(CLOCK_THREAD_CPUTIME_ID) - t0;
        p->times_wall[p->ntimes++] = clock_ms(CLOCK_MONOTONIC) - w0;
    }
    p->cur ^= 1;

    /* A NaN or Inf in the state would stay there for good, because the state
     * feeds back into the next frame. Start the stream over instead. */
    for (int i = 0; i < p->bins * 2; i++) {
        if (!isfinite(p->spec_out[i])) {
            if (p->logged++ < 5)
                fprintf(stderr, "dpdfnet_ladspa: non-finite model output, state reset\n");
            memcpy(p->state[p->cur], p->init_state, (size_t)p->state_len * sizeof(float));
            memset(p->spec_out, 0, (size_t)p->bins * 2 * sizeof(float));
            break;
        }
    }

    /* Attenuation limit: mix the noisy frame that belongs to this output frame
     * back in. With 100 dB the weight is 1e-5, so the output is the model's. */
    if (atten_lin > 0.0f && p->frame_no >= LOOKAHEAD) {
        const float *noisy =
            p->hist + (size_t)((p->frame_no - LOOKAHEAD) % (LOOKAHEAD + 1)) * p->bins * 2;
        for (int i = 0; i < p->bins * 2; i++)
            p->spec_out[i] = atten_lin * noisy[i] + (1.0f - atten_lin) * p->spec_out[i];
    }
    p->frame_no++;

    memcpy(p->fft_freq, p->spec_out, (size_t)p->bins * 2 * sizeof(float));
    kiss_fftri(p->inv, p->fft_freq, p->fft_time);
    const float norm = 1.0f / (float)p->win;
    for (int i = 0; i < p->win; i++)
        p->olabuf[i] += p->fft_time[i] * norm * p->window[i];

    /* the Vorbis window is COLA at 50 % overlap: the first hop is final */
    for (int i = 0; i < p->hop; i++)
        p->fifo[(p->fifo_wr++) & p->fifo_mask] = p->olabuf[i];
    memmove(p->olabuf, p->olabuf + p->hop, (size_t)(p->win - p->hop) * sizeof(float));
    memset(p->olabuf + (p->win - p->hop), 0, (size_t)p->hop * sizeof(float));
    memmove(p->inbuf, p->inbuf + p->hop, (size_t)(p->win - p->hop) * sizeof(float));
    p->incount -= p->hop;
    return 0;
}

/* After a model failure: drop what is half processed, so the next frames do
 * not play old audio. The output FIFO restarts with one window of silence. */
static void reset_stream(Plugin *p)
{
    memset(p->olabuf, 0, (size_t)p->win * sizeof(float));
    p->incount = 0;
    p->fifo_rd = p->fifo_wr;
    for (int i = 0; i < p->win; i++)
        p->fifo[(p->fifo_wr++) & p->fifo_mask] = 0.0f;
}

static void run(LADSPA_Handle h, unsigned long n)
{
    Plugin *p = h;
#if defined(__SSE__)
    unsigned int csr = _mm_getcsr();
    _mm_setcsr(csr | 0x8040); /* flush denormals to zero */
#endif
    if (p->latency)
        *p->latency = (float)(p->win + LOOKAHEAD * p->hop);
    float atten_lin = 0.0f;
    if (p->atten) {
        float db = *p->atten;
        db = db < 0.0f ? 0.0f : db > 100.0f ? 100.0f : db;
        atten_lin = powf(10.0f, -db / 20.0f);
    }

    unsigned long done = 0;
    while (done < n) {
        unsigned long c = n - done < CHUNK ? n - done : CHUNK;
        int failed = 0;
        for (unsigned long i = 0; i < c && !failed; i++) {
            float x = p->in[done + i];
            p->inbuf[p->incount++] = isfinite(x) ? x : 0.0f; /* a NaN would poison the state */
            if (p->incount == p->win && run_frame(p, atten_lin) < 0)
                failed = 1;
        }
        if (failed) {
            /* model failed: pass the input through rather than go silent */
            for (unsigned long k = 0; k < c; k++)
                p->out[done + k] = p->in[done + k];
            reset_stream(p);
        } else {
            for (unsigned long i = 0; i < c; i++)
                p->out[done + i] = p->fifo_rd == p->fifo_wr
                                       ? 0.0f /* cannot happen after start-up */
                                       : p->fifo[(p->fifo_rd++) & p->fifo_mask];
        }
        done += c;
    }
#if defined(__SSE__)
    _mm_setcsr(csr);
#endif
}

static int cmp_double(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static void report(const char *what, double *v, unsigned long n, double budget)
{
    qsort(v, n, sizeof(double), cmp_double);
    double sum = 0;
    for (unsigned long i = 0; i < n; i++)
        sum += v[i];
    fprintf(stderr,
            "dpdfnet_ladspa: %s per frame (ms): mean %.2f p50 %.2f p95 %.2f p99 %.2f max %.2f"
            " (budget %.1f)\n",
            what, sum / n, v[n / 2], v[n * 95 / 100], v[n * 99 / 100], v[n - 1], budget);
}

static void cleanup(LADSPA_Handle h)
{
    Plugin *p = h;
    if (p->debug && p->ntimes) {
        double budget = 1e3 * p->hop / p->rate;
        fprintf(stderr, "dpdfnet_ladspa: %lu frames\n", p->ntimes);
        report("CPU time ", p->times, p->ntimes, budget);
        report("wall time", p->times_wall, p->ntimes, budget);
    }
    destroy(p);
}

static const char *port_names[] = {"Input", "Output", "Attenuation limit (dB)", "latency"};
static const LADSPA_PortDescriptor port_desc[] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_CONTROL,
};
static const LADSPA_PortRangeHint port_hints[] = {
    {0, 0, 0},
    {0, 0, 0},
    {LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_DEFAULT_MAXIMUM, 0, 100},
    {LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_INTEGER, 0, 65536},
};

static const LADSPA_Descriptor descriptor = {
    .UniqueID = 4770001, /* private, not registered with the LADSPA ID registry */
    .Label = "dpdfnet_mono",
    .Properties = 0, /* runs ONNX Runtime in the callback: not certified hard-RT */
    .Name = "DPDFNet speech enhancer (mono)",
    .Maker = "meros/nixos",
    .Copyright = "MIT; DPDFNet model weights are Apache-2.0 (Ceva)",
    .PortCount = 4,
    .PortDescriptors = port_desc,
    .PortNames = port_names,
    .PortRangeHints = port_hints,
    .instantiate = instantiate,
    .connect_port = connect_port,
    .run = run,
    .cleanup = cleanup,
};

const LADSPA_Descriptor *ladspa_descriptor(unsigned long index)
{
    return index == 0 ? &descriptor : NULL;
}
