"""e5assist -- the pocket voice assistant's brain, on the NAS.

ask(wav) -> {question, answer, wav}: what was said (DeepSeek ASR), an answer
that will be read aloud (deepseek-flash, web search on, with the recordings'
summaries and the last two hours of what was said as background), and that
answer spoken (DeepSeek TTS, 24 kHz mono WAV).  A short memory of the last
turns lets a follow-up ("那明天呢？") make sense.
"""
import json
import threading
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
RECENT_S = 2 * 3600
MAX_CONTEXT = 24000

SYSTEM = ('你是用户随身带的口袋语音助手（装在一台叫 E5 的小设备上）。你的回答会被直接朗读出来：'
          '用口语，简短，一般一两句、最多三句、不超过120个字；不要 Markdown、列表、编号、链接、表情、括号注释。'
          '需要实时信息（天气、新闻、价格等）就联网搜索。'
          '下面附有用户这台设备录下的对话的整理（AI 总结和最近的原话），只有问题和它们有关时才用；'
          '引用时说清是什么时候说的。录音里没有的不要编。联网搜索时只搜公开信息，绝不把录音里的内容、人名或私事写进搜索词。'
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
    for d in range(2, -1, -1):
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
        out.append('【最近两小时的原话】')
        out.extend(f'{time.strftime("%H:%M", time.localtime(t))} {x}' for t, x in said)
    text = '\n'.join(out)
    return text[-MAX_CONTEXT:]


def answer(question, locked=False):
    with _memory_lock:
        now = time.time()
        _memory[:] = [m for m in _memory if now - m[0] < MEMORY_S][-MEMORY_TURNS:]
        # (from the lock screen, not the earlier turns either: they may hold what the recordings said)
        turns = [] if locked else list(_memory)
    msgs = [{'role': 'system', 'content': SYSTEM + '\n\n' + background(locked)}]
    for _at, q, a in turns:
        msgs += [{'role': 'user', 'content': q}, {'role': 'assistant', 'content': a}]
    msgs.append({'role': 'user', 'content': question})
    body = {'model': 'deepseek-flash', 'thinking': {'type': 'disabled'}, 'max_tokens': 500,
            'tools': [{'type': 'deepseek_web_search'}], 'messages': msgs}
    out = json.loads(_post(CHAT, body))
    text = (out['choices'][0]['message'].get('content') or '').strip()
    if not locked:
        with _memory_lock:
            _memory.append((time.time(), question, text))
    return text


def speak(text):
    return _post(TTS, {'text': text[:2000], 'voice_id': VOICE, 'format': 'wav'}, timeout=60)


def ask(wav, locked=False):
    t0 = time.monotonic()
    question = e5rec.asr_wav(wav)
    t1 = time.monotonic()
    if not question:
        reply = '没听清，请再说一遍。'
    else:
        try:
            reply = answer(question, locked) or '我没想好怎么回答，换个问法试试。'
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            e5rec.log(f'assistant: answer failed: {e}')
            reply = '网络不太好，我现在回答不了。'
    t2 = time.monotonic()
    try:
        audio = speak(reply)
    except (urllib.error.URLError, TimeoutError) as e:
        e5rec.log(f'assistant: speech failed: {e}')
        audio = b''
    t3 = time.monotonic()
    e5rec.log(f'assistant: heard {t1 - t0:.1f}s, answered {t2 - t1:.1f}s, spoke {t3 - t2:.1f}s: {question!r}')
    return {'question': question, 'answer': reply, 'wav': audio,
            'timing': {'asr': round(t1 - t0, 1), 'answer': round(t2 - t1, 1), 'tts': round(t3 - t2, 1)}}
