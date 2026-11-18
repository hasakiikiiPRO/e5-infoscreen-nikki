#!/usr/bin/python3
"""助手 (assistant): your own voice saying a wake word, recorded -- so the wake words can be
fitted to it (each word's threshold, and the voiceprint that tells it is you).

  enroll.py <word: fish|jarvis> [count]

Run by backend.uc's POST /enroll from the app's page.  The microphone is asked for as the
assistant asks for it (WANT: the recorder lets go, and the wake service stops listening);
then, COUNT times: the motor pulses, the word is said, and what was said (vad.py: from where
the sound rises to where it falls quiet) is kept as <time>-<n>.wav, 48 kHz.  They are
written to SAMPLES/<word>.new and, at the end, with at least KEEP_MIN of them, take the place
of the word's old recordings at once (a new round is how a bad recording goes; the wake
service starts again once, on the whole set).  Too short, too long or nothing: said again (at
most COUNT * 2 + 5 tries).  Each step goes to
STATE for the page; the stop file (POST /enroll/stop) ends it.
"""
import importlib.util
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave

RUN = '/tmp/run/e5-assistant'
STATE, STOP, PIDF = RUN + '/enroll.json', RUN + '/enroll.stop', RUN + '/enroll.pid'
WANT = '/tmp/run/e5-assistant.want'
SAMPLES = '/mnt/e5-data/e5assist/enroll'
WORDS = {'fish': '大肥鱼', 'jarvis': '贾维斯'}
RATE = 48000
MIN_S, MAX_S = 0.35, 2.5                # (a wake word said: about 0.6-1.5 s)
KEEP_MIN = 3                            # (fewer: the old recordings stay)
GAP_S = 0.8                             # (between two: the voice settles, the pulse is not heard as the word)

sys.path.insert(0, '/opt/e5')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vad  # noqa: E402
spec = importlib.util.spec_from_file_location('e5_capture', '/opt/e5/e5-capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

ME = str(os.getpid())


def state(**kw):
    tmp = f'{STATE}.{ME}'
    with open(tmp, 'w') as f:
        json.dump({'at': time.time(), 'pid': os.getpid(), **kw}, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def stopped():
    return os.path.exists(STOP)


def call_waiting():
    try:
        return bool(capture.routing.pending_voice())
    except Exception:
        return False


def drop_mine(path):
    try:
        if open(path).read().strip() == ME:
            os.unlink(path)
    except OSError:
        pass


def keep_want(stop):
    while not stop.wait(10):
        try:
            os.utime(WANT)
        except OSError:
            return


def save(pcm, word, n):
    d = f'{SAMPLES}/{word}.new'
    os.makedirs(d, exist_ok=True)
    path = f'{d}/{time.strftime("%Y%m%d-%H%M%S")}-{n:02d}.wav'
    with wave.open(path + '.part', 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    os.replace(path + '.part', path)
    return path


def replace_old(word):
    """The new round in place of the old recordings, all at once."""
    new, cur, old = f'{SAMPLES}/{word}.new', f'{SAMPLES}/{word}', f'{SAMPLES}/{word}.old'
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(cur):
        os.rename(cur, old)
    os.rename(new, cur)
    shutil.rmtree(old, ignore_errors=True)


def quietly(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def main():
    word = sys.argv[1] if len(sys.argv) > 1 else ''
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    if word not in WORDS:
        return state(phase='error', error='不认识的唤醒词')
    name = WORDS[word]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    fresh = threading.Event()
    cap, got, tries = None, 0, 0
    shutil.rmtree(f'{SAMPLES}/{word}.new', ignore_errors=True)
    try:
        with open(PIDF, 'w') as f:
            f.write(ME)
        with open(WANT, 'w') as f:
            f.write(ME)
        threading.Thread(target=keep_want, args=(fresh,), daemon=True).start()
        state(phase='starting', word=word, name=name, got=0, of=count)
        # (the recorder and the wake service let go within ~0.5 s of WANT)
        for _ in range(16):
            c = capture.Capture()
            try:
                started = c.start(None)
            except Exception:
                started = False
            if started:
                cap = c
                break
            quietly(c.stop)
            if stopped() or call_waiting():
                return state(phase='done', word=word, name=name, got=0, of=count, stopped=True)
            time.sleep(0.25)
        if cap is None:
            return state(phase='error', word=word, name=name, error='麦克风被占用（通话中？）')
        fd = cap.recorder.stdout.fileno()
        rest = b''
        miss = False
        while got < count and tries < count * 2 + 5:
            if stopped() or call_waiting():
                break
            tries += 1
            state(phase='say', word=word, name=name, got=got, of=count, miss=miss)
            subprocess.Popen(['e5-vibrate', '60'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            q = vad.Question()
            pcm = None
            while pcm is None and not stopped() and not call_waiting():
                r, _, _ = select.select([fd], [], [], 0.1)
                data = b''
                if r:
                    data = os.read(fd, 65536)
                    if not data:
                        raise RuntimeError('麦克风断了')
                    data, rest = rest + data, b''
                    if len(data) % 2:
                        data, rest = data[:-1], data[-1:]
                pcm = q.feed(data)
            secs = len(pcm or b'') / 2 / RATE
            # (what vad keeps ends with QUIET_S of quiet: the word itself is that much shorter)
            said = secs - vad.QUIET_S
            if pcm and MIN_S <= said <= MAX_S:
                got += 1
                save(pcm, word, got)
                miss = False
                state(phase='got', word=word, name=name, got=got, of=count)
            else:
                miss = True
            time.sleep(GAP_S)
        kept = got >= KEEP_MIN
        if kept:
            replace_old(word)
        state(phase='done', word=word, name=name, got=got, of=count, stopped=stopped(), kept=kept)
    except Exception as e:
        state(phase='error', word=word, name=name, got=got, of=count, error=str(e) or type(e).__name__)
    finally:
        # (each on its own: one failing must not leave the microphone or the flag held)
        if cap is not None:
            quietly(cap.stop)
        fresh.set()
        quietly(drop_mine, WANT)
        quietly(drop_mine, PIDF)
        quietly(os.unlink, STOP)
        quietly(shutil.rmtree, f'{SAMPLES}/{word}.new')


if __name__ == '__main__':
    main()
