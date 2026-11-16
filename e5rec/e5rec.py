#!/home/hasakiikii/cc/e5-recorder/.venv/bin/python
"""e5rec -- the computer half of the E5 recorder.

  e5rec sync         find the E5 (USB 192.168.9.1, or its MAC on the home WiFi),
                     copy finished recordings, verify them (SHA-256), delete
                     them on the E5, and tell it when that was (.last_sync)
  e5rec transcribe   recordings without a transcript -> text with clock times
                     (silero VAD + SenseVoice, offline, sherpa-onnx)
  e5rec run          both (the systemd timer runs this)
  e5rec status       what the E5 and this side have
  e5rec record on|off  start or stop recording on the E5

Recordings and transcripts live in ~/录音/E5/YYYY-MM-DD/:
  rec-YYYYMMDD-HHMMSS.mp3 / .json (the E5's) / .transcript.json / .txt
and every transcript segment in ~/录音/E5/index.db (SQLite) for searching.
"""
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(os.environ.get('E5REC_ROOT') or Path.home() / '录音' / 'E5')
DB = ROOT / 'index.db'
STATE = ROOT / '.e5rec-state.json'
KEY = Path.home() / '.ssh' / 'e5_rec'
E5_DIR = '/mnt/e5-data/e5rec'
USB_IP = '192.168.9.1'
# ~/.config/e5rec/config.json: {"macs": [the E5's MACs -- its hotspot's and its
# client's on the home WiFi], "sensevoice": model dir, "vad": silero_vad.onnx}
CONFIG = Path.home() / '.config' / 'e5rec' / 'config.json'
try:
    _cfg = json.loads(CONFIG.read_text())
except (OSError, ValueError):
    _cfg = {}
E5_MACS = {m.lower() for m in _cfg.get('macs', [])}
SENSEVOICE = Path(_cfg.get('sensevoice') or Path.home() / 'models' / 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17')
VAD = Path(_cfg.get('vad') or Path.home() / 'models' / 'silero_vad.onnx')
# "deepseek": each VAD sentence to DeepSeek's ASR (api.deepseek.com/v1/tools/asr), the local
# SenseVoice only where that fails; anything else: SenseVoice only
ASR = _cfg.get('asr', 'sensevoice')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY') or _cfg.get('deepseek_api_key', '')
DEEPSEEK_ASR = 'https://api.deepseek.com/v1/tools/asr'
NAME = re.compile(r'^rec-(\d{8})-(\d{6})(-\d+)?$')
KNOWN_HOSTS = Path.home() / '.config' / 'e5rec' / 'known_hosts'


def log(*a):
    print(time.strftime('%F %T'), *a, flush=True)


_state_lock = __import__('threading').RLock()


def load_state():
    try:
        st = json.loads(STATE.read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st):
    ROOT.mkdir(parents=True, exist_ok=True)
    # (each writer its own temp file: two threads on one name could interleave into bad JSON)
    tmp = STATE.with_name(f'{STATE.name}.{os.getpid()}.{__import__("threading").get_ident()}.tmp')
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(STATE)


def update_state(fn):
    """Read, change (fn(st)), write -- one at a time across the threads of this process."""
    with _state_lock:
        st = load_state()
        fn(st)
        save_state(st)
        return st


# ---------- reaching the E5 ----------

def ssh_argv(ip, cmd, stdin=False):
    # The E5's host key is pinned under one alias whatever its address (USB or
    # home WiFi): an impostor answering "e5" is refused.  stdin never comes from
    # ours (the MCP server's protocol pipe) unless something is sent.
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    return (['ssh'] + ([] if stdin else ['-n']) +
            ['-i', str(KEY), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
             '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
             '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={KNOWN_HOSTS}',
             '-o', 'HostKeyAlias=e5rec-e5', '-o', 'LogLevel=ERROR', f'root@{ip}', cmd])


def ssh(ip, cmd, timeout=30, input=None):
    try:
        return subprocess.run(ssh_argv(ip, cmd, stdin=input is not None), capture_output=True, text=True,
                              timeout=timeout, input=input,
                              **({} if input is not None else {'stdin': subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 255, '', 'timeout')


def is_e5(ip):
    r = ssh(ip, 'cat /etc/e5/device-profile', timeout=12)
    return r.returncode == 0 and r.stdout.strip() == 'e5'


def neighbours():
    out = subprocess.run(['ip', 'neigh'], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    found = {}
    for line in out.splitlines():
        m = re.match(r'^(\d+\.\d+\.\d+\.\d+) .* lladdr ([0-9a-f:]+)', line)
        if m and m.group(2) in E5_MACS:
            found[m.group(1)] = m.group(2)
    return list(found)


def local_nets():
    out = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stdout
    nets = []
    for line in out.splitlines():
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+/\d+)', line)
        if not m or 'docker' in line or 'lxc' in line or ' tun' in line or 'Meta' in line:
            continue
        net = ipaddress.ip_interface(m.group(1)).network
        if net.prefixlen >= 22 and net.is_private and not str(net).startswith('192.168.9.'):
            nets.append(net)
    return nets


def find_e5():
    """The E5's address: USB first, then the last one, the neighbour table, a sweep."""
    st = load_state()
    tried = []
    for ip in [USB_IP, st.get('last_ip')] + neighbours():
        if ip and ip not in tried:
            tried.append(ip)
            if subprocess.run(['ping', '-c1', '-W1', ip], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0 and is_e5(ip):
                return ip
    # (a sweep of the home network fills the neighbour table)
    for net in local_nets():
        hosts = [str(h) for h in net.hosts()][:1022]
        with ThreadPoolExecutor(64) as ex:
            list(ex.map(lambda h: subprocess.run(['ping', '-c1', '-W1', h], capture_output=True, stdin=subprocess.DEVNULL), hosts))
    for ip in neighbours():
        if ip not in tried and is_e5(ip):
            return ip
    return None


# ---------- sync ----------

def day_dir(name):
    m = NAME.match(re.sub(r'-dup\d+$', '', name))
    d = m.group(1)
    return ROOT / f'{d[:4]}-{d[4:6]}-{d[6:]}'


class locked:
    """One sync or transcription at a time, whoever runs it (timer, command, MCP)."""
    def __init__(self, name):
        self.path = ROOT / f'.e5rec-{name}.lock'

    def __enter__(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, 'w')
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __exit__(self, *a):
        self.f.close()


def fetch(ip, f, dest, want):
    """Stream one file from the E5, hashed on the way, made durable: True if it verified."""
    tmp = dest.with_name(dest.name + '.part')
    h = hashlib.sha256()
    try:
        with open(tmp, 'wb') as out:
            p = subprocess.Popen(ssh_argv(ip, f'cat {E5_DIR}/{f}'), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
            for chunk in iter(lambda: p.stdout.read(1 << 16), b''):
                h.update(chunk)
                out.write(chunk)
            p.wait(timeout=60)
            out.flush()
            os.fsync(out.fileno())
    except (OSError, subprocess.TimeoutExpired):
        tmp.unlink(missing_ok=True)
        return False
    if p.returncode != 0 or h.hexdigest() != want:
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    fd = os.open(dest.parent, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)
    return True


def sync():
    with locked('sync') as ok:
        if not ok:
            log('sync: another sync is going on')
            return 0
        return _sync()


def _sync():
    ip = find_e5()
    st = load_state()
    if not ip:
        log('sync: E5 not reachable')
        st['last_seen_ok'] = False
        save_state(st)
        return 0
    st.update(last_ip=ip, last_seen=round(time.time()), last_seen_ok=True)
    save_state(st)
    # (names only: hashing a backlog of GBs in one call would never finish)
    r = ssh(ip, f'cd {E5_DIR} 2>/dev/null && ls rec-*.mp3 rec-*.json 2>/dev/null', timeout=30)
    have = set(r.stdout.split())
    names = sorted(n[:-4] for n in have if n.endswith('.mp3') and NAME.match(n[:-4]) and n[:-4] + '.json' in have)
    got = 0
    for name in names:
        h = ssh(ip, f'cd {E5_DIR} && sha256sum {name}.mp3 {name}.json', timeout=120)
        sums = {f: x for x, f in (l.split() for l in h.stdout.splitlines() if len(l.split()) == 2)}
        if len(sums) != 2:
            log(f'sync: {name}: could not hash on the E5, left for the next run')
            continue
        dest = day_dir(name)
        dest.mkdir(parents=True, exist_ok=True)
        # (a name already here with other content: kept apart, never overwritten)
        local = name
        n = 1
        while (dest / f'{local}.mp3').exists() and \
                hashlib.sha256((dest / f'{local}.mp3').read_bytes()).hexdigest() != sums[f'{name}.mp3']:
            local = f'{name}-dup{n}'
            n += 1
        ok = all(fetch(ip, f'{name}.{ext}', dest / f'{local}.{ext}', sums[f'{name}.{ext}']) for ext in ('mp3', 'json'))
        if not ok:
            log(f'sync: {name} did not verify, kept on the E5')
            continue
        ssh(ip, f'rm -f {E5_DIR}/{name}.mp3 {E5_DIR}/{name}.json')
        got += 1
        log(f'sync: {name} copied, verified and removed from the E5' + (f' (as {local})' if local != name else ''))
    stamp = json.dumps({'at': round(time.time()), 'files': got, 'host': os.uname().nodename})
    ssh(ip, f"cat > {E5_DIR}/.last_sync.tmp && mv {E5_DIR}/.last_sync.tmp {E5_DIR}/.last_sync", input=stamp)
    return got


# ---------- transcription ----------

_rec = None


def recognizer():
    global _rec
    if _rec is None:
        import sherpa_onnx
        _rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(SENSEVOICE / 'model.int8.onnx'), tokens=str(SENSEVOICE / 'tokens.txt'),
            use_itn=True, language='auto', num_threads=4)
    return _rec


def decode(path):
    import numpy as np
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-ac', '1', '-ar', '16000',
                          '-f', 's16le', '-'], capture_output=True, check=True, stdin=subprocess.DEVNULL,
                         timeout=1800).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def wav_bytes(samples):
    import io
    import wave
    import numpy as np
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())
    return b.getvalue()


def deepseek_asr(samples, tries=3):
    """One sentence (<= 20 s: the VAD's cap, the API takes 120 s) -> its text."""
    import urllib.error
    import urllib.request
    import uuid
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="s.wav"\r\n'
            'Content-Type: audio/wav\r\n\r\n').encode() + wav_bytes(samples) + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(DEEPSEEK_ASR, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + DEEPSEEK_KEY, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    # (a domestic API: never through a proxy)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for i in range(tries):
        try:
            with opener.open(req, timeout=60) as r:
                return json.loads(r.read())['text'].strip()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503, 504) or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise
        time.sleep(2 * (i + 1))


_local_lock = __import__('threading').Lock()


def local_asr(samples):
    # (one model, loaded once, used one at a time: the fallback is rare)
    with _local_lock:
        rec = recognizer()
        st = rec.create_stream()
        st.accept_waveform(16000, samples)
        rec.decode_stream(st)
        return st.result.text.strip()


def transcribe_file(mp3):
    """-> (segments [{start, end, text, engine}], seconds of audio)."""
    import sherpa_onnx
    samples = decode(mp3)
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(VAD)
    cfg.silero_vad.min_silence_duration = 0.5
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = 20
    cfg.sample_rate = 16000
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    speech = []

    def drain():
        while not vad.empty():
            speech.append((vad.front.start, vad.front.samples))
            vad.pop()
    win = cfg.silero_vad.window_size
    for i in range(0, len(samples), win):
        vad.accept_waveform(samples[i:i + win])
        drain()
    vad.flush()
    drain()

    # DeepSeek wants the sound around a sentence: the VAD cuts tight and it then loses the
    # first word (measured: 0.8 s before / 0.4 s after got all of a test exactly right) --
    # but never into the next or last sentence, whose words it would pick up
    PRE, POST = int(0.8 * 16000), int(0.4 * 16000)
    padded = []
    for i, (start, s) in enumerate(speech):
        lo = max(0, start - PRE, speech[i - 1][0] + len(speech[i - 1][1]) if i else 0)
        hi = min(len(samples), start + len(s) + POST, speech[i + 1][0] if i + 1 < len(speech) else len(samples))
        padded.append(samples[lo:hi])

    def one(i):
        start, s = speech[i]
        text, engine = None, 'sensevoice'
        if ASR == 'deepseek' and DEEPSEEK_KEY:
            try:
                text, engine = deepseek_asr(padded[i]), 'deepseek'
            except Exception as e:
                log(f'transcribe: {mp3.name} @{start / 16000:.1f}s: DeepSeek failed ({e}), local SenseVoice')
        if text is None:
            text = local_asr(s)
        return {'start': round(start / 16000, 2), 'end': round((start + len(s)) / 16000, 2),
                'text': text, 'engine': engine}
    # (DeepSeek allows 50 at once; the local fallback is one model, so few)
    with ThreadPoolExecutor(8 if ASR == 'deepseek' else 1) as pool:
        segs = [x for x in pool.map(one, range(len(speech))) if x['text']]
    return segs, len(samples) / 16000


def db():
    ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript('''
        create table if not exists recordings (name text primary key, day text, start integer,
            seconds real, path text, transcribed_at integer, segments integer, chars integer);
        create table if not exists segments (name text, start integer, end integer, rel real, text text);
        create index if not exists seg_name on segments(name);
        create index if not exists seg_start on segments(start);
    ''')
    return c


def clock(t):
    return time.strftime('%H:%M:%S', time.localtime(t))


def transcribe():
    with locked('transcribe') as ok:
        if not ok:
            log('transcribe: another transcription is going on')
            return 0
        return _transcribe()


def _transcribe():
    c = db()
    st = load_state()
    failed = st.get('failed', {})
    done = {r[0] for r in c.execute('select name from recordings where transcribed_at is not null')}
    # (one that failed three times is left: it would fail every run)
    todo = sorted(p for p in ROOT.glob('*/rec-*.mp3') if p.stem not in done and failed.get(p.stem, 0) < 3)
    for mp3 in todo:
        meta = {}
        try:
            meta = json.loads(mp3.with_suffix('.json').read_text())
            meta = meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            pass
        m = NAME.match(re.sub(r'-dup\d+$', '', mp3.stem))
        start = meta.get('start') if isinstance(meta.get('start'), int) else None
        start = start or int(time.mktime(time.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')))
        t0 = time.monotonic()
        try:
            segs, secs = transcribe_file(mp3)
        except Exception as e:
            log(f'transcribe: {mp3.name} failed: {e}')
            failed[mp3.stem] = failed.get(mp3.stem, 0) + 1
            update_state(lambda st: st.setdefault('failed', {}).__setitem__(mp3.stem, failed[mp3.stem]))
            continue
        out = {'name': mp3.stem, 'start': start, 'seconds': round(secs, 1), 'segments': segs}
        mp3.with_suffix('.transcript.json').write_text(json.dumps(out, ensure_ascii=False, indent=1))
        with open(mp3.with_suffix('.txt'), 'w') as f:
            f.write(f'# {mp3.stem}  {time.strftime("%F %T", time.localtime(start))}  时长 {secs / 60:.1f} 分钟\n\n')
            for s in segs:
                f.write(f'[{clock(start + s["start"])}] {s["text"]}\n')
        with c:
            c.execute('delete from segments where name=?', (mp3.stem,))
            c.executemany('insert into segments values (?,?,?,?,?)',
                          [(mp3.stem, int(start + s['start']), int(start + s['end']), s['start'], s['text']) for s in segs])
            c.execute('insert or replace into recordings values (?,?,?,?,?,?,?,?)',
                      (mp3.stem, mp3.parent.name, start, secs, str(mp3), int(time.time()), len(segs),
                       sum(len(s['text']) for s in segs)))
        log(f'transcribe: {mp3.name}: {secs:.0f} s audio, {len(segs)} segments, {time.monotonic() - t0:.1f} s')
    return len(todo)


# ---------- the E5's own state, and control ----------

def e5_status(ip=None):
    ip = ip or find_e5()
    if not ip:
        return None
    r = ssh(ip, 'curl -s -m 5 http://127.0.0.1:8088/api/plugins/recorder/status', timeout=20)
    try:
        s = json.loads(r.stdout)
    except ValueError:
        return {'ip': ip, 'error': 'no answer from the recorder app'}
    s.pop('files', None)
    s['ip'] = ip
    return s


def e5_record(on):
    ip = find_e5()
    if not ip:
        return {'ok': False, 'error': 'E5 不在线（USB 和家里 WiFi 都没找到）'}
    body = json.dumps({'on': bool(on)})
    r = ssh(ip, f"curl -s -m 5 -X POST -H 'Content-Type: application/json' -d '{body}' "
                'http://127.0.0.1:8088/api/plugins/recorder/record', timeout=20)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {'ok': False, 'error': r.stderr.strip() or 'no answer'}


def run():
    ROOT.mkdir(parents=True, exist_ok=True)
    return {'synced': sync(), 'transcribed': transcribe()}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'run'
    if cmd == 'sync':
        print(sync())
    elif cmd == 'transcribe':
        print(transcribe())
    elif cmd == 'run':
        print(json.dumps(run()))
    elif cmd == 'status':
        print(json.dumps(e5_status(), ensure_ascii=False, indent=1))
    elif cmd == 'record' and len(argv) > 2 and argv[2] in ('on', 'off'):
        print(json.dumps(e5_record(argv[2] == 'on'), ensure_ascii=False))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
