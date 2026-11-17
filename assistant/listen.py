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
import base64
import http.client
import importlib.util
import json
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
SKIP = RUN + '/skip'                      # (read enough: stop speaking, listen for the next turn)
ANSWER = RUN + '/answer'                 # (+ .mp3 or .wav)
WANT = '/tmp/run/e5-assistant.want'
HISTORY = '/mnt/e5-data/e5assist/history.json'
NAS_CERT = '/etc/e5rec/nas.pem'
RATE = 48000
MAX_S = 60
MIN_S = 0.6
NAS_TIMEOUT = 240                       # (the NAS's own worst case: ASR + thinking hard with search + speech)
# every turn ends by the sound (vad.py): no key held
MAX_TURNS = 10

sys.path.insert(0, '/opt/e5')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vad  # noqa: E402
spec = importlib.util.spec_from_file_location('e5_capture', '/opt/e5/e5-capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else ''
LOCKED = len(sys.argv) > 2 and sys.argv[2] == 'locked'
# called by a wake word (wake.py): the first question is heard by wake.py, from the stream it
# already has (no gap after the word), and handed over in WAKE_PCM
WOKEN = len(sys.argv) > 3 and sys.argv[3] == 'wake'
WAKE_PCM = f'{RUN}/wake-{RUN_ID}.pcm'
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
        json.dump({'phase': phase, 'run': RUN_ID, 'at': time.time(), 'pid': os.getpid(), 'wake': WOKEN, **kw}, f, ensure_ascii=False)
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
            if not auto and said(STOP):
                raise RuntimeError('松手太快，还没开始录')
            if call_waiting():
                raise RuntimeError('有来电')
            time.sleep(0.25)
        if cap is None:
            raise RuntimeError('麦克风被占用（通话中？）')
        if not auto and said(STOP):
            raise RuntimeError('松手太快，还没开始录')
        fd = cap.recorder.stdout.fileno()
        pcm = bytearray()
        state('listening', since=time.time(), turn=TURN[0], auto=auto)
        if auto:
            buzz()
        end = time.monotonic() + MAX_S
        q = vad.Question() if auto else None
        while time.monotonic() < end and (auto or not said(STOP)):
            if said(CANCEL):
                return None
            if call_waiting():
                raise RuntimeError('有来电，不问了')
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                if q:
                    done = q.feed(b'')
                    if done is not None:
                        return done
                continue
            data = os.read(fd, 65536)
            if not data:
                break
            if not auto:
                pcm += data
                continue
            done = q.feed(data)
            if done is not None:
                return done
        if auto:
            return bytes(q.pcm) if q.started else b''
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


def ask(wav, step=lambda steps: None):
    """The NAS's answer; its progress lines (X-Stream) handed to step() as they come.
    None when cancelled meanwhile."""
    url, token = uci('upload_url'), uci('upload_token')
    if not url or not token or not os.path.exists(NAS_CERT):
        raise RuntimeError('还没设置 NAS（在「录音」应用里）')
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                       context=ssl.create_default_context(cafile=NAS_CERT), timeout=NAS_TIMEOUT)
    try:
        conn.request('POST', '/v1/assistant', body=wav, headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'audio/wav', 'X-Locked': '1' if LOCKED else '0',
            'X-Boost': setting('boost', '8'), 'X-Mode': setting('mode', 'max'), 'X-Stream': '1',
            # (MP3, an eighth of the size, only when PulseAudio plays it: aplay knows WAV only)
            'X-Audio': 'mp3' if pulse() else 'wav'})
        r = conn.getresponse()
        if r.status != 200:
            try:
                body = json.loads(r.read() or b'{}')
            except ValueError:
                body = {}
            raise RuntimeError(body.get('error') or f'NAS 返回 {r.status}')
        steps, body = [], {}
        while True:
            line = r.readline()
            if said(CANCEL):
                return None
            if not line:
                break
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if 'progress' in o:
                steps.append(str(o['progress']))
                step(steps[-4:])
            else:
                body = o
    finally:
        conn.close()
    if not body.get('ok'):
        raise RuntimeError(body.get('error') or 'NAS 没给回答（连接断了？）')
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


def pulse():
    return subprocess.run(['pidof', 'pulseaudio'], capture_output=True).returncode == 0


def ours_playing():
    # (only the player of this file -- paplay or aplay -- not a ringtone or another app's sound)
    return subprocess.run(['pgrep', '-f', ANSWER + '[.]'], capture_output=True).returncode == 0


def speak(audio, fmt='wav', secs=None):
    path = f'{ANSWER}.{"mp3" if fmt == "mp3" else "wav"}'
    for old in (ANSWER + '.mp3', ANSWER + '.wav'):
        if old != path:
            try:
                os.unlink(old)
            except FileNotFoundError:
                pass
    with open(path, 'wb') as f:
        f.write(audio)
    if not secs and fmt != 'mp3':
        # (the length from the size: a streamed WAV's header may not say how long it is)
        try:
            with wave.open(path) as w:
                per_s = w.getframerate() * w.getsampwidth() * w.getnchannels()
            secs = max(0, len(audio) - 44) / per_s
        except (wave.Error, EOFError, ZeroDivisionError):
            secs = None
    secs = secs or 600
    subprocess.run(['/usr/libexec/e5-volume', 'play', path])
    limit = time.monotonic() + secs + 3
    time.sleep(0.3)
    while time.monotonic() < limit and not said(CANCEL) and not said(SKIP):
        if not ours_playing():
            return
        time.sleep(0.2)
    # (no pkill on this image)
    # (the pattern does not match its own command line: the shell running it is not killed)
    subprocess.run("kill $(pgrep -f 'e5-assistant/answer[.]') 2>/dev/null", shell=True)


TURN = [0]
last_balance = [None]


def woken_question():
    """The first question after a wake word: wake.py's PCM (b'' for nothing said), None if cancelled."""
    state('listening', since=time.time(), turn=0, auto=True, wake=True)
    end = time.monotonic() + vad.WAIT_S + vad.TURN_MAX_S + 10
    try:
        while time.monotonic() < end:
            if said(CANCEL):
                return None
            if call_waiting():
                raise RuntimeError('有来电，不问了')
            try:
                with open(WAKE_PCM, 'rb') as f:
                    return f.read()
            except FileNotFoundError:
                time.sleep(0.1)
        raise RuntimeError('唤醒后没收到问题（唤醒服务没响应）')
    finally:
        for p in (WAKE_PCM, WAKE_PCM + '.part'):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


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
    fresh = threading.Event()

    def want():
        # (the recorder lets go of the microphone: not while wake.py still hears the question from it)
        with open(WANT, 'w') as f:
            f.write(ME)
        threading.Thread(target=keep_want, args=(fresh,), daemon=True).start()
    if not WOKEN:
        want()
    wake(True)
    continuous = setting('continuous', '1') == '1'
    q = a = None
    try:
        state('starting')
        while True:
            # (every turn, the first too, ends by the sound: the key only summons, it may be let go after the pulse)
            if WOKEN and not TURN[0]:
                pcm = woken_question()
            else:
                pcm = listen(auto=True)
            if pcm is None:
                return state('done', cancelled=True, question=q, answer=a)
            if not pcm:
                if WOKEN and not TURN[0]:
                    # (a wake word heard by mistake, nothing after it: the card just goes)
                    return state('done', cancelled=True, woken=True)
                if not TURN[0]:
                    return state('done', short=True, error='没听到你说话（震一下之后直接说就行）')
                return state('done', ended=True, question=q, answer=a, turn=TURN[0], balance=last_balance[0])
            secs = len(pcm) / 2 / RATE
            if secs < MIN_S:
                if WOKEN and not TURN[0]:
                    return state('done', cancelled=True, woken=True)
                return state('done', short=True, error='没听清，太短了')
            if WOKEN and not TURN[0]:
                # (only now the recorder lets go: a wake word heard by mistake does not cut its recording)
                want()
            state('thinking', seconds=round(secs, 1), turn=TURN[0])
            r = ask(to_wav(pcm), lambda steps: state('thinking', seconds=round(secs, 1), turn=TURN[0], steps=steps))
            if r is None or said(CANCEL):
                return state('done', cancelled=True)
            q, a = r.get('question') or '', r.get('answer') or ''
            remember(q, a)
            bal = r.get('balance')
            state('speaking', question=q, answer=a, timing=r.get('timing'), turn=TURN[0], balance=bal)
            audio = base64.b64decode(r.get('audio') or '')
            if audio:
                speak(audio, r.get('format') or 'wav', r.get('seconds'))
            if said(CANCEL):
                return state('done', cancelled=True, question=q, answer=a)
            last_balance[0] = bal
            skipped = said(SKIP)
            if skipped:
                try:
                    os.unlink(SKIP)
                except FileNotFoundError:
                    pass
            # (nothing heard, or a one-question setting: that is the end of it)
            if not continuous or not q or TURN[0] + 1 >= MAX_TURNS:
                return state('done', question=q, answer=a, timing=r.get('timing'), turn=TURN[0], balance=bal)
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
