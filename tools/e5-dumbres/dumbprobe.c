// dumbprobe: how many screen-sized dumb buffers the display driver can still give (then all freed)
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <unistd.h>
struct cd { uint32_t height, width, bpp, flags, handle, pitch; uint64_t size; };
struct dd { uint32_t handle; };
#define CREATE _IOWR('d', 0xB2, struct cd)
#define DESTROY _IOWR('d', 0xB4, struct dd)
int main(int argc, char **argv) {
  int max = argc > 1 ? atoi(argv[1]) : 200;
  int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
  if (fd < 0) { perror("open"); return 1; }
  uint32_t h[1000]; int n = 0;
  for (; n < max && n < 1000; n++) {
    struct cd c = { 480, 320, 32, 0, 0, 0, 0 };
    if (ioctl(fd, CREATE, &c)) break;
    h[n] = c.handle;
  }
  for (int i = 0; i < n; i++) { struct dd d = { h[i] }; ioctl(fd, DESTROY, &d); }
  printf("%d\n", n);
  close(fd);
  return 0;
}
