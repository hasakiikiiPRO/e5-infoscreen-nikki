"""e5sum -- AI summaries of the E5 recordings (DeepSeek chat, deepseek-flash).

  per recording: {title, summary, points[], todos[]}  -> table summaries, <name>.summary.json
  per day:       {title, summary, timeline[], todos[]} -> table daily

Only from what was said (the transcript); a recording with almost no speech
gets {"empty": true} and no call.  The key: e5rec.DEEPSEEK_KEY.
"""
import hashlib
import json
import time
import urllib.error
import urllib.request

import e5rec

CHAT = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-flash'
MIN_CHARS = 15                 # (less is a cough, a key press: nothing to sum up)
MAX_CHARS = 60000
DAY_EVERY = 15 * 60            # today's summary at most this often (a segment lands every 5 min)

ONE = ('你是录音整理助手。下面是一段随身录音的转写（可能有识别错字、多人对话，没有标注说话人）。'
       '只依据原文，不要编造，不确定就不写。输出 JSON：{"title": 不超过16个字的标题, '
       '"summary": 1到3句话概括说了什么, "points": [要点，最多5条，每条一句], '
       '"todos": [明确提到的待办、约定、承诺，每条一个字符串，如「我：周四晚上前把数据发群里」（谁、何时原文有才写），没有就空数组]}')
DAY = ('你是录音整理助手。下面是某人一天里各段随身录音的摘要（按时间）。只依据这些内容，不要编造。'
       '输出 JSON：{"title": 不超过16个字概括这一天, "summary": 3到5句话的当日总结, '
       '"timeline": [{"time": "HH:MM", "text": "这个时间前后在做/在聊什么"}]（合并相近的，最多10条）, '
       '"todos": [当天出现的待办、约定，去重，每条一个字符串]}')


def tables(c):
    c.executescript('''
        create table if not exists summaries (name text primary key, at integer, json text);
        create table if not exists daily (day text primary key, at integer, sig text, json text);
    ''')


def chat(system, user, tries=3):
    body = json.dumps({'model': MODEL, 'messages': [{'role': 'system', 'content': system},
                                                    {'role': 'user', 'content': user}],
                       'response_format': {'type': 'json_object'}, 'reasoning_effort': 'low',
                       'max_tokens': 2000}).encode()
    req = urllib.request.Request(CHAT, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + e5rec.DEEPSEEK_KEY, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for i in range(tries):
        try:
            with opener.open(req, timeout=120) as r:
                out = json.loads(r.read())
            got = json.loads(out['choices'][0]['message']['content'])
            if not isinstance(got, dict):
                # (json_object only promises JSON; the transcript is in the prompt and could steer it)
                raise ValueError(f'model returned {type(got).__name__}, not an object')
            return got
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            if i == tries - 1:
                raise
        time.sleep(3 * (i + 1))


def lines(v):
    """A list of strings, whatever the model returned (it sometimes gives {who, task, when})."""
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict):
            who, what, when = (str(x.get(k) or '').strip() for k in ('who', 'task', 'when'))
            what = what or '；'.join(str(y) for y in x.values() if y)
            x = (f'{who}：' if who and who not in ('未明确', '未知') else '') + what + (f'（{when}）' if when else '')
        x = str(x).strip()
        if x:
            out.append(x)
    return out


def clock(t):
    return time.strftime('%H:%M', time.localtime(t))


def summarize_pending(c):
    """Every transcribed recording without a summary yet -> one. Returns how many."""
    tables(c)
    todo = c.execute('''select r.name, r.start, r.path from recordings r left join summaries s on s.name = r.name
                        where r.transcribed_at is not null and s.name is null order by r.start''').fetchall()
    done = 0
    tries = e5rec.load_state().get('summary_failed', {})
    for name, start, path in todo:
        if tries.get(name, 0) >= 3:
            continue
        segs = c.execute('select start, text from segments where name=? order by rel', (name,)).fetchall()
        text = '\n'.join(f'[{time.strftime("%H:%M:%S", time.localtime(t))}] {x}' for t, x in segs)[:MAX_CHARS]
        if sum(len(x) for _, x in segs) < MIN_CHARS or not e5rec.DEEPSEEK_KEY:
            s = {'empty': True}
        else:
            try:
                s = chat(ONE, text)
                s = {'title': str(s.get('title') or ''), 'summary': str(s.get('summary') or ''),
                     'points': lines(s.get('points')), 'todos': lines(s.get('todos'))}
            except Exception as e:
                e5rec.log(f'summary: {name}: {e}')
                tries[name] = tries.get(name, 0) + 1
                e5rec.update_state(lambda st: st.setdefault('summary_failed', {}).__setitem__(name, tries[name]))
                continue
        with c:
            c.execute('insert or replace into summaries values (?,?,?)', (name, int(time.time()), json.dumps(s, ensure_ascii=False)))
        try:
            mp3 = e5rec.day_dir(name) / f'{name}.mp3'
            mp3.with_suffix('.summary.json').write_text(json.dumps(s, ensure_ascii=False, indent=1))
        except OSError:
            pass
        done += 1
    return done


def day_rows(c, day):
    return c.execute('''select r.name, r.start, r.seconds, r.segments, s.json from recordings r
                        left join summaries s on s.name = r.name where r.day = ? order by r.start''', (day,)).fetchall()


def daily_pending(c, days=3):
    """Today's and the last days' summaries, when their recordings' summaries changed."""
    tables(c)
    now = time.time()
    today = time.strftime('%Y-%m-%d')
    for d in range(days):
        day = time.strftime('%Y-%m-%d', time.localtime(now - d * 86400))
        rows = [r for r in day_rows(c, day) if r[4] and not json.loads(r[4]).get('empty')]
        sig = hashlib.sha1(json.dumps([(r[0], r[4]) for r in rows], ensure_ascii=False).encode()).hexdigest()
        old = c.execute('select at, sig from daily where day=?', (day,)).fetchone()
        if not rows or (old and old[1] == sig) or (old and day == today and now - old[0] < DAY_EVERY):
            continue
        parts = []
        for name, start, seconds, _n, sj in rows:
            s = json.loads(sj)
            parts.append(f'{clock(start)}–{clock(start + (seconds or 0))} {s.get("title") or ""}：{s.get("summary") or ""}'
                         + (f'（待办：{"；".join(s["todos"])}）' if s.get('todos') else ''))
        try:
            out = chat(DAY, f'日期 {day}\n' + '\n'.join(parts)[:MAX_CHARS])
            tl = out.get('timeline') if isinstance(out.get('timeline'), list) else []
            tl = [{'time': str(x.get('time') or ''), 'text': str(x.get('text') or '')} for x in tl if isinstance(x, dict)]
            out = {'title': str(out.get('title') or ''), 'summary': str(out.get('summary') or ''),
                   'timeline': tl, 'todos': lines(out.get('todos'))}
        except Exception as e:
            e5rec.log(f'daily summary: {day}: {e}')
            continue
        with c:
            c.execute('insert or replace into daily values (?,?,?,?)', (day, int(now), sig, json.dumps(out, ensure_ascii=False)))
        e5rec.log(f'daily summary: {day} ({len(rows)} recordings)')
