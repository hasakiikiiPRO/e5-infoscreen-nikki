#!/usr/bin/env python3
"""e5rec-server -- the NAS half of the E5 recorder.

The E5 uploads each finished segment here as soon as it is closed, over its
mobile data, through the public frp tunnel; it is transcribed at once
(e5rec.transcribe: silero VAD + DeepSeek ASR, SenseVoice where that fails)
into the same tree e5rec keeps, then summed up (e5sum, DeepSeek chat):

  PUT /v1/rec/<rec-YYYYMMDD-HHMMSS>    the MP3 as the body
      Authorization: Bearer <token>, X-Sha256: <the MP3's>, X-Meta: <its .json, base64>
      -> 200 {"ok": true, "sha256", "stored"} once both files are on disk
         (fsync) and the SHA-256 matched: only then does the E5 delete its copy.
         The same recording again (a lost reply): 200 again, nothing written.
  GET  /v1/health                      -> {"ok": true}   (no token)

Reading (the token -- the E5's screen app -- or the web page's session cookie):
  GET /v1/recent?limit=&before=        recordings, newest first, with their AI summaries
  GET /v1/rec/<name>                   one recording: sentences with times, its summary
  GET /v1/days?limit=                  days with recordings, each day's AI summary title
  GET /v1/day/<YYYY-MM-DD>             a day's AI summary and its recordings
  GET /v1/search?q=                    sentences containing all the words
  GET /v1/audio/<name>                 the MP3 (the web page's player; Range works)
  GET /                                the web page (web.html); POST /v1/login {"password"} sets the cookie

The pocket assistant (the E5's held OK key):
  POST /v1/assistant                   a WAV of the question (<= 8 MiB, <= 120 s) -> {question, answer,
                                       audio: the answer spoken, a 24 kHz WAV, base64}

Config (/vol2/1000/e5rec/server.json, mode 600):
  {"token": "...", "web_password": "...", "port": 18420, "cert": "tls/cert.pem", "key": "tls/key.pem"}
Recordings: E5REC_ROOT/YYYY-MM-DD/rec-*.mp3 / .json / .txt / .transcript.json / .summary.json, index.db
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import e5assist
import e5rec
import e5sum

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / 'server.json').read_text())
TOKEN = CFG['token'].encode()
WEB_PASSWORD = CFG.get('web_password', '').encode()
# (the E5 names a second segment in the same second, or one after a clock step back, rec-…-N)
NAME = re.compile(r'^rec-\d{8}-\d{6}(-\d+)?$')
STORED = re.compile(r'^rec-\d{8}-\d{6}(-\d+)?(-dup\d+)?$')
MAX_BYTES = 300 << 20
SESSION_DAYS = 90
# Logins: frp makes every client 127.0.0.1, so no per-address limit is possible -- one attempt
# at a time for everyone, a pause that doubles with each failure in a row, and after
# LOCK_AFTER failures in a row no login at all for an hour (the right password included).
LOCK_AFTER, LOCK_S = 10, 3600
login_lock = threading.Lock()
login_fails = {'n': 0, 'until': 0.0}
# (requests at once: a slow client holds one; past this new ones are closed)
slots = threading.BoundedSemaphore(64)
new_audio = threading.Event()
store_lock = threading.Lock()


def log(*a):
    print(time.strftime('%F %T'), *a, flush=True)


def file_sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def durable(tmp, dest):
    os.replace(tmp, dest)
    fd = os.open(dest.parent, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)


def write_durable(dest, data):
    tmp = dest.with_name(dest.name + '.part')
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    durable(tmp, dest)


def note_upload(name, client):
    e5rec.update_state(lambda st: st.__setitem__('last_upload', {'at': round(time.time()), 'name': name, 'from': client}))


# ---------- web sessions: random, kept server side, ended by logging out or a new password ----------

def pw_mark():
    return hashlib.sha256(b'e5rec web ' + WEB_PASSWORD).hexdigest()[:16]


def sessions_db():
    c = e5rec.db()
    c.execute('create table if not exists web_sessions (id text primary key, exp integer, pw text)')
    return c


def session_new():
    sid = secrets.token_urlsafe(32)
    c = sessions_db()
    with c:
        c.execute('delete from web_sessions where exp < ? or pw != ?', (int(time.time()), pw_mark()))
        c.execute('insert into web_sessions values (?,?,?)',
                  (hashlib.sha256(sid.encode()).hexdigest(), int(time.time()) + SESSION_DAYS * 86400, pw_mark()))
    return sid


def session_ok(sid):
    if not sid or len(sid) > 100:
        return False
    r = sessions_db().execute('select exp, pw from web_sessions where id=?',
                              (hashlib.sha256(sid.encode()).hexdigest(),)).fetchone()
    return bool(r and r[0] > time.time() and hmac.compare_digest(r[1], pw_mark()))


def session_end(sid):
    if sid:
        c = sessions_db()
        with c:
            c.execute('delete from web_sessions where id=?', (hashlib.sha256(sid.encode()).hexdigest(),))


# ---------- reading ----------

def summary_of(sj):
    try:
        return json.loads(sj) if sj else None
    except ValueError:
        return None


REC_SQL = ('select r.name, r.start, r.seconds, r.segments, s.json, '
           "(select group_concat(text, ' ') from (select text from segments g where g.name = r.name order by rel limit 2)) "
           'from recordings r left join summaries s on s.name = r.name')


def rec_row(r):
    name, start, seconds, segments, sj, opening = r
    s = summary_of(sj) or {}
    return {'name': name, 'start': start, 'seconds': round(seconds or 0), 'sentences': segments or 0,
            'title': s.get('title') or '', 'summary': s.get('summary') or '', 'todos': s.get('todos') or [],
            'empty': bool(s.get('empty')), 'summarized': bool(sj), 'opening': opening or ''}


def db():
    c = e5rec.db()
    e5sum.tables(c)
    return c


def api_recent(q):
    c = db()
    limit = max(1, min(200, int((q.get('limit') or ['30'])[0])))
    before = (q.get('before') or [''])[0]
    rows = c.execute(REC_SQL + (' where r.name < ?' if before else '') + ' order by r.start desc limit ?',
                     ([before] if before else []) + [limit]).fetchall()
    done = {n for (n,) in c.execute('select name from recordings where transcribed_at is not null')}
    failed = e5rec.load_state().get('failed', {})
    left = [p.stem for p in e5rec.ROOT.glob('*/rec-*.mp3') if p.stem not in done]
    return {'recordings': [rec_row(r) for r in rows],
            'transcribing': sorted((n for n in left if failed.get(n, 0) < 3), reverse=True),
            'failed': sorted((n for n in left if failed.get(n, 0) >= 3), reverse=True)}


def api_rec(name):
    c = db()
    r = c.execute(REC_SQL + ' where r.name = ?', (name,)).fetchone()
    if not r:
        return None
    s = summary_of(r[4]) or {}
    segs = c.execute('select start, text from segments where name=? order by rel', (name,)).fetchall()
    return {**rec_row(r), 'points': s.get('points') or [], 'segments': [{'t': t, 'text': x} for t, x in segs]}


def api_days(q):
    c = db()
    limit = max(1, min(366, int((q.get('limit') or ['30'])[0])))
    rows = c.execute('select r.day, count(*), sum(r.seconds), sum(r.segments), d.json from recordings r '
                     'left join daily d on d.day = r.day group by r.day order by r.day desc limit ?', (limit,)).fetchall()
    return {'days': [{'day': d, 'recordings': n, 'minutes': round((secs or 0) / 60), 'sentences': segs or 0,
                      'title': (summary_of(j) or {}).get('title') or ''} for d, n, secs, segs, j in rows]}


def api_day(day):
    c = db()
    d = c.execute('select at, json from daily where day=?', (day,)).fetchone()
    rows = c.execute(REC_SQL + ' where r.day = ? order by r.start', (day,)).fetchall()
    return {'day': day, 'summary': summary_of(d[1]) if d else None, 'summary_at': d[0] if d else None,
            'recordings': [rec_row(r) for r in rows]}


def api_search(q):
    words = [w for w in (q.get('q') or [''])[0].split() if w][:5]
    if not words:
        return {'hits': []}
    rows = db().execute('select name, start, text from segments where ' + ' and '.join(['text like ?'] * len(words))
                        + ' order by start desc limit 200', [f'%{w}%' for w in words]).fetchall()
    return {'hits': [{'name': n, 't': t, 'text': x} for n, t, x in rows]}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'e5rec'
    sys_version = ''
    timeout = 120                       # (per read, once the request line and headers are in)

    def setup(self):
        super().setup()
        # (the TLS handshake and the headers: 20 s at most, whoever is slow)
        self.connection.settimeout(20)

    def parse_request(self):
        ok = super().parse_request()
        self.connection.settimeout(self.timeout)
        return ok

    def cookie(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'e5s':
                return v
        return ''

    def reply(self, code, obj, headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log(self.client_address[0], fmt % args)

    def authed(self):
        if hmac.compare_digest(self.headers.get('Authorization', '').encode(), b'Bearer ' + TOKEN):
            return True
        return session_ok(self.cookie())

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/v1/health':
            return self.reply(200, {'ok': True})
        if u.path in ('/', '/index.html'):
            body = (HERE / 'web.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; media-src 'self'; img-src 'self' data:")
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.authed():
            time.sleep(0.5)
            return self.reply(401, {'ok': False, 'error': 'unauthorized'})
        try:
            if u.path == '/v1/recent':
                return self.reply(200, api_recent(q))
            if u.path == '/v1/days':
                return self.reply(200, api_days(q))
            if u.path == '/v1/search':
                return self.reply(200, api_search(q))
            m = re.fullmatch(r'/v1/(rec|day|audio)/([^/]+)', u.path)
            if m and m.group(1) == 'day' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', m.group(2)):
                return self.reply(200, api_day(m.group(2)))
            if m and m.group(1) == 'rec' and STORED.match(m.group(2)):
                r = api_rec(m.group(2))
                return self.reply(200, r) if r else self.reply(404, {'ok': False, 'error': 'no such recording'})
            if m and m.group(1) == 'audio' and STORED.match(m.group(2)):
                return self.send_audio(e5rec.day_dir(m.group(2)) / f'{m.group(2)}.mp3')
        except ValueError:
            return self.reply(400, {'ok': False, 'error': 'bad request'})
        self.reply(404, {'ok': False, 'error': 'not found'})

    def send_audio(self, p):
        try:
            size = p.stat().st_size
        except OSError:
            return self.reply(404, {'ok': False, 'error': 'no audio'})
        # (Range: a browser's <audio> seeks with it)
        lo, hi = 0, size - 1
        m = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers.get('Range', ''))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                lo, hi = int(m.group(1)), min(size - 1, int(m.group(2) or size - 1))
            else:
                lo = max(0, size - int(m.group(2)))
            if lo > hi:
                return self.reply(416, {'ok': False, 'error': 'bad range'}, [('Content-Range', f'bytes */{size}')])
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {lo}-{hi}/{size}')
        else:
            self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(hi - lo + 1))
        self.end_headers()
        with open(p, 'rb') as f:
            f.seek(lo)
            left = hi - lo + 1
            while left > 0:
                chunk = f.read(min(left, 1 << 16))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        if self.path == '/v1/assistant':
            return self.assistant()
        if self.path == '/v1/logout':
            session_end(self.cookie())
            return self.reply(200, {'ok': True}, [('Set-Cookie', 'e5s=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict')])
        if self.path != '/v1/login':
            return self.reply(404, {'ok': False, 'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 4096:
            return self.reply(413, {'ok': False, 'error': 'bad length'})
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
            pw = str(body.get('password', '')) if isinstance(body, dict) else ''
        except ValueError:
            pw = ''
        with login_lock:
            now = time.time()
            if login_fails['until'] > now:
                return self.reply(429, {'ok': False, 'error': f'试错太多，{int(login_fails["until"] - now) // 60 + 1} 分钟后再试'})
            if not WEB_PASSWORD or not hmac.compare_digest(pw.encode(), WEB_PASSWORD):
                login_fails['n'] += 1
                if login_fails['n'] >= LOCK_AFTER:
                    login_fails['until'] = now + LOCK_S
                    log(f'login: {login_fails["n"]} wrong passwords in a row, logins locked for an hour')
                else:
                    log(f'login refused ({login_fails["n"]} in a row)')
                # (held inside the lock: the next try, from anyone, waits this out too)
                time.sleep(min(60, 2 ** login_fails['n']))
                return self.reply(401, {'ok': False, 'error': '密码不对'})
            login_fails.update(n=0, until=0.0)
        self.reply(200, {'ok': True},
                   [('Set-Cookie', f'e5s={session_new()}; Max-Age={SESSION_DAYS * 86400}; Path=/; Secure; HttpOnly; SameSite=Strict')])

    def assistant(self):
        if not self.authed():
            time.sleep(0.5)
            return self.reply(401, {'ok': False, 'error': 'unauthorized'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if not 44 < n <= 8 << 20:
            return self.reply(413, {'ok': False, 'error': 'bad length'})
        wav = self.rfile.read(n)
        if len(wav) != n or wav[:4] != b'RIFF' or wav[8:12] != b'WAVE':
            return self.reply(400, {'ok': False, 'error': 'not a WAV'})
        try:
            try:
                boost = max(0, min(14, int(self.headers.get('X-Boost') or 8)))
            except ValueError:
                boost = 8
            r = e5assist.ask(wav, locked=self.headers.get('X-Locked') == '1', boost_db=boost)
        except Exception as e:
            log(f'assistant: {e}')
            return self.reply(502, {'ok': False, 'error': '助手出错了'})
        self.reply(200, {'ok': True, 'question': r['question'], 'answer': r['answer'], 'timing': r['timing'],
                         'audio': base64.b64encode(r['wav']).decode()})

    def do_PUT(self):
        auth = self.headers.get('Authorization', '')
        if not hmac.compare_digest(auth.encode(), b'Bearer ' + TOKEN):
            time.sleep(1)
            return self.reply(401, {'ok': False, 'error': 'unauthorized'})
        m = re.fullmatch(r'/v1/rec/([^/]+)', self.path)
        name = m.group(1) if m else ''
        want = (self.headers.get('X-Sha256') or '').lower()
        try:
            length = int(self.headers.get('Content-Length', ''))
            meta = base64.b64decode(self.headers.get('X-Meta', ''), validate=True)
            m = json.loads(meta)
            if not isinstance(m, dict) or not isinstance(m.get('start'), (int, type(None))):
                raise ValueError('meta')
        except ValueError:
            return self.reply(400, {'ok': False, 'error': 'bad length or meta'})
        if not NAME.match(name) or not re.fullmatch(r'[0-9a-f]{64}', want) or not 0 < length <= MAX_BYTES:
            return self.reply(400, {'ok': False, 'error': 'bad name, hash or size'})
        day = e5rec.day_dir(name)
        day.mkdir(parents=True, exist_ok=True)
        part = day / f'.{name}.{threading.get_ident()}.upload'
        h = hashlib.sha256()
        left = length
        try:
            with open(part, 'wb') as f:
                while left:
                    chunk = self.rfile.read(min(left, 1 << 16))
                    if not chunk:
                        raise ConnectionError('short body')
                    h.update(chunk)
                    f.write(chunk)
                    left -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
        except (OSError, ConnectionError) as e:
            part.unlink(missing_ok=True)
            log(f'{name}: upload broken off: {e}')
            return
        if h.hexdigest() != want:
            part.unlink(missing_ok=True)
            return self.reply(422, {'ok': False, 'error': 'sha256 mismatch'})
        try:
            local = self.store(name, day, part, want, meta)
        except OSError as e:
            part.unlink(missing_ok=True)
            log(f'{name}: could not store: {e}')
            return self.reply(500, {'ok': False, 'error': 'could not store'})
        if local is None:
            return self.reply(200, {'ok': True, 'sha256': want, 'stored': self.already})
        log(f'{name}: stored, {length} bytes' + (f' (as {local})' if local != name else ''))
        note_upload(local, self.client_address[0])
        new_audio.set()
        self.reply(200, {'ok': True, 'sha256': want, 'stored': local})

    def store(self, name, day, part, want, meta):
        """The verified upload into place; None when it was here already (self.already: as what)."""
        with store_lock:
            # (the same name with other content: kept apart, never overwritten)
            local, n = name, 1
            while (day / f'{local}.mp3').exists():
                if file_sha(day / f'{local}.mp3') == want:
                    part.unlink(missing_ok=True)
                    if not (day / f'{local}.json').exists():
                        write_durable(day / f'{local}.json', meta)
                    log(f'{name}: already here (as {local})')
                    self.already = local
                    return None
                local, n = f'{name}-dup{n}', n + 1
            # (the .json first: the transcriber reads it when it sees the .mp3)
            write_durable(day / f'{local}.json', meta)
            durable(part, day / f'{local}.mp3')
            return local


def transcriber():
    while True:
        new_audio.wait(300)
        new_audio.clear()
        try:
            e5rec.transcribe()
        except Exception as e:
            log(f'transcribe: {e}')
        # (then the AI summaries: each new recording's, and the days' whose recordings changed)
        try:
            c = db()
            n = e5sum.summarize_pending(c)
            if n:
                log(f'summary: {n} new')
            e5sum.daily_pending(c)
        except Exception as e:
            log(f'summary: {e}')


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        if not slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            slots.release()

    def handle_error(self, request, client_address):
        # (a scanner's failed TLS handshake: one line, not a traceback)
        log(f'{client_address[0]} connection error: {sys.exc_info()[1]}')


def main():
    e5rec.ROOT.mkdir(parents=True, exist_ok=True)
    e5rec.log = log
    threading.Thread(target=transcriber, daemon=True).start()
    new_audio.set()                     # (whatever arrived while it was down)
    srv = Server(('127.0.0.1', int(CFG.get('port', 18420))), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(HERE / CFG['cert'], HERE / CFG['key'])
    # (the handshake in each connection's own thread, not in accept(): a slow client holds up no one)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    log(f'listening on 127.0.0.1:{srv.server_address[1]}, recordings in {e5rec.ROOT}')
    srv.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
