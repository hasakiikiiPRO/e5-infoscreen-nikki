"""e5assist -- the pocket voice assistant's brain, on the NAS.

Tools it may call, as many rounds as it needs (MAX_ROUNDS): web_search (the user's own
four-engine search -- Exa, Bing, SearXNG, Grok -- the endpoint Claude Desktop uses,
websearch.json), DeepSeek's own quick search and page reading, and the recordings a layer at a
time: which days, a day's summaries, a search through every sentence, a transcript.  From the
lock screen none of the recordings' tools.

ask(wav) -> {question, answer, wav}: what was said (DeepSeek ASR), an answer
that will be read aloud (deepseek-flash, web search on, with the recordings'
summaries and the last two hours of what was said as background), and that
answer spoken (DeepSeek TTS, 24 kHz mono WAV).  A short memory of the last
turns lets a follow-up ("那明天呢？") make sense.
"""
import io
import json
import re
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
import urllib.error
import urllib.request

import e5rec
import e5sum

CHAT = 'https://api.deepseek.com/chat/completions'
TTS = 'https://api.deepseek.com/v1/tools/tts'
VOICE = 'stella'                       # (DeepSeek's two: stella, a woman's voice; tide, a man's)
PLACE = '浙江温州'
MEMORY_S = 10 * 60                     # follow-ups within this long see the earlier turns
MEMORY_TURNS = 6
MAX_ROUNDS = 8                          # tool rounds in one answer
ANSWER_BUDGET_S = 170                   # (the E5 waits 240 s for everything)
WEBSEARCH = Path(__file__).resolve().parent / 'websearch.json'
# the Obsidian vault's WebDAV (remotely-save's target, plain files): a saved note lands in
# NOTE_DIR and reaches the computer and the phone on their next sync
OBSIDIAN = Path(__file__).resolve().parent / 'obsidian-webdav.json'
NOTE_DIR = 'E5助手'
# how hard it thinks (the E5's 思考程度): (model, thinking effort or None) -- measured on a planning
# question: none 1.5 s, shallow; high 4 s, well reasoned; max 11.6 s, planned to the hour
# (v4-pro: 12 s and no better than flash at high, so not offered)
MODES = {'fast': ('deepseek-flash', None), 'smart': ('deepseek-flash', 'high'), 'max': ('deepseek-flash', 'max')}
DEFAULT_MODE = 'max'
RECENT_S = 2 * 3600
MAX_CONTEXT = 24000

SYSTEM = ('你是用户随身带的口袋语音助手（装在一台叫 E5 的小设备上）。你的回答会被直接朗读出来：'
          '用口语；想清楚再答，该详细就详细，给出有用的结论、理由和具体安排；'
          '不要 Markdown、列表符号、链接、表情（会被朗读出来）。'
          '需要实时信息就搜：简单的（天气、时间、单个事实）用自带的快速搜索；要全面、要核对、要最新动态或多方说法，'
          '用 web_search（四个搜索引擎一起搜），必要时再读网页原文。'
          '这台设备一直在录用户身边的对话：问到用户自己说过什么、答应过什么、某天做了什么，就用 recordings_ 开头的工具一层层查'
          '（先看有哪些日子或当天总结，再搜关键词，最后读原文），引用时说清是什么时候说的；录音里没有的不要编。'
          '搜索时只搜公开信息，绝不把录音里的内容、人名或私事写进搜索词。'
          '用户说「存下来、记到笔记、存到 Obsidian」之类，就用 save_note 把他要的内容整理成一篇完整的笔记存进去'
          '（是刚才的回答就整理刚才的回答，包括关键数据和来源），存好后简短告诉他存了什么。'
          '听不清或问题不完整就请用户再说一遍。')
LOCKED_NOTE = ('（这次是在锁屏上问的：没有附带用户的录音。问到用户自己说过什么、答应过什么时，'
               '请他解锁后再问，不要猜。）')

_memory = []                           # [(at, question, answer)]
_memory_lock = threading.Lock()


def _post(url, body, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + e5rec.DEEPSEEK_KEY, 'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
        return r.read()


def background(locked=False):
    """The recordings, as text: the last days' AI summaries and the last two hours said.
    Asked from the lock screen: only the time and place -- not unlocked, not told."""
    now = time.time()
    if locked:
        return (f'现在是 {time.strftime("%Y-%m-%d %H:%M 星期", time.localtime(now))}'
                f'{"一二三四五六日"[time.localtime(now).tm_wday]}，用户在{PLACE}。' + LOCKED_NOTE)
    c = e5rec.db()
    e5sum.tables(c)
    out = [f'现在是 {time.strftime("%Y-%m-%d %H:%M 星期", time.localtime(now))}{"一二三四五六日"[time.localtime(now).tm_wday]}，用户在{PLACE}。']
    for d in range(0, -1, -1):          # (today; the other days through the recordings_ tools)
        day = time.strftime('%Y-%m-%d', time.localtime(now - d * 86400))
        row = c.execute('select json from daily where day=?', (day,)).fetchone()
        recs = e5sum.day_rows(c, day)
        lines = []
        for name, start, seconds, _n, sj in recs:
            s = json.loads(sj) if sj else {}
            if s.get('empty') or not s:
                continue
            lines.append(f'- {e5sum.clock(start)} {s.get("title") or ""}：{s.get("summary") or ""}'
                         + (f'（待办：{"；".join(s.get("todos") or [])}）' if s.get('todos') else ''))
        if row or lines:
            ds = json.loads(row[0]) if row else {}
            out.append(f'【{day} 的录音】' + (f'当日总结：{ds.get("summary")}' if ds.get('summary') else ''))
            out.extend(lines)
    said = c.execute('select start, text from segments where start > ? order by start', (int(now - RECENT_S),)).fetchall()
    if said:
        out.append('【最近两小时的原话】（更早的用 recordings_ 工具查）')
        out.extend(f'{time.strftime("%H:%M", time.localtime(t))} {x}' for t, x in said)
    text = '\n'.join(out)
    return text[-MAX_CONTEXT:]


def web_search(query):
    c = json.loads(WEBSEARCH.read_text())
    req = urllib.request.Request(c['url'], data=json.dumps({'q': query}).encode(), method='POST',
                                 headers={**c['headers'], 'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=40) as r:
        res = json.load(r).get('results') or []
    return [{'title': x.get('title'), 'url': x.get('url'), 'snippet': (x.get('snippet') or '')[:400], 'engine': x.get('source')}
            for x in res[:10]]


def save_note(title, content):
    """A Markdown note into the vault, never over an existing one."""
    import base64 as b64
    import urllib.error
    import urllib.parse
    c = json.loads(OBSIDIAN.read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    head = {'Authorization': 'Basic ' + c['auth']}
    folder = f"{c['base']}/{NOTE_DIR}"
    try:
        opener.open(urllib.request.Request(c['url'] + urllib.parse.quote(folder) + '/', method='MKCOL', headers=head), timeout=20)
    except urllib.error.HTTPError as e:
        if e.code not in (405, 301):           # (405: it is there already)
            raise
    safe = re.sub(r'[\\/:*?"<>|#^\[\]\n\r\t]', ' ', str(title)).strip()[:60] or '笔记'
    day = time.strftime('%Y-%m-%d')
    body = (f'---\ntags: [E5助手]\ncreated: {time.strftime("%Y-%m-%d %H:%M")}\nsource: E5 口袋助手\n---\n\n'
            # (the model usually starts with its own heading: then not a second one)
            + ('' if content.lstrip().startswith('#') else f'# {title}\n\n') + f'{content.strip()}\n').encode()
    for n in range(1, 50):
        name = f'{day} {safe}' + (f' {n}' if n > 1 else '') + '.md'
        req = urllib.request.Request(c['url'] + urllib.parse.quote(f'{folder}/{name}'), data=body, method='PUT',
                                     headers={**head, 'If-None-Match': '*', 'Content-Type': 'text/markdown; charset=utf-8'})
        try:
            with opener.open(req, timeout=30):
                pass
            return {'saved': f'{NOTE_DIR}/{name}', 'note': '电脑和手机上的 Obsidian 下次同步（最多 30 分钟）就能看到'}
        except urllib.error.HTTPError as e:
            if e.code != 412:                   # (412: that name is taken: the next one)
                raise
    raise RuntimeError('同名笔记太多了')


def _clock(t):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))


def recordings_days(limit=30):
    c = e5rec.db()
    e5sum.tables(c)
    rows = c.execute('select r.day, count(*), sum(r.seconds), d.json from recordings r left join daily d on d.day = r.day '
                     'group by r.day order by r.day desc limit ?', (int(limit),)).fetchall()
    return [{'date': d, 'recordings': n, 'minutes': round((s or 0) / 60), 'day_title': (json.loads(j) or {}).get('title') if j else None}
            for d, n, s, j in rows]


def recordings_day(date):
    c = e5rec.db()
    e5sum.tables(c)
    d = c.execute('select json from daily where day=?', (date,)).fetchone()
    recs = []
    for name, start, seconds, _n, sj in e5sum.day_rows(c, date):
        s = json.loads(sj) if sj else {}
        if s.get('empty'):
            continue
        recs.append({'name': name, 'from': _clock(start)[11:16], 'to': _clock(start + (seconds or 0))[11:16],
                     'title': s.get('title'), 'summary': s.get('summary'), 'todos': s.get('todos') or []})
    return {'date': date, 'day_summary': json.loads(d[0]) if d else None, 'recordings': recs}


def recordings_search(query, date_from=None, date_to=None):
    words = [w for w in str(query).split() if w][:5]
    if not words:
        return []
    q, p = [], []
    for w in words:
        q.append('text like ?')
        p.append(f'%{w}%')
    if date_from:
        q.append('start >= ?')
        p.append(time.mktime(time.strptime(date_from, '%Y-%m-%d')))
    if date_to:
        q.append('start < ?')
        p.append(time.mktime(time.strptime(date_to, '%Y-%m-%d')) + 86400)
    rows = e5rec.db().execute('select name, start, text from segments where ' + ' and '.join(q)
                              + ' order by start desc limit 40', p).fetchall()
    return [{'time': _clock(t), 'recording': n, 'text': x} for n, t, x in rows]


def recordings_transcript(name=None, date=None, time_from=None, time_to=None):
    c = e5rec.db()
    if name:
        rows = c.execute('select start, text from segments where name=? order by rel', (name,)).fetchall()
    elif date:
        lo = time.mktime(time.strptime(f'{date} {time_from or "00:00"}', '%Y-%m-%d %H:%M'))
        hi = time.mktime(time.strptime(f'{date} {time_to}', '%Y-%m-%d %H:%M')) + 59 if time_to else \
            time.mktime(time.strptime(date, '%Y-%m-%d')) + 86400
        rows = c.execute('select start, text from segments where start >= ? and start < ? order by start', (lo, hi)).fetchall()
    else:
        return {'error': '需要 name，或 date（可加 time_from / time_to）'}
    text = '\n'.join(f'[{_clock(t)[11:]}] {x}' for t, x in rows)
    return {'sentences': len(rows), 'text': text[:15000] + ('\n…（太长截断了，请缩小时间范围）' if len(text) > 15000 else '')}


def _fn(name, desc, props=None, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': desc, 'parameters': {
        'type': 'object', 'properties': props or {}, 'required': list(required)}}}


DATE = {'type': 'string', 'description': 'YYYY-MM-DD'}
TOOLS_WEB = [
    {'type': 'deepseek_web_search'}, {'type': 'deepseek_read_url'},
    _fn('web_search', '用户自己的四引擎搜索（Exa、Bing、SearXNG、Grok 同时搜、去重），比自带搜索全，约 10 多秒。返回标题、链接、摘要、来源引擎。',
        {'query': {'type': 'string'}}, ['query']),
]
TOOLS_NOTE = [
    _fn('save_note', '把内容存成 Obsidian 笔记（Markdown，存在 E5助手 文件夹，不会覆盖已有笔记）。用户要求存、记下来时用。',
        {'title': {'type': 'string', 'description': '笔记标题，简短'}, 'content': {'type': 'string', 'description': '完整的笔记正文（Markdown）'}},
        ['title', 'content']),
]
TOOLS_REC = [
    _fn('recordings_days', '有录音的日期（最近的在前）、每天录了几段多少分钟、当日 AI 总结的标题。', {'limit': {'type': 'integer'}}),
    _fn('recordings_day', '某一天的 AI 当日总结（概括、时间线、待办）和当天每段录音的时间、标题、摘要、待办。', {'date': DATE}, ['date']),
    _fn('recordings_search', '在所有录音的逐句原文里搜关键词（多个词用空格分开，需同时出现），返回时间、录音名、那句话。',
        {'query': {'type': 'string'}, 'date_from': DATE, 'date_to': DATE}, ['query']),
    _fn('recordings_transcript', '读一段录音的逐句原文（name 来自其他工具），或某天某个时间段的全部原文（date + time_from/time_to，HH:MM）。',
        {'name': {'type': 'string'}, 'date': DATE, 'time_from': {'type': 'string'}, 'time_to': {'type': 'string'}}),
]
FUNCS = {'save_note': save_note, 'web_search': web_search, 'recordings_days': recordings_days, 'recordings_day': recordings_day,
         'recordings_search': recordings_search, 'recordings_transcript': recordings_transcript}


def describe(call):
    """What a tool call is doing, for the E5's card."""
    try:
        a = json.loads(call['function'].get('arguments') or '{}')
    except ValueError:
        a = {}
    n = call['function']['name']
    return {'web_search': lambda: f"四引擎搜索：{a.get('query', '')}",
            'save_note': lambda: f"存到 Obsidian：{a.get('title', '')}",
            'recordings_days': lambda: '看有哪些天的录音',
            'recordings_day': lambda: f"看 {a.get('date', '')} 的录音总结",
            'recordings_search': lambda: f"在录音里找：{a.get('query', '')}",
            'recordings_transcript': lambda: '读录音原文' + (f"：{a.get('date')} {a.get('time_from') or ''}-{a.get('time_to') or ''}".rstrip('-')
                                                         if a.get('date') else ''),
            }.get(n, lambda: n)()


def run_tool(call, allowed):
    name = call['function']['name']
    try:
        args = json.loads(call['function'].get('arguments') or '{}')
        if name not in allowed or not isinstance(args, dict):
            raise ValueError(f'没有这个工具：{name}')
        out = FUNCS[name](**args)
    except Exception as e:
        out = {'error': str(e) or type(e).__name__}
    e5rec.log(f'assistant: tool {name}({call["function"].get("arguments")!s:.80}) -> {len(json.dumps(out, ensure_ascii=False))} chars')
    return {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(out, ensure_ascii=False)[:20000]}


def answer(question, locked=False, mode=DEFAULT_MODE, progress=lambda text: None):
    with _memory_lock:
        now = time.time()
        _memory[:] = [m for m in _memory if now - m[0] < MEMORY_S][-MEMORY_TURNS:]
        # (from the lock screen, not the earlier turns either: they may hold what the recordings said)
        turns = [] if locked else list(_memory)
    msgs = [{'role': 'system', 'content': SYSTEM + '\n\n' + background(locked)}]
    for _at, q, a in turns:
        msgs += [{'role': 'user', 'content': q}, {'role': 'assistant', 'content': a}]
    msgs.append({'role': 'user', 'content': question})
    model, effort = MODES.get(mode, MODES[DEFAULT_MODE])
    tools = TOOLS_WEB + TOOLS_NOTE + ([] if locked else TOOLS_REC)
    allowed = {t['function']['name'] for t in tools if t['type'] == 'function'}
    deadline = time.monotonic() + ANSWER_BUDGET_S
    text = ''
    for rnd in range(MAX_ROUNDS + 1):
        last = rnd == MAX_ROUNDS or time.monotonic() > deadline - 30
        body = {'model': model, 'thinking': {'type': 'enabled' if effort else 'disabled'}, 'max_tokens': 8000,
                **({'reasoning_effort': effort} if effort else {}), 'messages': msgs,
                # (the last round: no more tools, an answer from what there is)
                **({} if last else {'tools': tools})}
        progress('整理答案…' if last else '在想…' if rnd == 0 else '看了结果，接着想…')
        out = json.loads(_post(CHAT, body, timeout=max(30, min(180, deadline - time.monotonic()))))
        m = out['choices'][0]['message']
        calls = [c for c in (m.get('tool_calls') or []) if c.get('type') == 'function']
        if not calls or last:
            text = (m.get('content') or '').strip()
            break
        msgs.append({k: m[k] for k in ('role', 'content', 'reasoning_content', 'tool_calls') if k in m})
        progress('；'.join(describe(c) for c in calls))
        with ThreadPoolExecutor(min(4, len(calls))) as pool:
            msgs.extend(pool.map(lambda c: run_tool(c, allowed), calls))
    if not locked:
        with _memory_lock:
            _memory.append((time.time(), question, text))
    return text


def louder(wav, boost_db):
    """The E5's speaker is capped at Android's own gain and DeepSeek speaks at about -19 dB:
    raise it, a limiter keeping the peaks under full scale (measured: +8 dB -> -12.7 dB, no clipping)."""
    if boost_db <= 0:
        return wav
    r = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', 'pipe:0', '-af',
                        f'volume={boost_db}dB,alimiter=limit=0.93:attack=3:release=60:level=disabled',
                        '-ar', '24000', '-ac', '1', '-c:a', 'pcm_s16le', '-f', 'wav', 'pipe:1'],
                       input=wav, capture_output=True, timeout=30)
    return r.stdout if r.returncode == 0 and r.stdout[:4] == b'RIFF' else wav


def _tts(text):
    return _post(TTS, {'text': text, 'voice_id': VOICE, 'format': 'pcm'}, timeout=60)


def to_mp3(wav):
    """48 kbps mono MP3: an eighth of the WAV (a long answer over mobile data); the E5 plays it with paplay."""
    r = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', 'pipe:0', '-c:a', 'libmp3lame', '-b:a', '48k',
                        '-ac', '1', '-f', 'mp3', 'pipe:1'], input=wav, capture_output=True, timeout=120)
    return r.stdout if r.returncode == 0 and r.stdout else None


def wav_seconds(wav):
    try:
        with wave.open(io.BytesIO(wav)) as w:
            return (len(wav) - 44) / (w.getframerate() * w.getsampwidth() * w.getnchannels())
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def speak(text, boost_db=0):
    """The answer spoken: sentence by sentence, all at once (DeepSeek takes 50 together), joined --
    a long answer takes about its longest sentence, not their sum (measured: 6.1 s in one piece)."""
    parts = [p.strip() for p in re.split(r'(?<=[。！？!?；;])', text[:2000]) if p.strip()]
    # (very short pieces ride with the next: a pause between each costs nothing, a request each does)
    merged = []
    for p in parts:
        if merged and len(merged[-1]) < 12:
            merged[-1] += p
        else:
            merged.append(p)
    with ThreadPoolExecutor(min(16, max(1, len(merged)))) as pool:
        pcm = b''.join(pool.map(_tts, merged or [text]))
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:           # (DeepSeek's pcm: 24 kHz, 16-bit, mono)
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return louder(b.getvalue(), boost_db)


def ask(wav, locked=False, boost_db=8, mode=DEFAULT_MODE, progress=lambda text: None):
    t0 = time.monotonic()
    progress('听…')
    question = e5rec.asr_wav(wav)
    t1 = time.monotonic()
    if question:
        progress(f'听到：{question}')
    if not question:
        reply = '没听清，请再说一遍。'
    else:
        try:
            reply = answer(question, locked, mode, progress) or '我没想好怎么回答，换个问法试试。'
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            e5rec.log(f'assistant: answer failed: {e}')
            reply = '网络不太好，我现在回答不了。'
    t2 = time.monotonic()
    progress('合成语音…')
    try:
        audio = speak(reply, boost_db)
    except (urllib.error.URLError, TimeoutError) as e:
        e5rec.log(f'assistant: speech failed: {e}')
        audio = b''
    t3 = time.monotonic()
    e5rec.log(f'assistant: heard {t1 - t0:.1f}s, answered {t2 - t1:.1f}s, spoke {t3 - t2:.1f}s: {question!r}')
    return {'question': question, 'answer': reply, 'wav': audio,
            'timing': {'asr': round(t1 - t0, 1), 'answer': round(t2 - t1, 1), 'tts': round(t3 - t2, 1)}}
