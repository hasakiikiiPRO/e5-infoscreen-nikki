'use strict';
// 助手 (assistant): hold OK to ask.  The screen half: early.html in the screen's
// index.html (before power-menu.js, like the sidekey app's: those take every key
// while the screen is locked) holds the key and shows what happens; listen.py
// does one question (starting -> listening -> thinking -> speaking -> done).
//   GET  /state     what the current run last wrote ({run, phase, question, answer, error})
//   POST /start     {locked}: a new run (the last one ended first) -> {run}
//   POST /stop      the key let go: stop listening (the current run)
//   POST /cancel    drop the current run (and its voice)
//   POST /skip      enough of the answer read: stop the voice (and, in a conversation, listen again)
//   GET  /history   the last questions and answers
//   GET  /notifications  (the minute check: early.html put in, if not)
import { readfile, writefile, rename, stat, unlink, mkdir } from 'fs';

const APP = '/etc/e5-infoscreen/plugins/assistant';
const RUN = '/tmp/run/e5-assistant';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const ANCHOR = '<script src="power-menu.js"></script>\n';
const BEGIN = '<script>/* BEGIN e5-assistant', END = '/* END e5-assistant */</script>\n';
const HISTORY = '/mnt/e5-data/e5assist/history.json';
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
	function checks() {
		if (!ctx.state_get('checked', 60)) {
			ctx.state_put('checked', { at: time() });
			ensure_early();
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
				continuous: ctx.uci().get('e5-plugin-assistant', 'settings', 'continuous') != '0' };
		},
		'POST /start': function(req) {
			checks();
			if (!enabled()) return fail('助手已关闭', 409);
			let pid = running();
			if (pid) {
				mark('cancel');
				ctx.run(`kill ${pid} 2>/dev/null; kill $(pgrep -f 'e5-assistant/answer[.]') 2>/dev/null`);
			}
			let c = clock() ?? [ time(), 0 ];
			let run = sprintf('%d-%09d', c[0], c[1]);
			mkdir(RUN);
			ctx.run(`chmod 700 '${RUN}'; rm -f '${RUN}/stop' '${RUN}/cancel'`);
			writefile(`${RUN}/run`, run);
			writefile(`${RUN}/state.json`, sprintf('%J', { phase: 'starting', run, at: time() }));
			// (asked from the lock screen: the recordings stay out only if lock_private is on -- off by default:
			// a voice conversation should not need the screen unlocked)
			let locked = req.body?.locked && ctx.uci().get('e5-plugin-assistant', 'settings', 'lock_private') == '1' ? 'locked' : 'open';
			// (the pid written as it is started: a second press within python's start-up sees it)
			ctx.run(`cd / && (setsid /usr/bin/python3 '${APP}/listen.py' '${run}' '${locked}' </dev/null >/dev/null 2>&1 & echo $! > '${RUN}/pid')`);
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
