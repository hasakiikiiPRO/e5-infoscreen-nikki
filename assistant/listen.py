#!/usr/bin/python3
"""助手 (assistant): one question, start to end -- run by backend.uc's POST /start.

  listen.py <run id> [locked]

  starting   the 录音 app is asked to let go of the microphone (WANT, as a call would)
  listening  the microphone taken: what is said goes to a WAV until backend.uc's
             POST /stop, a cancel, an incoming call or MAX_S
  thinking   the WAV to the NAS (e5rec_server.py POST /v1/assistant: pinned
             certificate, the 录音 app's upload_url / upload_token); "locked": asked
             from the lock screen, the NAS leaves the recordings out of it
  speaking   the answer through the speaker (e5-volume play), raised by the NAS (boost)
  then, in a conversation (setting "continuous"), listening again by itself: the
  motor pulses, and what is said is taken from where the sound rises to where it
  falls quiet (an energy measure: no key to hold); nothing said for WAIT_S, a tap,
  or MAX_TURNS, and the conversation ends
  done | error

Stop and cancel are files holding the run id they are for: one meant for an
earlier run, or arriving before this one started, is not taken for this one.
Each step goes to STATE (with the run id) for the screen.  The recorder's flag
and the pid file are removed only by the run that wrote them.
"""
import array
import base64
import http.client
import importlib.util
import json
import math
import os
import select
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import wave

RUN = '/tmp/run/e5-assistant'
STATE, STOP, CANCEL, PIDF = RUN + '/state.json', RUN + '/stop', RUN + '/cancel', RUN + '/pid'
ANSWER = RUN + '/answer.wav'
WANT = '/tmp/run/e5-assistant.want'
HISTORY = '/mnt/e5-data/e5assist/history.json'
NAS_CERT = '/etc/e5rec/nas.pem'
RATE = 48000
MAX_S = 60
MIN_S = 0.6
NAS_TIMEOUT = 240                       # (the NAS's own worst case: ASR + thinking hard with search + speech)
# a conversation's later turns: no key held, the sound tells
WAIT_S = 6                              # nothing said this long after the pulse: it is over
TURN_MAX_S = 30
QUIET_S = 1.3                           # this long quiet after speech: the turn is said (a pause between sentences is ~1.1 s)
CHUNK = 1440                            # 30 ms at 48 kHz, 16-bit mono: 2880 bytes
MAX_TURNS = 10

sys.path.insert(0, '/opt/e5')
spec = importlib.util.spec_from_file_location('e5_capture', '/opt/e5/e5-capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else ''
LOCKED = len(sys.argv) > 2 and sys.argv[2] == 'locked'
ME = str(os.getpid())


def log(msg):
    subprocess.run(['logger', '-t', 'e5-assistant', '--', msg])


def uci(key, conf='e5-plugin-recorder'):
    r = subprocess.run(['uci', '-q', 'get', f'{conf}.settings.{key}'], capture_output=True, text=True)
    return r.stdout.strip()


def setting(key, default):
    return uci(key, 'e5-plugin-assistant') or default


def said(path):
    """A stop / cancel for this run."""
    try:
        return open(path).read().strip() == RUN_ID
    except OSError:
        return False


def state(phase, **kw):
    tmp = f'{STATE}.{ME}'
    with open(tmp, 'w') as f:
        json.dump({'phase': phase, 'run': RUN_ID, 'at': time.time(), 'pid': os.getpid(), **kw}, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def wake(on):
    try:
        with open('/sys/power/wake_lock' if on else '/sys/power/wake_unlock', 'w') as f:
            # (with a timeout: a run killed outright must not keep the device awake for good)
            f.write('e5-assistant 240000000000' if on else 'e5-assistant')
    except OSError:
        pass


def drop_mine(path):
    try:
        if open(path).read().strip() == ME:
            os.unlink(path)
    except OSError:
        pass


def call_waiting():
    try:
        return bool(capture.routing.pending_voice())
    except Exception:
        return False


def buzz(ms=60):
    subprocess.Popen(['e5-vibrate', str(ms)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rms(b):
    a = array.array('h', b)
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def listen(auto=False):
    """-> 16-bit mono PCM at RATE; None when cancelled; b'' when, listening by itself
    (auto), nothing was said; raises on a call or no microphone."""
    cap = None
    try:
        # (the recorder looks every 0.25 s and closes its segment: up to ~4 s here)
        for _ in range(16):
            c = capture.Capture()
            if c.start(None):
                cap = c
                break
            try:
                c.stop()
            except Exception:
                pass
            if said(CANCEL):
                return None
            if said(STOP):
                raise RuntimeError('松手太快，还没开始录')
            if call_waiting():
                raise RuntimeError('有来电')
            time.sleep(0.25)
        if cap is None:
            raise RuntimeError('麦克风被占用（通话中？）')
        if said(STOP):
            raise RuntimeError('松手太快，还没开始录')
        fd = cap.recorder.stdout.fileno()
        pcm = bytearray()
        state('listening', since=time.time(), turn=TURN[0], auto=auto)
        if auto:
            buzz()
        end = time.monotonic() + (TURN_MAX_S if auto else MAX_S)
        # (auto: the noise floor from the quietest stretch, speech well above it)
        floor, started, quiet, buf = None, False, 0, bytearray()
        give_up = time.monotonic() + WAIT_S
        while time.monotonic() < end and (auto or not said(STOP)):
            if said(CANCEL):
                return None
            if call_waiting():
                raise RuntimeError('有来电，不问了')
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            data = os.read(fd, 65536)
            if not data:
                break
            if not auto:
                pcm += data
                continue
            buf += data
            while len(buf) >= CHUNK * 2:
                chunk, buf = bytes(buf[:CHUNK * 2]), buf[CHUNK * 2:]
                level = rms(chunk)
                # (the room's own noise: down at once to the quietest, up only slowly)
                floor = level if floor is None or level < floor else floor * 1.003
                floor = max(floor, 30.0)
                # (measured on this microphone: speech ~150-480, a quiet room ~25-100)
                loud = level > max(floor * 2.5, 110)
                pcm += chunk
                if not started:
                    # (0.3 s kept before the speech begins)
                    del pcm[:-CHUNK * 2 * 10]
                    quiet = quiet + 1 if loud else 0
                    if quiet >= 3:
                        started, quiet = True, 0
                    elif time.monotonic() > give_up:
                        return b''
                else:
                    quiet = 0 if loud else quiet + 1
                    if quiet * CHUNK / RATE >= QUIET_S:
                        return bytes(pcm)
        if auto and not started:
            return b''
        return bytes(pcm[:len(pcm) // 2 * 2])
    finally:
        if cap is not None:
            cap.stop()


def to_wav(pcm):
    import io
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return b.getvalue()


def ask(wav):
    url, token = uci('upload_url'), uci('upload_token')
    if not url or not token or not os.path.exists(NAS_CERT):
        raise RuntimeError('还没设置 NAS（在「录音」应用里）')
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                       context=ssl.create_default_context(cafile=NAS_CERT), timeout=NAS_TIMEOUT)
    try:
        conn.request('POST', '/v1/assistant', body=wav, headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'audio/wav', 'X-Locked': '1' if LOCKED else '0',
            'X-Boost': setting('boost', '8'), 'X-Mode': setting('mode', 'max')})
        r = conn.getresponse()
        body = json.loads(r.read() or b'{}')
    finally:
        conn.close()
    if r.status != 200 or not body.get('ok'):
        raise RuntimeError(body.get('error') or f'NAS 返回 {r.status}')
    return body


def remember(q, a):
    try:
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        try:
            h = json.load(open(HISTORY))
        except (OSError, ValueError):
            h = []
        h = (h if isinstance(h, list) else [])[-49:] + [{'at': round(time.time()), 'q': q, 'a': a}]
        with open(HISTORY + '.new', 'w') as f:
            json.dump(h, f, ensure_ascii=False)
        os.replace(HISTORY + '.new', HISTORY)
    except OSError:
        pass


def ours_playing():
    # (only the aplay playing this file: not a ringtone or another app's sound)
    return subprocess.run(['pgrep', '-f', ANSWER], capture_output=True).returncode == 0


def speak(audio):
    with open(ANSWER, 'wb') as f:
        f.write(audio)
    # (the length from the size: a streamed WAV's header may not say how long it is)
    try:
        with wave.open(ANSWER) as w:
            per_s = w.getframerate() * w.getsampwidth() * w.getnchannels()
        secs = max(0, len(audio) - 44) / per_s
    except (wave.Error, EOFError, ZeroDivisionError):
        secs = 30
    subprocess.run(['/usr/libexec/e5-volume', 'play', ANSWER])
    limit = time.monotonic() + secs + 3
    time.sleep(0.3)
    while time.monotonic() < limit and not said(CANCEL):
        if not ours_playing():
            return
        time.sleep(0.2)
    # (no pkill on this image)
    # (the pattern does not match its own command line: the shell running it is not killed)
    subprocess.run("kill $(pgrep -f 'e5-assistant/answer[.]wav') 2>/dev/null", shell=True)


TURN = [0]


def keep_want(stop):
    """The recorder's flag for the whole conversation, fresh (it counts as gone after 90 s)."""
    while not stop.wait(10):
        try:
            os.utime(WANT)
        except OSError:
            return


def main():
    # (a new question kills this one: its finally -- the microphone, the recorder's flag -- must still run)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    with open(PIDF, 'w') as f:
        f.write(ME)
    with open(WANT, 'w') as f:
        f.write(ME)
    fresh = threading.Event()
    threading.Thread(target=keep_want, args=(fresh,), daemon=True).start()
    wake(True)
    continuous = setting('continuous', '1') == '1'
    q = a = None
    try:
        state('starting')
        while True:
            pcm = listen(auto=TURN[0] > 0)
            if pcm is None:
                return state('done', cancelled=True, question=q, answer=a)
            if TURN[0] and not pcm:
                return state('done', ended=True, question=q, answer=a, turn=TURN[0])
            secs = len(pcm) / 2 / RATE
            if secs < MIN_S:
                return state('done', short=True, error='按住时间太短，没录到话')
            state('thinking', seconds=round(secs, 1), turn=TURN[0])
            r = ask(to_wav(pcm))
            if said(CANCEL):
                return state('done', cancelled=True)
            q, a = r.get('question') or '', r.get('answer') or ''
            remember(q, a)
            state('speaking', question=q, answer=a, timing=r.get('timing'), turn=TURN[0])
            audio = base64.b64decode(r.get('audio') or '')
            if audio:
                speak(audio)
            if said(CANCEL):
                return state('done', cancelled=True, question=q, answer=a)
            # (nothing heard, or a one-question setting: that is the end of it)
            if not continuous or not q or TURN[0] + 1 >= MAX_TURNS:
                return state('done', question=q, answer=a, timing=r.get('timing'), turn=TURN[0])
            TURN[0] += 1
            try:
                os.unlink(STOP)              # (the first turn's release: the later turns hold no key)
            except FileNotFoundError:
                pass
    except Exception as e:
        log(f'error: {e}')
        state('error', error=str(e) or type(e).__name__, question=q, answer=a)
    finally:
        fresh.set()
        wake(False)
        drop_mine(WANT)
        drop_mine(PIDF)


if __name__ == '__main__':
    main()
