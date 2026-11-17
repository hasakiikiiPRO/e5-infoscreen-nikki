"""助手 (assistant): when a question has been said -- by the sound, no key held.

listen.py (every turn) and wake.py (the question after a wake word) give it the
microphone's 16-bit mono PCM at RATE as it comes; it keeps what was said, from where
the sound rises well above the room's noise to where it has been quiet QUIET_S.
"""
import array
import math
import time

RATE = 48000
CHUNK = 1440                            # 30 ms at 48 kHz, 16-bit mono: 2880 bytes
WAIT_S = 6                              # nothing said this long: there is no question
TURN_MAX_S = 30
QUIET_S = 1.3                           # this long quiet after speech: it is said (a pause between sentences is ~1.1 s)
SETTLE_S = 0.4                          # the motor's own pulse, heard by the microphone: not speech
START_CHUNKS = 5                        # 0.15 s loud in a row: speech has begun
MIN_LOUD = 8                            # under 0.24 s loud in all: a knock or a cough, not a question
PREROLL = 10                            # chunks kept from before the speech began (0.3 s)


def rms(b):
    a = array.array('h', b)
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


class Question:
    """feed() the PCM as it comes: None while it goes on, then the question's PCM --
    b'' when nothing was said within WAIT_S (or only a knock)."""

    def __init__(self, floor=None, preroll=PREROLL, settle=SETTLE_S):
        now = time.monotonic()
        self.end = now + TURN_MAX_S
        self.give_up = now + WAIT_S
        self.settle = now + settle
        # (the room's own noise: from the caller if it heard the room before, else the quietest seen)
        self.floor = floor
        self.preroll = preroll
        self.started, self.quiet, self.loud_n = False, 0, 0
        self.buf, self.pcm = bytearray(), bytearray()

    def feed(self, data):
        self.buf += data
        while len(self.buf) >= CHUNK * 2:
            chunk = bytes(self.buf[:CHUNK * 2])
            del self.buf[:CHUNK * 2]
            done = self._chunk(chunk)
            if done is not None:
                return done
        if time.monotonic() >= self.end:
            return bytes(self.pcm) if self.started else b''
        return None

    def _chunk(self, chunk):
        now = time.monotonic()
        if now < self.settle:
            return None                 # (the motor, not speech: not kept)
        self.pcm += chunk
        level = rms(chunk)
        self.floor = level if self.floor is None or level < self.floor else self.floor * 1.003
        self.floor = max(self.floor, 30.0)
        # (measured on this microphone: speech ~150-480, a quiet room ~25-100)
        loud = level > max(self.floor * 2.5, 110)
        if not self.started:
            del self.pcm[:-CHUNK * 2 * self.preroll]
            self.quiet = self.quiet + 1 if loud else 0
            if self.quiet >= START_CHUNKS:
                self.started, self.loud_n, self.quiet = True, self.quiet, 0
            elif now > self.give_up:
                return b''
            return None
        self.loud_n += loud
        self.quiet = 0 if loud else self.quiet + 1
        if self.quiet * CHUNK / RATE >= QUIET_S:
            if self.loud_n >= MIN_LOUD:
                return bytes(self.pcm)
            # (a blip: forget it, keep waiting for the question)
            self.started, self.quiet, self.loud_n = False, 0, 0
            del self.pcm[:-CHUNK * 2 * self.preroll]
        return None


def quietest(pcm):
    """The room's noise from PCM heard before (the quietest 30 ms of it), or None."""
    levels = [rms(pcm[i:i + CHUNK * 2]) for i in range(0, len(pcm) - CHUNK * 2 + 1, CHUNK * 2)]
    return min(levels) if levels else None
