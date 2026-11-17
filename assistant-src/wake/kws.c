// e5-kws: the wake words, spotted in a PCM stream (sherpa-onnx's keyword spotter, C API).
//
//   e5-kws MODEL_DIR KEYWORDS_FILE THRESHOLD RATE < S16LE mono PCM
//
// The E5's microphone is quiet (speech peaks ~0.01-0.1 of full scale) and the model wants
// speech at an ordinary level: an automatic gain brings the loudest of the last seconds to
// TARGET (at most MAX_GAIN, so a quiet room is not made into speech; down at once for a
// loud sound, back up slowly -- halved per HALF_S).
//
// Prints one line per wake word heard, at once:  HIT <keyword> <samples read so far>
// (the sample count lets the caller cut the speech that follows out of its own copy of the
// stream).  "READY" once the model is loaded.  Ends at the end of its input.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "c-api.h"

#define TARGET 0.3f
#define MAX_GAIN 32.0f
#define HALF_S 2.0f

int main(int argc, char **argv) {
  if (argc != 5) {
    fprintf(stderr, "usage: %s MODEL_DIR KEYWORDS_FILE THRESHOLD RATE < s16le\n", argv[0]);
    return 2;
  }
  const char *dir = argv[1];
  char enc[512], dec[512], join[512], tok[512];
  snprintf(enc, sizeof enc, "%s/encoder.int8.onnx", dir);
  snprintf(dec, sizeof dec, "%s/decoder.onnx", dir);
  snprintf(join, sizeof join, "%s/joiner.int8.onnx", dir);
  snprintf(tok, sizeof tok, "%s/tokens.txt", dir);
  int rate = atoi(argv[4]);

  SherpaOnnxKeywordSpotterConfig c;
  memset(&c, 0, sizeof c);
  c.feat_config.sample_rate = 16000;
  c.feat_config.feature_dim = 80;
  c.model_config.transducer.encoder = enc;
  c.model_config.transducer.decoder = dec;
  c.model_config.transducer.joiner = join;
  c.model_config.tokens = tok;
  c.model_config.num_threads = 1;
  c.model_config.provider = "cpu";
  c.max_active_paths = 4;
  c.num_trailing_blanks = 1;
  c.keywords_score = 1.0f;
  c.keywords_threshold = (float)atof(argv[3]);
  c.keywords_file = argv[2];

  const SherpaOnnxKeywordSpotter *ks = SherpaOnnxCreateKeywordSpotter(&c);
  if (!ks) {
    fprintf(stderr, "e5-kws: the model did not load\n");
    return 1;
  }
  const SherpaOnnxOnlineStream *s = SherpaOnnxCreateKeywordStream(ks);
  printf("READY\n");
  fflush(stdout);

  enum { BLOCK = 4800 };  // (0.1 s at 48 kHz)
  int16_t raw[BLOCK];
  float f[BLOCK];
  size_t have = 0;  // (bytes of raw filled: reads come in any size)
  long long total = 0;
  float env = 1e-3f;  // (the loudest lately, full scale = 1)
  for (;;) {
    ssize_t n = read(0, (char *)raw + have, sizeof raw - have);
    if (n <= 0) break;
    have += (size_t)n;
    size_t k = have / 2;
    // (0.1 s at a time: the microphone's 20 ms reads one by one cost the E5 three times the CPU)
    if (have < sizeof raw) continue;
    // 48 kHz: down to the model's 16 kHz here, three samples averaged (sherpa's own resampler
    // costs more than the model itself on the E5); the rest of a three goes to the next block
    int down = rate == 48000 ? 3 : 1;
    size_t use = k / down * down, m = 0;
    for (size_t i = 0; i < use; i += down) {
      int sum = 0;
      for (int j = 0; j < down; j++) sum += raw[i + j];
      f[m++] = sum / (32768.0f * down);
    }
    // the gain: per block (0.1 s at most), the envelope decays between blocks
    float peak = 1e-4f;
    for (size_t i = 0; i < m; i++) peak = fmaxf(peak, fabsf(f[i]));
    env = fmaxf(env * powf(0.5f, (float)m / (rate / down) / HALF_S), peak);
    float gain = fminf(MAX_GAIN, TARGET / env);
    for (size_t i = 0; i < m; i++) f[i] = fmaxf(-1.0f, fminf(1.0f, f[i] * gain));
    total += (long long)use;
    size_t left = have - use * 2;  // (bytes not used yet: part of a three, an odd byte)
    memmove(raw, (char *)raw + use * 2, left);
    have = left;
    SherpaOnnxOnlineStreamAcceptWaveform(s, rate / down, f, (int32_t)m);
    while (SherpaOnnxIsKeywordStreamReady(ks, s)) {
      SherpaOnnxDecodeKeywordStream(ks, s);
      const SherpaOnnxKeywordResult *r = SherpaOnnxGetKeywordResult(ks, s);
      if (r && r->keyword && r->keyword[0]) {
        printf("HIT %s %lld\n", r->keyword, total);
        fflush(stdout);
        SherpaOnnxResetKeywordStream(ks, s);
      }
      if (r) SherpaOnnxDestroyKeywordResult(r);
    }
  }
  SherpaOnnxDestroyOnlineStream(s);
  SherpaOnnxDestroyKeywordSpotter(ks);
  return 0;
}
