#!/usr/bin/python3
"""录音 (recorder) -- the background half of the info screen app.

While e5-plugin-recorder.settings.enabled is 1 it records the built-in
microphone into MP3 segments (48 kHz mono, ~33 kbps, ~15 MB an hour) under
/mnt/e5-data/e5rec: rec-YYYYMMDD-HHMMSS.mp3, each with a .json beside it
(start, end, seconds, why it ended).  A segment being written is *.mp3.part;
only finished pairs are for the computer to take (e5rec-sync).

The microphone is the system's one capture path (/opt/e5/e5-capture.py's
Capture, the hardware lease of e5_audio_routing): a call asks for the lease
(VOICE_PENDING) and gets it within its 5 s wait -- the segment is closed at
once and recording resumes after the call.  A wake lock is held while it
records: deep sleep would stop the microphone.
"""
import ctypes
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import time

sys.path.insert(0, '/opt/e5')

APP = '/etc/e5-infoscreen/plugins/recorder'
OUT = '/mnt/e5-data/e5rec'
RUN = '/tmp/run/e5-recorder'
STATE = RUN + '/state.json'
RUNNING = '/var/run/e5-recorder.running'
WAKE = 'e5-recorder'
MIN_FREE_MB = 300
GONE_LOOPS = 30                         # (~30 s gone: removed, not a reinstall)


def log(msg):
    subprocess.run(['logger', '-t', 'e5-recorder', '--', msg])


def setting(key, default):
    r = subprocess.run(['uci', '-q', 'get', f'e5-plugin-recorder.settings.{key}'],
                       capture_output=True, text=True)
    return r.stdout.strip() or default


def write_json(path, obj):
    tmp = f'{path}.{os.getpid()}'
    with open(tmp, 'w') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def free_mb():
    try:
        st = os.statvfs(os.path.dirname(OUT))
        return st.f_bavail * st.f_frsize // 1048576
    except OSError:
        return 0


def wake(on):
    try:
        with open('/sys/power/wake_lock' if on else '/sys/power/wake_unlock', 'w') as f:
            f.write(WAKE)
    except OSError:
        pass


# ---------- MP3 through libsndfile (LAME) ----------

class _Info(ctypes.Structure):
    _fields_ = [('frames', ctypes.c_int64), ('samplerate', ctypes.c_int), ('channels', ctypes.c_int),
                ('format', ctypes.c_int), ('sections', ctypes.c_int), ('seekable', ctypes.c_int)]


_sf = ctypes.CDLL('libsndfile.so.1')
_sf.sf_open.restype = ctypes.c_void_p
_sf.sf_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_Info)]
_sf.sf_write_short.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int64]
_sf.sf_write_short.restype = ctypes.c_int64
_sf.sf_command.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
_sf.sf_close.argtypes = [ctypes.c_void_p]
_sf.sf_strerror.restype = ctypes.c_char_p
_sf.sf_strerror.argtypes = [ctypes.c_void_p]
SF_WRITE, SF_MP3 = 0x20, 0x230000 | 0x0082
SFC_SET_COMPRESSION_LEVEL = 0x1301


class Mp3:
    def __init__(self, path, rate):
        info = _Info(0, rate, 1, SF_MP3, 0, 0)
        self.h = _sf.sf_open(path.encode(), SF_WRITE, ctypes.byref(info))
        if not self.h:
            raise RuntimeError('sf_open: ' + _sf.sf_strerror(None).decode())
        level = ctypes.c_double(0.85)        # (~33 kbps for speech)
        _sf.sf_command(self.h, SFC_SET_COMPRESSION_LEVEL, ctypes.byref(level), 8)
        self.frames = 0

    def write(self, pcm):
        n = len(pcm) // 2
        if n:
            self.frames += _sf.sf_write_short(self.h, pcm[:n * 2], n)
        return pcm[n * 2:]

    def close(self):
        if self.h:
            _sf.sf_close(self.h)
            self.h = None


# ---------- the system's capture path ----------

spec = importlib.util.spec_from_file_location('e5_capture', '/opt/e5/e5-capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)
routing = capture.routing
RATE = 48000


class Segment:
    def __init__(self):
        os.makedirs(OUT, exist_ok=True)
        self.start = time.time()
        self.name = 'rec-' + time.strftime('%Y%m%d-%H%M%S', time.localtime(self.start))
        self.part = f'{OUT}/{self.name}.mp3.part'
        n = 1
        while os.path.exists(f'{OUT}/{self.name}.mp3') or os.path.exists(self.part):
            # (the same second twice -- a clock set back: never over an unsynced one)
            self.name = 'rec-' + time.strftime('%Y%m%d-%H%M%S', time.localtime(self.start)) + f'-{n}'
            self.part = f'{OUT}/{self.name}.mp3.part'
            n += 1
        self.cap = capture.Capture()
        if not self.cap.start(None):
            raise BlockingIOError('microphone busy (a call or another capture)')
        try:
            self.fd = self.cap.recorder.stdout.fileno()
            self.mp3 = Mp3(self.part, RATE)
        except BaseException:
            # (the hardware lease must never stay held: a call waits for it)
            self.cap.stop()
            raise
        self.rest = b''

    def pump(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if r:
            data = os.read(self.fd, 65536)
            if not data:
                raise RuntimeError('the microphone stream ended')
            self.rest = self.mp3.write(self.rest + data)

    def seconds(self):
        return self.mp3.frames / RATE

    def close(self, why):
        try:
            self.cap.stop()
        finally:
            self.mp3.close()
        secs = round(self.seconds(), 1)
        if secs < 1:
            os.unlink(self.part)
            return None
        write_json(f'{OUT}/{self.name}.json', {
            'name': self.name, 'start': round(self.start), 'end': round(self.start + secs),
            'seconds': secs, 'rate': RATE, 'ended': why})
        os.replace(self.part, f'{OUT}/{self.name}.mp3')
        log(f'{self.name}.mp3: {secs:.0f} s ({why})')
        return self.name


def size(path):
    # (the computer deletes what it took, at any moment)
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def finished():
    try:
        names = sorted(f[:-4] for f in os.listdir(OUT) if f.endswith('.mp3'))
    except FileNotFoundError:
        names = []
    return names


def last_sync():
    try:
        with open(OUT + '/.last_sync') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def unpatch_index():
    index = '/usr/share/e5-infoscreen/www/index.html'
    try:
        src = open(index).read()
    except OSError:
        return
    a = src.find('<!-- BEGIN e5-recorder')
    end = '<!-- END e5-recorder -->\n'
    b = src.find(end, a)
    if a < 0 or b < 0:
        return
    with open(index + '.recorder-new', 'w') as f:
        f.write(src[:a] + src[b + len(end):])
    os.replace(index + '.recorder-new', index)
    subprocess.Popen("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1", shell=True)


def removed(seg):
    unpatch_index()
    # (the side key's "record" choice would only fail now: back to the hotspot)
    subprocess.run("[ \"$(uci -q get e5-plugin-sidekey.settings.mode)\" = record ] && "
                   "uci set e5-plugin-sidekey.settings.mode=toggle && uci commit e5-plugin-sidekey", shell=True)
    if seg:
        seg.close('app removed')
    wake(False)
    log('app removed: stopping')
    for p in (STATE, RUNNING):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    subprocess.run('/etc/init.d/e5-recorder disable; rm -f /etc/init.d/e5-recorder; '
                   'ubus call service delete \'{"name":"e5-recorder"}\'', shell=True)
    sys.exit(0)


def main():
    os.makedirs(RUN, exist_ok=True)
    # (a segment cut short by a crash or a power loss: what was written is kept, as it is)
    for f in os.listdir(OUT) if os.path.isdir(OUT) else []:
        if f.endswith('.mp3.part'):
            name = f[:-9]
            if size(f'{OUT}/{f}') < 4096 or os.path.exists(f'{OUT}/{name}.mp3'):
                os.unlink(f'{OUT}/{f}')
                continue
            if not os.path.exists(f'{OUT}/{name}.json'):
                write_json(f'{OUT}/{name}.json', {'name': name, 'ended': 'interrupted (restart)', 'seconds': None})
            os.replace(f'{OUT}/{f}', f'{OUT}/{name}.mp3')
    with open(f'{APP}/recorder.py', 'rb') as src, open(RUNNING, 'wb') as dst:
        dst.write(src.read())
    seg, err, gone, stopping = None, None, 0, False
    blocked_until = 0

    def term(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, term)
    log('started')
    while True:
        gone = 0 if os.path.exists(f'{APP}/manifest.json') else gone + 1
        if gone >= GONE_LOOPS:
            removed(seg)
        want = setting('enabled', '0') == '1'
        seg_s = max(60, int(setting('segment_min', '30')) * 60)
        call = routing.pending_voice()
        fm = free_mb()
        if seg:
            why = ('stopped' if not want else 'call' if call else 'service stopped' if stopping
                   else 'storage low' if fm < MIN_FREE_MB else 'segment' if seg.seconds() >= seg_s else None)
            if why:
                seg.close(why)
                seg = None
                if why == 'storage low':
                    err = f'存储不足（剩 {fm} MB），已停止录音'
                if why == 'call':
                    blocked_until = time.monotonic() + 3
        if stopping:
            wake(False)
            sys.exit(0)
        if want and not seg and fm < MIN_FREE_MB:
            err = f'存储不足（剩 {fm} MB），等电脑同步后再录'
        if not seg and want and not call and time.monotonic() >= blocked_until and fm >= MIN_FREE_MB:
            try:
                seg = Segment()
                err = None
            except BlockingIOError as e:
                err = f'麦克风被占用（{e}），稍后重试'
                blocked_until = time.monotonic() + 10
            except Exception as e:
                err = f'无法开始录音：{e}'
                log(err)
                blocked_until = time.monotonic() + 30
        wake(bool(seg))
        try:
            files = finished()
            write_json(STATE, {
                'recording': bool(seg), 'enabled': want, 'call': bool(call),
                'current': seg.name if seg else None, 'current_s': round(seg.seconds()) if seg else 0,
                'since': round(seg.start) if seg else None, 'error': err, 'free_mb': fm,
                'pending': len(files), 'pending_mb': round(sum(size(f'{OUT}/{n}.mp3') for n in files) / 1048576, 1),
                'last_sync': last_sync(), 'at': round(time.time())})
        except Exception as e:
            log(f'state: {e}')
        if seg:
            try:
                end = time.monotonic() + 1
                while time.monotonic() < end:
                    seg.pump(0.25)
                    # (a call waits 5 s for the lease: look every 0.25 s)
                    if routing.pending_voice():
                        break
            except Exception as e:
                err = f'录音中断：{e}'
                log(err)
                seg.close('error')
                seg = None
                blocked_until = time.monotonic() + 10
        else:
            time.sleep(1)


if __name__ == '__main__':
    try:
        main()
    finally:
        wake(False)
