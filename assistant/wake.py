#!/usr/bin/python3
"""助手 (assistant): call it by name -- 「贾维斯」or「大肥鱼」-- instead of holding the key.

The background service (/etc/init.d/e5-assistant-wake, wake.init), on while the app's
setting "wake" is 1.  The words are spotted on the E5 itself (sherpa-onnx's keyword
spotter: /opt/e5-kws -- its own glibc, lib/e5-kws from wake/kws.c, a 3 MB model): nothing
leaves the E5 until a word is heard.  That runtime is too big for the app's package: the
first time "wake" is on it is fetched from the NAS (the 录音 app's address, token and
pinned certificate; e5rec_server.py GET /v1/kws/...) and taken only with KWS_SHA256.

Where the sound comes from (the system has one capture path, e5-capture.py's lease):
  the 录音 app recording  it copies what it records into FEED (a FIFO): both go on
  not recording           this service takes the microphone itself (and keeps the E5
                          from deep sleep: the microphone stops in it)
  a call, the assistant asking, listening or answering: nothing is heard here (and the
  microphone is let go) -- its own voice saying the name would call it again.

While the assistant speaks an answer the microphone is free: this listens on, and the name
said then cuts the answer short for a new question (barge-in) -- not while the answer itself
holds a name, and given back the moment listen.py wants the microphone (mic.want).

A word heard: the motor pulses and -- unless the question is already being said, in one
breath with the word -- it answers aloud first (here.wav, 「我在，你说」: heard from across
the room; setting "wake_reply"), what the microphone takes meanwhile thrown away.  The
question is cut from the same stream (vad.py: no gap after the word); POST /start
{wake} starts the assistant's run, and listen.py takes the question from
/tmp/run/e5-assistant/wake-<run>.pcm instead of listening itself -- then on as if
the key had been held (the card, the answer, the conversation).
"""
import hashlib
import http.client
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import shutil
import ssl
import tarfile
import time
import urllib.parse
import urllib.request
import wave

APP = '/etc/e5-infoscreen/plugins/assistant'
KWS = '/opt/e5-kws'
KWS_FILE = 'e5-kws-3.tar.gz'            # (wake/build-runtime.sh: its version 3)
KWS_SHA256 = '124d8ef4194f3835bf1e4e5f9b2711125b27c99403916985f5bc09f9298a41f3'
NAS_CERT = '/etc/e5rec/nas.pem'
FETCH_RETRY_S = 600
RUN = '/tmp/run/e5-wake'
FEED = RUN + '/feed'
STATE = RUN + '/state.json'
ARUN = '/tmp/run/e5-assistant'
WANT = '/tmp/run/e5-assistant.want'
WANT_MAX_S = 90
API = 'http://127.0.0.1:8088/api/plugins/assistant'
LOCK = 'e5-assistant-wake'
RUNNING = '/var/run/e5-assistant-wake.running'
GONE_S = 30                             # (the app gone this long: removed, not a reinstall)
RATE = 48000
REC_STATE = '/tmp/run/e5-recorder/state.json'
# the recorder about to take the microphone (it touches this before each try): let it go
REC_WANTS = '/tmp/run/e5-recorder/wants-mic'
REC_WANTS_S = 15
HERE = APP + '/here.wav'               # (the answer to the name; DeepSeek's stella, +8 dB like the answers)
HERE_TAIL_S = 0.3                       # (the room's echo of it, after it ends: not the question either)
LATENCY_S = 0.3                         # (a hit is told this long after the word ended, at most: cut that much earlier)
RING_S = 3                              # what was heard last, kept: the room's noise, the speech after the word
WORDS = {'jarvis': ('j iǎ w éi s ī', '贾维斯'), 'fish': ('d à f éi y ú', '大肥鱼')}
# how sure the spotter must be (sherpa's keywords_threshold: higher = fewer false calls, more misses).
# Measured with the gain: 0.12 still no false call in 3.2 h of podcasts and the E5's own recordings;
# 0.25 misses the wake word played by a speaker across the room
SENSITIVITY = {'high': 0.12, 'normal': 0.18, 'low': 0.25}

sys.path.insert(0, '/opt/e5')
sys.path.insert(0, APP)
import vad  # noqa: E402
spec = importlib.util.spec_from_file_location('e5_capture', '/opt/e5/e5-capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)
routing = capture.routing


def log(msg):
    subprocess.run(['logger', '-t', 'e5-assistant-wake', '--', msg])


_uci = {}


def uci(key, conf='e5-plugin-assistant'):
    # (read again every few seconds: the loop runs four times a second, uci is a process each)
    k = (conf, key)
    hit = _uci.get(k)
    if hit and time.monotonic() - hit[0] < 3:
        return hit[1]
    r = subprocess.run(['uci', '-q', 'get', f'{conf}.settings.{key}'], capture_output=True, text=True)
    _uci[k] = (time.monotonic(), r.stdout.strip())
    return _uci[k][1]


def call_waiting():
    try:
        return bool(routing.pending_voice())
    except Exception:
        return False


def wake_lock(on):
    try:
        with open('/sys/power/wake_lock' if on else '/sys/power/wake_unlock', 'w') as f:
            f.write(LOCK)
    except OSError:
        pass


def buzz(ms=60):
    subprocess.Popen(['e5-vibrate', str(ms)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_json(path, obj):
    tmp = f'{path}.{os.getpid()}'
    with open(tmp, 'w') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def recorder_recording():
    """The recorder recording now (its own state, fresh), not just switched on."""
    try:
        r = json.load(open(REC_STATE))
        return bool(r.get('recording')) and time.time() - float(r.get('at') or 0) < 10
    except (OSError, ValueError, TypeError):
        return False


def recorder_wants():
    try:
        return time.time() - os.path.getmtime(REC_WANTS) < REC_WANTS_S
    except OSError:
        return False


def pid_file_alive(path, name='listen.py'):
    try:
        pid = open(path).read().strip()
        return pid.isdigit() and name.encode() in open(f'/proc/{pid}/cmdline', 'rb').read()
    except OSError:
        return False


def assistant_busy():
    """The assistant asking (its run listening, thinking), or a call: not for this service's
    ears.  Speaking an answer it is not: the name then cuts it short."""
    if call_waiting():
        return 'call'
    if pid_file_alive(ARUN + '/pid'):
        try:
            s = json.load(open(ARUN + '/state.json'))
        except (OSError, ValueError):
            s = {}
        said = f"{s.get('question') or ''}{s.get('answer') or ''}"
        if (s.get('phase') == 'speaking' and not pid_file_alive(ARUN + '/mic.want')
                and not any(WORDS[w][1] in said for w in WORDS)):
            return None
        return 'assistant'
    try:
        if time.time() - os.path.getmtime(WANT) < WANT_MAX_S:
            return 'assistant'
    except OSError:
        pass
    return None


class Spotter:
    """lib/e5-kws, running: PCM in, "HIT <word> <samples>" out."""

    def __init__(self, words, threshold):
        os.makedirs(RUN, exist_ok=True)
        kw = RUN + '/keywords.txt'
        with open(kw, 'w') as f:
            for w in words:
                f.write(f'{WORDS[w][0]} @{WORDS[w][1]}\n')
        self.key = (tuple(words), threshold)
        self.p = subprocess.Popen([f'{KWS}/lib/ld-linux-aarch64.so.1', '--library-path', f'{KWS}/lib',
                                   f'{KWS}/lib/e5-kws', f'{KWS}/model', kw, str(threshold), str(RATE)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(self.p.stdout.fileno(), False)
        self.out = b''
        self.fed = 0                    # (samples written: the hits count in the same samples)
        # (fed only once the model is loaded: a full pipe meanwhile would block the loop that
        # lets go of the microphone for a call)
        self.ready = False
        self.dead = False

    def write(self, pcm):
        """False (nothing written, nothing counted) until READY, or once it has died."""
        if not self.ready or self.dead:
            return False
        try:
            self.p.stdin.write(pcm)
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            self.dead = True
            return False
        self.fed += len(pcm) // 2
        return True

    def hits(self):
        try:
            self.out += os.read(self.p.stdout.fileno(), 4096) or b''
        except BlockingIOError:
            pass
        lines = self.out.split(b'\n')
        self.out = lines.pop()
        found = []
        for line in lines:
            parts = line.decode(errors='replace').split()
            if parts == ['READY']:
                self.ready = True
            elif len(parts) == 3 and parts[0] == 'HIT' and parts[2].isdigit():
                found.append((parts[1], int(parts[2])))
        return found

    def alive(self):
        return not self.dead and self.p.poll() is None

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


class Mic:
    """The microphone itself, when the recorder is not recording."""

    def __init__(self):
        self.cap = None
        self.retry_at = 0
        self.rest = b''

    def take(self):
        if self.cap or time.monotonic() < self.retry_at:
            return self.cap is not None
        c = capture.Capture()
        try:
            ok = c.start(None)
        except Exception as e:
            log(f'microphone: {e}')
            ok = False
        if not ok:
            try:
                c.stop()
            except Exception:
                pass
            self.retry_at = time.monotonic() + 5
            return False
        self.cap, self.rest = c, b''
        wake_lock(True)
        return True

    def fd(self):
        return self.cap.recorder.stdout.fileno() if self.cap else None

    def drop(self):
        if self.cap:
            try:
                self.cap.stop()
            finally:
                self.cap = None
                wake_lock(False)


def runtime_ok():
    try:
        return open(f'{KWS}/.sha256').read().strip() == KWS_SHA256 and os.path.exists(f'{KWS}/lib/e5-kws')
    except OSError:
        return False


def fetch_runtime():
    """The runtime from the NAS into KWS; raises with what went wrong."""
    url, token = uci('upload_url', 'e5-plugin-recorder'), uci('upload_token', 'e5-plugin-recorder')
    if not url or not token or not os.path.exists(NAS_CERT):
        raise RuntimeError('还没设置 NAS（在「录音」应用里），唤醒组件要从 NAS 下载')
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                       context=ssl.create_default_context(cafile=NAS_CERT), timeout=60)
    for f in os.listdir('/tmp'):
        if f.startswith('e5-kws-') and '.tar.gz.' in f:
            os.unlink('/tmp/' + f)      # (one cut short by a kill: 19 MB of RAM)
    st = os.statvfs('/opt')
    if st.f_bavail * st.f_frsize < 120 << 20:
        raise RuntimeError('E5 存储不够装唤醒组件（要约 120 MB）')
    tmp = f'/tmp/{KWS_FILE}.{os.getpid()}'
    h = hashlib.sha256()
    wake_lock(True)                     # (19 MB over mobile data: not stalled by sleep)
    try:
        conn.request('GET', f'/v1/kws/{KWS_FILE}', headers={'Authorization': 'Bearer ' + token})
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError(f'下载唤醒组件：NAS 返回 {r.status}')
        with open(tmp, 'wb') as f:
            for chunk in iter(lambda: r.read(1 << 16), b''):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != KWS_SHA256:
            raise RuntimeError('下载的唤醒组件校验不对，没用它')
        new = KWS + '.new'
        shutil.rmtree(new, ignore_errors=True)
        os.makedirs(new)
        with tarfile.open(tmp) as t:
            t.extractall(new, filter='data')
        with open(f'{new}/e5-kws/.sha256', 'w') as f:
            f.write(KWS_SHA256)
        shutil.rmtree(KWS, ignore_errors=True)
        os.rename(f'{new}/e5-kws', KWS)
        shutil.rmtree(new, ignore_errors=True)
    finally:
        conn.close()
        shutil.rmtree(KWS + '.new', ignore_errors=True)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def start_run():
    req = urllib.request.Request(API + '/start', json.dumps({'wake': True}).encode(),
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b'{}').get('run')


def wav_seconds(path):
    try:
        with wave.open(path) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0


def speaking(fresh, floor):
    """The question already going on, straight after the word: most of FRESH (heard after the
    hit itself -- never the word's own tail) loud."""
    tail = fresh
    if len(tail) < vad.CHUNK * 2:
        return False
    loud = [vad.rms(tail[i:i + vad.CHUNK * 2]) > max((floor or 30) * 2.5, 110)
            for i in range(0, len(tail) - vad.CHUNK * 2 + 1, vad.CHUNK * 2)]
    return sum(loud) * 2 >= len(loud)


def other_run(run):
    """A key held meanwhile: a new run (a call is looked at by itself)."""
    try:
        return open(ARUN + '/run').read().strip() != run
    except OSError:
        return True


def cancelled(run):
    try:
        return open(ARUN + '/cancel').read().strip() == run
    except OSError:
        return False


def removed():
    """The app removed: this service goes too (the script it runs is gone with it)."""
    log('app removed: stopping')
    for p in (STATE, RUNNING, FEED):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    subprocess.run('/etc/init.d/e5-assistant-wake disable; rm -f /etc/init.d/e5-assistant-wake; '
                   'ubus call service delete \'{"name":"e5-assistant-wake"}\'', shell=True)
    sys.exit(0)


def main():
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    with open(__file__, 'rb') as src, open(RUNNING, 'wb') as dst:
        dst.write(src.read())
    gone_since = None
    fetch_at = 0
    os.chmod(RUN, 0o700)
    if not os.path.exists(FEED):
        os.mkfifo(FEED, 0o600)
    def term(*_):
        # (raised wherever it is -- the question being heard, a download, a blocked write:
        # the finally lets go of the microphone before procd's kill)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, term)
    feed = None                         # (open read-write: the recorder's open never waits, no end-of-file)
    mic, spot = Mic(), None
    ring, ring_start = bytearray(), 0   # (ring_start: the sample number of ring[0])
    st = {'enabled': False, 'source': None, 'hits': 0, 'last': None, 'error': None}
    last_state = 0
    deaths, spot_at, spot_retry = 0, 0, 0
    log('started')
    try:
        while True:
            if os.path.exists(f'{APP}/manifest.json'):
                gone_since = None
            elif gone_since is None:
                gone_since = time.monotonic()
            elif time.monotonic() - gone_since > GONE_S:
                mic.drop()
                removed()
            on = uci('wake') == '1' and uci('enabled') != '0'
            if on and not runtime_ok():
                if time.monotonic() >= fetch_at:
                    st.update(enabled=True, source=None, error='正在从 NAS 下载唤醒组件（约 19 MB）')
                    write_json(STATE, {**st, 'at': round(time.time())})
                    try:
                        fetch_runtime()
                        log('the wake-word runtime installed')
                        st['error'] = None
                    except Exception as e:
                        log(f'runtime: {e}')
                        st['error'] = str(e)
                        fetch_at = time.monotonic() + FETCH_RETRY_S
                    finally:
                        if not mic.cap:
                            wake_lock(False)
                if not runtime_ok():
                    on = False
            words = [w for w in (uci('wake_words') or 'jarvis fish').split() if w in WORDS] or list(WORDS)
            threshold = SENSITIVITY.get(uci('wake_sensitivity'), SENSITIVITY['normal'])
            if not on:
                mic.drop()
                if spot:
                    spot.close()
                    spot = None
                if feed is not None:
                    os.close(feed)
                    feed = None
                st.update(enabled=False, source=None)
                write_json(STATE, {**st, 'at': round(time.time())})
                time.sleep(2)
                continue
            if feed is None:
                feed = os.open(FEED, os.O_RDWR | os.O_NONBLOCK)
            if spot and (spot.key != (tuple(words), threshold) or not spot.alive()):
                if not spot.alive():
                    # (a runtime that will not run: not every 5 s for ever)
                    deaths = deaths + 1 if time.monotonic() - spot_at < 60 else 1
                    spot_retry = time.monotonic() + min(600, 10 * 2 ** deaths)
                    st['error'] = '唤醒检测程序退出了，稍后重试'
                    log('the spotter ended')
                spot.close()
                spot = None
            if spot is None and time.monotonic() >= spot_retry:
                spot = Spotter(words, threshold)
                spot_at = time.monotonic()
                ring, ring_start = bytearray(), spot.fed
            if spot is None:
                mic.drop()
                write_json(STATE, {**st, 'enabled': True, 'source': None, 'at': round(time.time())})
                time.sleep(2)
                continue
            busy = assistant_busy()
            # (the recorder's copy only while it really records; it switched on but not recording --
            # storage low, an error -- and the microphone is taken here, given back when it tries again)
            recorder = recorder_recording()
            yielding = not recorder and recorder_wants()
            if busy or recorder or yielding:
                mic.drop()
            elif not mic.take():
                st['error'] = '麦克风被占用，稍后再试'
            source = None if busy else 'recorder' if recorder else 'mic' if mic.cap else None
            if source:
                st['error'] = None
            elif yielding and not busy:
                st['error'] = '录音正在开始，稍等'
            if not spot.ready:
                spot.hits()             # (its READY)
            st.update(enabled=True, source=source, busy=busy)
            if time.monotonic() - last_state > 5:
                write_json(STATE, {**st, 'at': round(time.time())})
                last_state = time.monotonic()
            fds = [feed] + ([mic.fd()] if mic.cap else [])
            r, _, _ = select.select(fds, [], [], 0.25)
            for fd in r:
                try:
                    data = os.read(fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if fd == feed and source != 'recorder':
                    continue            # (the recorder's copy while not listening: thrown away, never stale later)
                if not data:
                    if fd != feed:
                        mic.drop()      # (the microphone's stream ended: taken again next time)
                    continue
                if fd != feed:
                    # (the microphone's reads come in any size: whole samples only, the odd byte kept)
                    data, mic.rest = mic.rest + data, b''
                    if len(data) % 2:
                        data, mic.rest = data[:-1], data[-1:]
                    if not data:
                        continue
                if not spot.write(data):
                    continue
                ring += data
                if len(ring) > RING_S * RATE * 2:
                    cut = len(ring) - RING_S * RATE * 2
                    del ring[:cut]
                    ring_start += cut // 2
            for word, at in spot.hits():
                # (late: a word inside the question just asked, or the assistant / a call since: not a call)
                if source is None or at <= ring_start or assistant_busy():
                    continue
                st['hits'] += 1
                st['last'] = {'word': word, 'at': round(time.time())}
                log(f'heard {word}')
                heard(word, at, ring, ring_start, feed, mic, source)
                ring, ring_start = bytearray(), spot.fed
                spot.hits()             # (a second word in the same breath is part of the question: dropped)
                break
    finally:
        mic.drop()
        if spot:
            spot.close()
        if feed is not None:
            os.close(feed)
        write_json(STATE, {**st, 'enabled': False, 'source': None, 'at': round(time.time())})
        log('stopped')


def read_pcm(fd, feed, mic, want_bytes, limit_s):
    """Up to WANT_BYTES of whole samples from the source, waiting at most LIMIT_S."""
    out, end = bytearray(), time.monotonic() + limit_s
    while len(out) < want_bytes and time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            data = os.read(fd, 65536)
        except (BlockingIOError, InterruptedError):
            continue
        if not data:
            break
        if fd != feed:
            data, mic.rest = mic.rest + data, b''
            if len(data) % 2:
                data, mic.rest = data[:-1], data[-1:]
        out += data
    return bytes(out)


def heard(word, at, ring, ring_start, feed, mic, source):
    """A wake word: the pulse, the question after it, and the assistant's run with it."""
    buzz()
    # (what came after the word, already here; the room's noise from the 3 s before it)
    cut = max(0, min(len(ring), (at - int(LATENCY_S * RATE) - ring_start) * 2))
    before, after = bytes(ring[:cut]), bytes(ring[cut:])
    # (more kept from before the speech: it may go on straight after the word, during the pulse)
    # (no settle: what is here already was heard before the pulse; the pulse itself, 60 ms, is
    # shorter than speech must be to count)
    floor = vad.quietest(before)
    q = vad.Question(floor=floor, preroll=25, settle=0)
    run = None
    try:
        run = start_run()
    except Exception as e:
        log(f'the assistant did not start: {e}')
        return
    if not run:
        return
    fd = feed if source == 'recorder' else mic.fd()
    if fd is None:
        pcm = b''
    else:
        # (0.35 s more, after the hit: is the question already being said?)
        fresh = read_pcm(fd, feed, mic, int(0.35 * RATE) * 2, 1.0)
        after += fresh
    # (not said yet: "我在，你说" first, then listened to; while it plays, what comes in is dropped)
    quiet_until = 0
    secs = (wav_seconds(HERE) if fd is not None and uci('wake_reply') != '0'
            and not speaking(fresh, floor) else 0)
    if fd is None:
        pass
    elif secs:
        subprocess.run(['/usr/libexec/e5-volume', 'play', HERE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log('answered the name: 我在，你说')
        quiet_until = time.monotonic() + secs + HERE_TAIL_S
        q = vad.Question(floor=floor, preroll=10, settle=0)
        pcm = None
    else:
        pcm = q.feed(after)
    while pcm is None and fd is not None:
        if cancelled(run) or call_waiting() or other_run(run):
            pcm = b''
            break
        r, _, _ = select.select([fd], [], [], 0.1)
        data = b''
        if r:
            try:
                data = os.read(fd, 65536)
            except (BlockingIOError, InterruptedError):
                pass
            if not data and fd != feed:
                pcm = b''               # (the microphone's stream ended)
                break
        if fd != feed:
            data, mic.rest = mic.rest + data, b''
            if len(data) % 2:
                data, mic.rest = data[:-1], data[-1:]
        if time.monotonic() < quiet_until:
            continue
        if quiet_until:
            # (the wait for the question from when it has finished saying 我在)
            q, quiet_until = vad.Question(floor=floor, preroll=10, settle=0), 0
        pcm = q.feed(data)
    pcm = pcm or b''
    path = f'{ARUN}/wake-{run}.pcm'
    with open(path + '.part', 'wb') as f:
        f.write(pcm)
    os.replace(path + '.part', path)
    # (now listen.py asks; the microphone goes to it for the next turn -- the main loop sees it busy)


if __name__ == '__main__':
    try:
        main()
    finally:
        wake_lock(False)
