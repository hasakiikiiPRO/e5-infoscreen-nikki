// e5-dumbres: screen-sized display buffers held in reserve, given back the moment the screen
// cannot get one.
//
// The E5's kernel has no CMA and no compaction: a screen buffer (sprd_gem_dumb_create, 600 KB)
// needs a whole free 1 MB block of ordinary memory, and after a day or two of programs starting
// and ending there may be none left -- the compositor (cage) then cannot draw: the backlight is
// on, the screen black, "failed to allocate buffer with size 614400/626688" over and over in
// the kernel log, until a reboot.  This takes a few such buffers while blocks are still whole,
// holds them (as a DRM client on /dev/dri/card0, nothing drawn into them), watches the kernel
// log, and on that error frees all of them: cage, retrying, gets the freed blocks (it needs
// three at once to start: one short and it fails and starts again, for ever).  Every
// REFILL_S it tries to take back up to KEEP while memory allows.
//
//   e5-dumbres [KEEP]
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

struct cd { uint32_t height, width, bpp, flags, handle, pitch; uint64_t size; };
struct dd { uint32_t handle; };
#define CREATE _IOWR('d', 0xB2, struct cd)
#define DESTROY _IOWR('d', 0xB4, struct dd)
#define DROP_MASTER _IO('d', 0x1f)
#define MAXKEEP 16
#define REFILL_S 60

static int drm, held;
static uint32_t h[MAXKEEP];

static int take(void) {
  // (the screen's own size: 320 x 480, 32 bpp -- what cage asks for)
  struct cd c = { 480, 320, 32, 0, 0, 0, 0 };
  if (ioctl(drm, CREATE, &c)) return 0;
  h[held++] = c.handle;
  return 1;
}

static void give(int n) {
  while (n-- > 0 && held > 0) {
    struct dd d = { h[--held] };
    ioctl(drm, DESTROY, &d);
  }
}

// (our own takes that fail log the same error: what the log got meanwhile is not the screen's)
static void skip_log(int kmsg) { lseek(kmsg, 0, SEEK_END); }

static void say(const char *msg) {
  FILE *k = fopen("/dev/kmsg", "w");
  if (k) { fprintf(k, "e5-dumbres: %s (holding %d)\n", msg, held); fclose(k); }
}

int main(int argc, char **argv) {
  int keep = argc > 1 ? atoi(argv[1]) : 4;
  if (keep < 1 || keep > MAXKEEP) keep = 4;
  drm = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
  if (drm < 0) { perror("e5-dumbres: /dev/dri/card0"); return 1; }
  // (the first to open the device is its master: started before the screen, this would be,
  // and the compositor could not set the display -- "Swapchain failed test", a black screen.
  // Buffers need no master: let it go at once; not master already, it fails, harmlessly)
  ioctl(drm, DROP_MASTER, 0);
  int kmsg = open("/dev/kmsg", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
  if (kmsg < 0) { perror("e5-dumbres: /dev/kmsg"); return 1; }
  while (held < keep && take()) {}
  skip_log(kmsg);                      // (only what comes from now on)
  say("started");
  time_t last_refill = time(NULL), last_give = 0;
  char line[2048];
  for (;;) {
    struct pollfd p = { kmsg, POLLIN, 0 };
    poll(&p, 1, 5000);
    for (;;) {
      ssize_t n = read(kmsg, line, sizeof line - 1);
      if (n < 0) {
        if (errno == EPIPE) continue;  // (records overwritten: go on with the next)
        break;
      }
      line[n] = 0;
      if (strstr(line, "failed to allocate buffer")) {
        time_t now = time(NULL);
        if (held > 0 && now != last_give) {
          give(held);
          last_give = now;
          last_refill = now;   // (not taken back at once: the screen first)
          say("the screen could not get a buffer: given back");
        }
      }
    }
    time_t now = time(NULL);
    if (held < keep && now - last_refill >= REFILL_S) {
      last_refill = now;
      int before = held;
      while (held < keep && take()) {}
      skip_log(kmsg);
      if (held != before) say("taken back while memory allows");
    }
  }
}
