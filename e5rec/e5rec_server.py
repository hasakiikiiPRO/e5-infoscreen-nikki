#!/usr/bin/env python3
"""e5rec-server -- the NAS half of the E5 recorder.

The E5 uploads each finished segment here as soon as it is closed, over its
mobile data, through the public frp tunnel; it is transcribed at once
(e5rec.transcribe: silero VAD + SenseVoice) into the same tree e5rec keeps:

  PUT /v1/rec/<rec-YYYYMMDD-HHMMSS>    the MP3 as the body
      Authorization: Bearer <token>, X-Sha256: <the MP3's>, X-Meta: <its .json, base64>
      -> 200 {"ok": true, "sha256", "stored"} once both files are on disk
         (fsync) and the SHA-256 matched: only then does the E5 delete its copy.
         The same recording again (a lost reply): 200 again, nothing written.
  GET  /v1/health                      -> {"ok": true}   (no token)

Config (/vol2/1000/e5rec/server.json, mode 600):
  {"token": "...", "port": 18420, "cert": "tls/cert.pem", "key": "tls/key.pem"}
Recordings: E5REC_ROOT/YYYY-MM-DD/rec-*.mp3 / .json / .txt / .transcript.json, index.db
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import ssl
import sys
import threading
import time
from pathlib import Path

import e5rec

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / 'server.json').read_text())
TOKEN = CFG['token'].encode()
NAME = re.compile(r'^rec-\d{8}-\d{6}$')
MAX_BYTES = 300 << 20
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
    with store_lock:
        st = e5rec.load_state()
        st['last_upload'] = {'at': round(time.time()), 'name': name, 'from': client}
        e5rec.save_state(st)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'e5rec'
    sys_version = ''

    def reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log(self.client_address[0], fmt % args)

    def do_GET(self):
        if self.path == '/v1/health':
            return self.reply(200, {'ok': True})
        self.reply(404, {'ok': False, 'error': 'not found'})

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
            json.loads(meta)
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
        with store_lock:
            # (the same name with other content: kept apart, never overwritten)
            local, n = name, 1
            while (day / f'{local}.mp3').exists():
                if file_sha(day / f'{local}.mp3') == want:
                    part.unlink(missing_ok=True)
                    if not (day / f'{local}.json').exists():
                        write_durable(day / f'{local}.json', meta)
                    log(f'{name}: already here (as {local})')
                    return self.reply(200, {'ok': True, 'sha256': want, 'stored': local})
                local, n = f'{name}-dup{n}', n + 1
            # (the .json first: the transcriber reads it when it sees the .mp3)
            write_durable(day / f'{local}.json', meta)
            durable(part, day / f'{local}.mp3')
        log(f'{name}: stored, {length} bytes' + (f' (as {local})' if local != name else ''))
        note_upload(local, self.client_address[0])
        new_audio.set()
        self.reply(200, {'ok': True, 'sha256': want, 'stored': local})


def transcriber():
    while True:
        new_audio.wait(300)
        new_audio.clear()
        try:
            e5rec.transcribe()
        except Exception as e:
            log(f'transcribe: {e}')


def main():
    e5rec.ROOT.mkdir(parents=True, exist_ok=True)
    e5rec.log = log
    threading.Thread(target=transcriber, daemon=True).start()
    new_audio.set()                     # (whatever arrived while it was down)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', int(CFG.get('port', 18420))), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(HERE / CFG['cert'], HERE / CFG['key'])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    log(f'listening on 127.0.0.1:{srv.server_address[1]}, recordings in {e5rec.ROOT}')
    srv.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
