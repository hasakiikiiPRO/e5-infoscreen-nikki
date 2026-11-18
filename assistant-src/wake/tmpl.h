// The owner's own wake words: their recordings (the app's 录唤醒词样本, 48 kHz WAVs) as
// templates, matched by subsequence DTW on log-mel cepstra -- the generic model does not
// hear every voice and accent (it heard one owner's 「贾维斯」 once in ten), the recordings of
// the voice itself do.  The cepstra have their running mean taken out (the room, the
// microphone, the loudness: no gain needed), 13 per 10 ms; a template is the loud part of a
// recording, with 3 s of synthetic room noise before it so the running mean is as in use.
// A word is heard when a path through one of its templates, ending now, costs less than its
// threshold per step.  Measured on one owner: their own word 14-23, their other word 25-44,
// 3 h of podcasts and their own recordings no lower than ~55.
#include <dirent.h>

#define T_SR 16000
#define T_WIN 400
#define T_HOP 160
#define T_NFFT 512
#define T_NMEL 40
#define T_NCEP 13
#define T_CMN 300.0f        // (frames: ~3 s running mean)
#define T_MAXT 24           // (templates per word)
#define T_MAXW 2
#define T_REFRACT 100       // (frames after a hit before another: 1 s)

typedef struct {
  float fb[T_NMEL][T_NFFT / 2 + 1], dct[T_NCEP][T_NMEL], ham[T_WIN];
} TFront;

typedef struct {
  float buf[T_WIN];         // (the last T_WIN samples at 16 kHz, a ring: pos the oldest)
  int pos, fill, since;     // (samples in buf; samples since the last frame)
  float mean[T_NCEP];
  int started;
} TStream;

typedef struct {
  float *t;                 // len x T_NCEP
  int len;
  float *d, *l;             // the DTW's last column: cost, path length
} TTmpl;

typedef struct {
  char name[64];
  float thr;
  TTmpl tm[T_MAXT];
  int n;
} TWord;

static TFront tf;

static void t_front_init(void) {
  float lo = 2595 * log10f(1 + 80 / 700.0f), hi = 2595 * log10f(1 + 7600 / 700.0f);
  int bins[T_NMEL + 2];
  for (int i = 0; i < T_NMEL + 2; i++) {
    float m = lo + (hi - lo) * i / (T_NMEL + 1);
    float hz = 700 * (powf(10, m / 2595) - 1);
    bins[i] = (int)floorf((T_NFFT + 1) * hz / T_SR);
  }
  memset(tf.fb, 0, sizeof tf.fb);
  for (int i = 0; i < T_NMEL; i++) {
    int a = bins[i], b = bins[i + 1], c = bins[i + 2];
    for (int k = a; k < b; k++) tf.fb[i][k] = (float)(k - a) / (b - a > 0 ? b - a : 1);
    for (int k = b; k < c; k++) tf.fb[i][k] = (float)(c - k) / (c - b > 0 ? c - b : 1);
  }
  for (int j = 0; j < T_NCEP; j++)
    for (int i = 0; i < T_NMEL; i++) tf.dct[j][i] = cosf((float)M_PI / T_NMEL * (i + 0.5f) * (j + 1));
  for (int i = 0; i < T_WIN; i++) tf.ham[i] = 0.54f - 0.46f * cosf(2 * (float)M_PI * i / (T_WIN - 1));
}

// in-place radix-2 FFT, n a power of two
static void t_fft(float *re, float *im, int n) {
  for (int i = 1, j = 0; i < n; i++) {
    int bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      float t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (int len = 2; len <= n; len <<= 1) {
    float ang = -2 * (float)M_PI / len, wr = cosf(ang), wi = sinf(ang);
    for (int i = 0; i < n; i += len) {
      float cr = 1, ci = 0;
      for (int k = 0; k < len / 2; k++) {
        int a = i + k, b = i + k + len / 2;
        float xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
        re[b] = re[a] - xr; im[b] = im[a] - xi;
        re[a] += xr; im[a] += xi;
        float t = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = t;
      }
    }
  }
}

// one frame of cepstra from the T_WIN samples in w (oldest first); its energy (rms) in *energy
static void t_frame(const float *w, float *cep, float *energy) {
  static float re[T_NFFT], im[T_NFFT];
  float e = 0;
  for (int i = 0; i < T_NFFT; i++) {
    re[i] = i < T_WIN ? w[i] * tf.ham[i] : 0;
    im[i] = 0;
    if (i < T_WIN) e += w[i] * w[i];
  }
  *energy = sqrtf(e / T_WIN);
  t_fft(re, im, T_NFFT);
  float mel[T_NMEL];
  for (int m = 0; m < T_NMEL; m++) {
    float s = 0;
    for (int k = 0; k <= T_NFFT / 2; k++)
      if (tf.fb[m][k] != 0) s += tf.fb[m][k] * (re[k] * re[k] + im[k] * im[k]);
    mel[m] = logf(s + 1e-6f);
  }
  for (int j = 0; j < T_NCEP; j++) {
    float s = 0;
    for (int m = 0; m < T_NMEL; m++) s += tf.dct[j][m] * mel[m];
    cep[j] = s;
  }
}

// samples at 16 kHz in; each finished frame (mean taken out) to emit(cep, energy, arg)
static void t_feed(TStream *st, const float *x, int n, void (*emit)(const float *, float, void *), void *arg) {
  float w[T_WIN];
  for (int i = 0; i < n; i++) {
    st->buf[st->pos] = x[i];
    st->pos = (st->pos + 1) % T_WIN;
    if (st->fill < T_WIN) st->fill++;
    if (++st->since < T_HOP || st->fill < T_WIN) continue;
    st->since = 0;
    memcpy(w, st->buf + st->pos, (size_t)(T_WIN - st->pos) * sizeof(float));
    memcpy(w + (T_WIN - st->pos), st->buf, (size_t)st->pos * sizeof(float));
    float cep[T_NCEP], e;
    t_frame(w, cep, &e);
    for (int j = 0; j < T_NCEP; j++) {
      if (!st->started) st->mean[j] = cep[j];
      else st->mean[j] = st->mean[j] * (1 - 1 / T_CMN) + cep[j] / T_CMN;
      cep[j] -= st->mean[j];
    }
    st->started = 1;
    emit(cep, e, arg);
  }
}

// ---- templates from WAVs ----

typedef struct { float *cep, *en; int n, cap; } TCollect;

static void t_collect(const float *cep, float e, void *arg) {
  TCollect *c = arg;
  if (c->n == c->cap) {
    c->cap = c->cap ? c->cap * 2 : 512;
    c->cep = realloc(c->cep, (size_t)c->cap * T_NCEP * sizeof(float));
    c->en = realloc(c->en, (size_t)c->cap * sizeof(float));
  }
  memcpy(c->cep + (size_t)c->n * T_NCEP, cep, T_NCEP * sizeof(float));
  c->en[c->n++] = e;
}

static int t_cmpf(const void *a, const void *b) {
  float x = *(const float *)a, y = *(const float *)b;
  return x < y ? -1 : x > y;
}

// 16-bit mono 48 kHz WAV -> samples at 16 kHz (three averaged, as the stream); NULL if not that
static float *t_read_wav(const char *path, int *n16) {
  FILE *f = fopen(path, "rb");
  if (!f) return NULL;
  unsigned char h[12];
  float *out = NULL;
  if (fread(h, 1, 12, f) == 12 && !memcmp(h, "RIFF", 4) && !memcmp(h + 8, "WAVE", 4)) {
    unsigned char ch[8], fmt[16];
    int good = 0;           // (fmt: PCM, mono, 48 kHz, 16-bit -- what enroll.py writes)
    while (fread(ch, 1, 8, f) == 8) {
      uint32_t sz = ch[4] | ch[5] << 8 | ch[6] << 16 | (uint32_t)ch[7] << 24;
      if (!memcmp(ch, "fmt ", 4) && sz >= 16 && fread(fmt, 1, 16, f) == 16) {
        good = fmt[0] == 1 && fmt[2] == 1 && (fmt[4] | fmt[5] << 8 | fmt[6] << 16) == 48000 && fmt[14] == 16;
        fseek(f, (long)(sz - 16 + (sz & 1)), SEEK_CUR);
        continue;
      }
      if (memcmp(ch, "data", 4)) { fseek(f, (long)(sz + (sz & 1)), SEEK_CUR); continue; }
      if (!good) break;
      int ns = (int)(sz / 2), m = ns / 3;
      int16_t *pcm = malloc(sz ? sz : 1);
      if (pcm && fread(pcm, 1, sz, f) == sz && m > 0) {
        out = malloc((size_t)m * sizeof(float));
        for (int i = 0; i < m; i++) out[i] = (pcm[3 * i] + pcm[3 * i + 1] + pcm[3 * i + 2]) / (3 * 32768.0f);
        *n16 = m;
      }
      free(pcm);
      break;
    }
  }
  fclose(f);
  return out;
}

// the stream after 3 s of the room (rms 45 of 32768, Gaussian, fixed seed): every template starts
// from it, so the running mean is as in use (the same for all: computed once)
static TStream t_prefix;
static int t_prefix_frames, t_prefix_done;

static void t_count(const float *cep, float e, void *arg) { (void)cep; (void)e; ++*(int *)arg; }

static void t_prefix_init(void) {
  int nn = 3 * T_SR;
  float *all = malloc((size_t)nn * sizeof(float));
  uint32_t seed = 12345;
  for (int i = 0; i < nn; i += 2) {
    seed = seed * 1664525u + 1013904223u; float u1 = ((seed >> 8) + 1) / 16777217.0f;
    seed = seed * 1664525u + 1013904223u; float u2 = (seed >> 8) / 16777216.0f;
    float r = sqrtf(-2 * logf(u1)) * 45 / 32768.0f / sqrtf(3.0f);   // (three averaged: rms down by sqrt 3)
    all[i] = r * cosf(2 * (float)M_PI * u2);
    if (i + 1 < nn) all[i + 1] = r * sinf(2 * (float)M_PI * u2);
  }
  memset(&t_prefix, 0, sizeof t_prefix);
  t_prefix_frames = 0;
  t_feed(&t_prefix, all, nn, t_count, &t_prefix_frames);
  free(all);
  t_prefix_done = 1;
}

#define T_TAIL_FRAMES 80    // (enroll.py keeps 1.3 s of quiet after the word: its last 0.8 s, the room)

static int t_template(TTmpl *tm, const char *path) {
  int n16 = 0;
  float *x = t_read_wav(path, &n16);
  if (!x) return 0;
  if (!t_prefix_done) t_prefix_init();
  TStream st = t_prefix;
  TCollect c = {0};
  t_feed(&st, x, n16, t_collect, &c);
  free(x);
  int skip = 0, ok = 0;
  if (c.n > T_TAIL_FRAMES + 25) {
    // the loud part: above 3x the room (the median of the quiet after the word -- not of the
    // whole, which a slowly said word fills) and 15 % of the loudest, 3 frames either side
    int m = c.n - skip;
    float *e = malloc(T_TAIL_FRAMES * sizeof(float)), mx = 0;
    for (int i = 0; i < m; i++) mx = fmaxf(mx, c.en[skip + i]);
    memcpy(e, c.en + c.n - T_TAIL_FRAMES, T_TAIL_FRAMES * sizeof(float));
    qsort(e, T_TAIL_FRAMES, sizeof(float), t_cmpf);
    float lim = fmaxf(e[T_TAIL_FRAMES / 2] * 3, mx * 0.15f);
    free(e);
    int a = -1, b = -1;
    for (int i = 0; i < m; i++)
      if (c.en[skip + i] > lim) { if (a < 0) a = i; b = i; }
    if (a >= 0) {
      a = skip + a - 3 < skip ? skip : skip + a - 3;
      b = skip + b + 4 > c.n ? c.n : skip + b + 4;
      tm->len = b - a;
      if (tm->len >= 25) {   // (shorter: a recording cut short, not the word)
        tm->t = malloc((size_t)tm->len * T_NCEP * sizeof(float));
        memcpy(tm->t, c.cep + (size_t)a * T_NCEP, (size_t)tm->len * T_NCEP * sizeof(float));
        tm->d = malloc((size_t)tm->len * sizeof(float));
        tm->l = malloc((size_t)tm->len * sizeof(float));
        for (int i = 0; i < tm->len; i++) { tm->d[i] = INFINITY; tm->l[i] = 0; }
        ok = 1;
      }
    }
  }
  free(c.cep);
  free(c.en);
  return ok;
}

// "NAME=DIR:THRESHOLD": the word's templates from DIR/*.wav
static int t_word(TWord *w, const char *spec) {
  memset(w, 0, sizeof *w);
  const char *eq = strchr(spec, '='), *colon = strrchr(spec, ':');
  if (!eq || !colon || colon < eq) return 0;
  snprintf(w->name, sizeof w->name, "%.*s", (int)(eq - spec), spec);
  char dir[512];
  snprintf(dir, sizeof dir, "%.*s", (int)(colon - eq - 1), eq + 1);
  w->thr = (float)atof(colon + 1);
  // (the newest first: names start with the time they were recorded)
  struct dirent **list = NULL;
  int k = scandir(dir, &list, NULL, alphasort);
  for (int i = k - 1; i >= 0; i--) {
    size_t l = strlen(list[i]->d_name);
    if (w->n < T_MAXT && l >= 5 && !strcmp(list[i]->d_name + l - 4, ".wav")) {
      char p[1024];
      snprintf(p, sizeof p, "%s/%s", dir, list[i]->d_name);
      if (t_template(&w->tm[w->n], p)) w->n++;
    }
    free(list[i]);
  }
  free(list);
  return w->n;
}

// one input frame through every template of a word -> the best cost per step of a path ending now
static float t_step(TWord *w, const float *x) {
  float best = INFINITY;
  for (int k = 0; k < w->n; k++) {
    TTmpl *tm = &w->tm[k];
    float pd = 0, pl = 0;   // (the column before, at j-1: a path may start anywhere)
    for (int j = 0; j < tm->len; j++) {
      const float *t = tm->t + (size_t)j * T_NCEP;
      float c = 0;
      for (int i = 0; i < T_NCEP; i++) { float v = t[i] - x[i]; c += v * v; }
      c = sqrtf(c);
      float od = tm->d[j], ol = tm->l[j];
      int diag = pd <= od;
      tm->d[j] = (diag ? pd : od) + c;
      tm->l[j] = (diag ? pl : ol) + 1;
      pd = od; pl = ol;
    }
    // (a path held on the template's last frames through the quiet after a word that was not it
    // would average its cost down: no longer than twice the template)
    if (tm->l[tm->len - 1] <= tm->len * 2.0f) best = fminf(best, tm->d[tm->len - 1] / tm->l[tm->len - 1]);
  }
  return best;
}

static void t_reset(TWord *w) {
  for (int k = 0; k < w->n; k++)
    for (int j = 0; j < w->tm[k].len; j++) { w->tm[k].d[j] = INFINITY; w->tm[k].l[j] = 0; }
}
