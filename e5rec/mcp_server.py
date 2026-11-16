#!/home/hasakiikii/cc/e5-recorder/.venv/bin/python
"""E5 recorder MCP server (stdio JSON-RPC 2.0).

Tools over the recordings the E5 makes and e5rec.py transcribes:
status, start/stop recording, sync now, list, search, read a transcript.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e5rec  # noqa: E402

DATE = {'type': 'string', 'description': '日期 YYYY-MM-DD（本地时间，含当天）'}
# On the NAS (E5REC_NAS=1): the E5 uploads by itself the moment each segment closes, so
# "sync now" only transcribes what is waiting, and the status says when the last one came
NAS = os.environ.get('E5REC_NAS') == '1'
TOOLS = [
    {'name': 'e5rec_status',
     'description': '荣悦 E5 录音状态：E5 是否在线、是否正在录音、当前这段录了多久、E5 上待同步的文件、电脑上已有的录音数和最近同步时间。',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'e5rec_record',
     'description': '开始或停止 E5 录音（E5 需在线：USB 或同一个家里 WiFi）。录音时 E5 锁屏会显示红点。'
                    '开始录音（on=true）前必须先得到用户本人在对话里的明确同意，不能自行决定开启；停止录音不需要。',
     'inputSchema': {'type': 'object', 'properties': {'on': {'type': 'boolean', 'description': 'true 开始，false 停止'}},
                     'required': ['on']}},
    {'name': 'e5rec_sync_now',
     'description': '立刻从 E5 拉取已完成的录音（平时每 5 分钟自动做一次），转文字在后台进行，稍后用 e5rec_list 查看。',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'e5rec_list',
     'description': '列出电脑上已有的录音（按时间倒序）：开始时间、时长、识别出的句数，以及开头几句。',
     'inputSchema': {'type': 'object', 'properties': {
         'date_from': DATE, 'date_to': DATE,
         'limit': {'type': 'integer', 'description': '最多几条，默认 20', 'default': 20}}}},
    {'name': 'e5rec_search',
     'description': '在录音转写文字里搜关键词，返回命中的句子、说话的时间点和所属录音。用于"我那天说过什么""谁提到了某事"这类问题。',
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': '关键词（多个词用空格分开，需同时出现）'},
         'date_from': DATE, 'date_to': DATE,
         'limit': {'type': 'integer', 'description': '最多几句，默认 50', 'default': 50}},
         'required': ['query']}},
    {'name': 'e5rec_transcript',
     'description': '读取一段录音的完整文字稿（每句带时间），或某一天某个时间段内所有录音的文字。',
     'inputSchema': {'type': 'object', 'properties': {
         'name': {'type': 'string', 'description': '录音名，如 rec-20261116-124701（来自 e5rec_list / e5rec_search）'},
         'date': DATE,
         'time_from': {'type': 'string', 'description': '时间 HH:MM，配合 date'},
         'time_to': {'type': 'string', 'description': '时间 HH:MM，配合 date'}}}},
]


def epoch(date, hm='00:00', end=False):
    t = time.mktime(time.strptime(f'{date} {hm}', '%Y-%m-%d %H:%M'))
    return t + (86400 if end and hm == '00:00' else 59 if end else 0)


def con():
    c = e5rec.db()
    c.row_factory = sqlite3.Row
    return c


def fmt(t):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))


def t_status(a):
    s = e5rec.e5_status()
    c = con()
    n, secs = c.execute('select count(*), coalesce(sum(seconds),0) from recordings').fetchone()
    st = e5rec.load_state()
    out = {'computer': {'recordings': n, 'hours': round(secs / 3600, 2), 'folder': str(e5rec.ROOT),
                        'last_seen_e5': fmt(st['last_seen']) if st.get('last_seen') else None}}
    if NAS:
        lu = st.get('last_upload') or {}
        out['computer']['where'] = 'NAS（E5 每段录完经流量/WiFi 直接上传）'
        out['computer']['last_upload'] = {'at': fmt(lu['at']), 'recording': lu.get('name')} if lu.get('at') else None
        waiting = [p.stem for p in e5rec.ROOT.glob('*/rec-*.mp3')
                   if not c.execute('select 1 from recordings where name=? and transcribed_at is not null', (p.stem,)).fetchone()]
        out['computer']['not_transcribed_yet'] = waiting
    if s is None:
        out['e5'] = {'online': False, 'note': ('NAS 连不上 E5 本身（它不在家里 WiFi 时就是这样）；录音照常，每段录完自动上传，看 last_upload'
                                               if NAS else 'USB 和家里 WiFi 都没找到 E5；它在外面时录音照常进行，回来后自动同步')}
    else:
        out['e5'] = {'online': True, 'ip': s.get('ip'), 'recording': s.get('recording'), 'enabled': s.get('enabled'),
                     'current_seconds': s.get('current_s'), 'in_call': s.get('call'), 'error': s.get('error'),
                     'pending_files': s.get('pending'), 'pending_mb': s.get('pending_mb'),
                     'free_mb': s.get('free_mb')}
    return out


def t_record(a):
    return e5rec.e5_record(bool(a.get('on')))


def t_sync(a):
    if NAS:
        n = e5rec.transcribe()
        return {'transcribed_now': n, 'note': 'E5 每段录完会自己上传到 NAS；这里只是立刻把还没转的转成文字'}
    got = e5rec.sync()
    # (transcription can take minutes: in the background, not holding the protocol)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / 'e5rec.py'), 'transcribe'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {'synced': got, 'transcription': '已在后台开始，稍后用 e5rec_list 查看'}


def range_clause(a, col):
    q, p = [], []
    if a.get('date_from'):
        q.append(f'{col} >= ?'); p.append(epoch(a['date_from']))
    if a.get('date_to'):
        q.append(f'{col} < ?'); p.append(epoch(a['date_to'], end=True))
    return q, p


def t_list(a):
    c = con()
    q, p = range_clause(a, 'start')
    rows = c.execute('select * from recordings' + (' where ' + ' and '.join(q) if q else '') +
                     ' order by start desc limit ?', p + [int(a.get('limit') or 20)]).fetchall()
    out = []
    for r in rows:
        first = [s['text'] for s in c.execute('select text from segments where name=? order by rel limit 3', (r['name'],))]
        out.append({'name': r['name'], 'start': fmt(r['start']), 'minutes': round((r['seconds'] or 0) / 60, 1),
                    'sentences': r['segments'], 'opening': ' '.join(first)})
    pending = sorted(p.stem for p in e5rec.ROOT.glob('*/rec-*.mp3')
                     if not c.execute('select 1 from recordings where name=? and transcribed_at is not null', (p.stem,)).fetchone())
    return {'recordings': out, 'not_transcribed_yet': pending}


def t_search(a):
    words = [w for w in str(a.get('query', '')).split() if w]
    if not words:
        return {'error': 'query 不能为空'}
    q, p = range_clause(a, 'start')
    for w in words:
        q.append('text like ?'); p.append(f'%{w}%')
    rows = con().execute('select name, start, text from segments where ' + ' and '.join(q) +
                         ' order by start limit ?', p + [int(a.get('limit') or 50)]).fetchall()
    return {'hits': [{'time': fmt(r['start']), 'recording': r['name'], 'text': r['text']} for r in rows],
            'count': len(rows)}


def t_transcript(a):
    c = con()
    if a.get('name'):
        rows = c.execute('select start, text from segments where name=? order by rel', (a['name'],)).fetchall()
        rec = c.execute('select * from recordings where name=?', (a['name'],)).fetchone()
        if not rec:
            return {'error': f'没有这段录音：{a["name"]}（可能还没同步或转写）'}
        head = {'name': rec['name'], 'start': fmt(rec['start']), 'minutes': round((rec['seconds'] or 0) / 60, 1)}
    elif a.get('date'):
        lo = epoch(a['date'], a.get('time_from') or '00:00')
        hi = epoch(a['date'], a['time_to'], end=True) if a.get('time_to') else epoch(a['date'], end=True)
        rows = c.execute('select start, text from segments where start >= ? and start < ? order by start',
                         (lo, hi)).fetchall()
        head = {'from': fmt(lo), 'to': fmt(hi)}
    else:
        return {'error': '需要 name，或 date（可加 time_from / time_to）'}
    text = '\n'.join(f'[{time.strftime("%H:%M:%S", time.localtime(r["start"]))}] {r["text"]}' for r in rows)
    if len(text) > 60000:
        text = text[:60000] + '\n…（太长，已截断；请缩小时间范围）'
    return {**head, 'sentences': len(rows), 'text': text}


HANDLERS = {'e5rec_status': t_status, 'e5rec_record': t_record, 'e5rec_sync_now': t_sync,
            'e5rec_list': t_list, 'e5rec_search': t_search, 'e5rec_transcript': t_transcript}


def reply(id_, result=None, error=None):
    msg = {'jsonrpc': '2.0', 'id': id_}
    if error:
        msg['error'] = error
    else:
        msg['result'] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def main():
    # (e5rec logs go to stderr here: stdout is the protocol)
    e5rec.log = lambda *x: print(*x, file=sys.stderr, flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            reply(None, error={'code': -32700, 'message': 'parse error'})
            continue
        if not isinstance(req, dict):
            reply(None, error={'code': -32600, 'message': 'invalid request'})
            continue
        try:
            dispatch(req)
        except Exception as e:
            if req.get('id') is not None:
                reply(req.get('id'), error={'code': -32603, 'message': str(e)})


def dispatch(req):
    m, id_ = req.get('method'), req.get('id')
    params = req.get('params') or {}
    if m == 'initialize':
        reply(id_, {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'e5rec', 'version': '1.1'}})
    elif m == 'tools/list':
        reply(id_, {'tools': TOOLS})
    elif m == 'tools/call':
        h = HANDLERS.get(params.get('name'))
        if not h:
            reply(id_, error={'code': -32601, 'message': f'unknown tool {params.get("name")}'})
            return
        try:
            res = h(params.get('arguments') or {})
            reply(id_, {'content': [{'type': 'text', 'text': json.dumps(res, ensure_ascii=False, indent=1)}]})
        except Exception as e:
            reply(id_, {'content': [{'type': 'text', 'text': f'出错：{e}'}], 'isError': True})
    elif m == 'ping':
        reply(id_, {})
    elif id_ is not None:
        reply(id_, error={'code': -32601, 'message': f'unknown method {m}'})


if __name__ == '__main__':
    main()
