'use strict';
// 助手 (assistant): hold OK to ask.  The screen half: early.html in the screen's
// index.html (before power-menu.js, like the sidekey app's: those take every key
// while the screen is locked) holds the key and shows what happens; listen.py
// does one question (starting -> listening -> thinking -> speaking -> done).
//   GET  /state     what the current run last wrote ({run, phase, question, answer, error})
//   POST /start     {locked} or {wake}: a new run (the last one ended first) -> {run}; wake: called by
//                   name (wake.py, which hears the first question itself and hands it over)
//   POST /stop      the key let go: stop listening (the current run)
//   POST /cancel    drop the current run (and its voice)
//   POST /skip      enough of the answer read: stop the voice (and, in a conversation, listen again)
//   GET  /history   the last questions and answers
//   GET  /enroll    recording your voice saying a wake word (enroll.py): its state, the samples kept
//   POST /enroll    {word: fish|jarvis}: start (not while a question runs)
//   POST /enroll/stop
//   GET  /notifications  (the minute check: early.html put in, the wake service installed, if not)
import { readfile, writefile, rename, stat, unlink, mkdir, lsdir } from 'fs';

const APP = '/etc/e5-infoscreen/plugins/assistant';
const RUN = '/tmp/run/e5-assistant';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const ANCHOR = '<script src="power-menu.js"></script>\n';
const BEGIN = '<script>/* BEGIN e5-assistant', END = '/* END e5-assistant */</script>\n';
const HISTORY = '/mnt/e5-data/e5assist/history.json';
const SAMPLES = '/mnt/e5-data/e5assist/enroll';
const INIT = '/etc/init.d/e5-assistant-wake', WAKE_STATE = '/tmp/run/e5-wake/state.json';
const STARTING_S = 10;                  // (python's start and the microphone: "starting" is not "gone" before this)

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	function load(path, def) {
		try { return json(readfile(path) ?? 'null') ?? def; } catch (e) { return def; }
	}
	function running() {
		// (the pid, and that it is still listen.py: a reused pid is someone else)
		let pid = trim(readfile(`${RUN}/pid`) ?? '');
		if (!match(pid, /^[0-9]+$/)) return null;
		let cmd = readfile(`/proc/${pid}/cmdline`);
		return cmd != null && index(cmd, 'listen.py') >= 0 ? pid : null;
	}
	function enrolling() {
		let pid = trim(readfile(`${RUN}/enroll.pid`) ?? '');
		if (!match(pid, /^[0-9]+$/)) return null;
		let cmd = readfile(`/proc/${pid}/cmdline`);
		return cmd != null && index(cmd, 'enroll.py') >= 0 ? pid : null;
	}
	function samples() {
		let n = {};
		for (let w in [ 'fish', 'jarvis' ])
			n[w] = length(filter(lsdir(`${SAMPLES}/${w}`) ?? [], (f) => match(f, /\.wav$/)));
		return n;
	}
	function current() {
		return trim(readfile(`${RUN}/run`) ?? '');
	}
	function mark(f) {
		mkdir(RUN);
		writefile(`${RUN}/${f}`, current());
	}
	function cut(src) {
		let a = index(src, BEGIN);
		if (a < 0) return src;
		let rest = substr(src, a), b = index(rest, END);
		return b < 0 ? null : substr(src, 0, a) + substr(rest, b + length(END));
	}
	function ensure_early() {
		let src = readfile(INDEX), want = readfile(`${APP}/early.html`);
		if (src == null || want == null || index(src, want) >= 0) return;
		src = cut(src);
		if (src == null || length(split(src, ANCHOR)) != 2) return;
		let parts = split(src, ANCHOR), tmp = INDEX + '.assistant-new';
		if (writefile(tmp, parts[0] + want + ANCHOR + parts[1]) == null || !rename(tmp, INDEX)) {
			unlink(tmp);
			return;
		}
		ctx.log('index.html patched (hold OK to ask), the screen restarts');
		ctx.run("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
	}
	function service_running() {
		let s = ctx.ubus('service', 'list', { name: 'e5-assistant-wake' })?.['e5-assistant-wake'];
		for (let k, inst in (s?.instances ?? {})) if (inst.running) return true;
		return false;
	}
	function ensure_service() {
		let want = readfile(`${APP}/wake.init`);
		if (!want) return;
		if (readfile(INIT) != want) {
			ctx.run(`cp '${APP}/wake.init' '${INIT}.new' && chmod 755 '${INIT}.new' && ` +
				`mv -f '${INIT}.new' '${INIT}' && '${INIT}' enable && '${INIT}' restart >/dev/null 2>&1`);
			ctx.log('wake service installed');
		} else if (!service_running()) {
			ctx.run(`'${INIT}' enable; '${INIT}' start >/dev/null 2>&1`);
		} else if (readfile(`${APP}/wake.py`) != readfile('/var/run/e5-assistant-wake.running')) {
			ctx.run(`setsid '${INIT}' restart >/dev/null 2>&1 &`);
			ctx.log('wake service restarted for the new version');
		}
	}
	function checks() {
		if (!ctx.state_get('checked', 60)) {
			ctx.state_put('checked', { at: time() });
			ensure_early();
			ensure_service();
		}
	}
	function hold_ms() {
		let v = int(ctx.uci().get('e5-plugin-assistant', 'settings', 'hold_ms') ?? '600');
		return v >= 300 && v <= 2000 ? v : 600;
	}
	const enabled = () => ctx.uci().get('e5-plugin-assistant', 'settings', 'enabled') != '0';

	return {
		'GET /state': function(req) {
			let s = load(`${RUN}/state.json`, { phase: 'idle' });
			let busy = running() != null;
			let fresh = time() - (s.at ?? 0) < STARTING_S;
			if (!busy && !fresh && s.phase != 'done' && s.phase != 'error' && s.phase != 'idle')
				s = { ...s, phase: 'error', error: '助手进程意外退出了' };
			return { ok: true, ...s, busy, current: current(), hold_ms: hold_ms(), enabled: enabled(),
				continuous: ctx.uci().get('e5-plugin-assistant', 'settings', 'continuous') != '0',
				wake_service: load(WAKE_STATE, null) };
		},
		'POST /start': function(req) {
			checks();
			if (!enabled()) return fail('助手已关闭', 409);
			if (enrolling()) return fail('正在录唤醒词样本', 409);
			let pid = running();
			if (pid) {
				mark('cancel');
				ctx.run(`kill ${pid} 2>/dev/null; kill $(pgrep -f 'e5-assistant/answer[.]') 2>/dev/null`);
			}
			// (called by name the screen may be locked or not: with lock_private on, taken as locked)
			let woken = req.body?.wake == true;
			let c = clock() ?? [ time(), 0 ];
			let run = sprintf('%d-%09d', c[0], c[1]);
			mkdir(RUN);
			ctx.run(`chmod 700 '${RUN}'; rm -f '${RUN}/stop' '${RUN}/cancel'`);
			writefile(`${RUN}/run`, run);
			writefile(`${RUN}/state.json`, sprintf('%J', { phase: 'starting', run, at: time(), wake: woken }));
			// (asked from the lock screen: the recordings stay out only if lock_private is on -- off by default:
			// a voice conversation should not need the screen unlocked)
			let locked = (req.body?.locked || woken) && ctx.uci().get('e5-plugin-assistant', 'settings', 'lock_private') == '1' ? 'locked' : 'open';
			// (the pid written as it is started: a second press within python's start-up sees it)
			ctx.run(`cd / && (setsid /usr/bin/python3 '${APP}/listen.py' '${run}' '${locked}' '${woken ? 'wake' : 'key'}' </dev/null >/dev/null 2>&1 & echo $! > '${RUN}/pid')`);
			return { ok: true, run };
		},
		'POST /stop': function(req) {
			mark('stop');
			return { ok: true };
		},
		'POST /skip': function(req) {
			mark('skip');
			ctx.run(`kill $(pgrep -f 'e5-assistant/answer[.]') 2>/dev/null`);
			return { ok: true };
		},
		'GET /enroll': function(req) {
			let s = load(`${RUN}/enroll.json`, { phase: 'idle' });
			let busy = enrolling() != null;
			if (!busy && s.phase != 'done' && s.phase != 'error' && s.phase != 'idle' && time() - (s.at ?? 0) > 10)
				s = { ...s, phase: 'error', error: '录音进程意外退出了' };
			return { ok: true, ...s, busy, samples: samples() };
		},
		'POST /enroll': function(req) {
			let w = req.body?.word;
			if (w != 'fish' && w != 'jarvis') return fail('不认识的唤醒词', 400);
			if (enrolling()) return fail('已经在录了', 409);
			if (running()) return fail('助手正在用麦克风，等它说完', 409);
			mkdir(RUN);
			ctx.run(`chmod 700 '${RUN}'; rm -f '${RUN}/enroll.stop'`);
			writefile(`${RUN}/enroll.json`, sprintf('%J', { phase: 'starting', word: w, at: time() }));
			ctx.run(`cd / && (setsid /usr/bin/python3 '${APP}/enroll.py' '${w}' 10 </dev/null >/dev/null 2>&1 & echo $! > '${RUN}/enroll.pid')`);
			return { ok: true };
		},
		'POST /enroll/stop': function(req) {
			mkdir(RUN);
			writefile(`${RUN}/enroll.stop`, '');
			return { ok: true };
		},
		'POST /cancel': function(req) {
			mark('cancel');
			ctx.run(`kill $(pgrep -f 'e5-assistant/answer[.]') 2>/dev/null`);
			return { ok: true };
		},
		'GET /history': function(req) {
			let h = load(HISTORY, []);
			return { ok: true, history: type(h) == 'array' ? reverse(slice(h, -30)) : [] };
		},
		'GET /notifications': function(req) {
			checks();
			return { notifications: [] };
		}
	};
};
