'use strict';
// 配额 (aiquota): the AI accounts' quotas from the NAS panel (opencode-quota-webui,
// GET /api/quota: probing takes seconds, so the last answer is served at once and
// refreshed in the background, at most once a minute), and on the lock screen:
// early.html, put in the screen's index.html before power-menu.js (like the
// sidekey app's: mobile.js takes every other key on the lock screen) -- right
// shows the quotas over the clock, left goes back.
//   GET /quota            {ok, at, accounts: the panel's, as they are}
//   GET /notifications    (the minute check: early.html put in, if not)
import { readfile, writefile, rename, stat, unlink, mkdir } from 'fs';

const APP = '/etc/e5-infoscreen/plugins/aiquota';
const RUN = '/tmp/run/e5-aiquota', CACHE = RUN + '/quota.json';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const ANCHOR = '<script src="power-menu.js"></script>\n';
const BEGIN = '<script>/* BEGIN e5-aiquota', END = '/* END e5-aiquota */</script>\n';
const URL = 'http://47.105.105.85:18418/api/quota';
const TTL = 60;

return function(ctx) {
	function refresh() {
		mkdir(RUN);
		if (stat(RUN + '/fetching') && time() - (stat(RUN + '/fetching').mtime ?? 0) < 40) return;
		writefile(RUN + '/fetching', '');
		// (only a real answer replaces the last good one: not an error page, not {"error": ...})
		ctx.run(`setsid sh -c "curl -sf -m 30 --noproxy '*' -o '${CACHE}.new' '${URL}' && jsonfilter -i '${CACHE}.new' -e '@.accounts[0]' >/dev/null && mv -f '${CACHE}.new' '${CACHE}'; rm -f '${CACHE}.new' '${RUN}/fetching'" </dev/null >/dev/null 2>&1 &`);
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
		let parts = split(src, ANCHOR), tmp = INDEX + '.aiquota-new';
		if (writefile(tmp, parts[0] + want + ANCHOR + parts[1]) == null || !rename(tmp, INDEX)) {
			unlink(tmp);
			return;
		}
		ctx.log('index.html patched (the lock screen\'s AI quota page), the screen restarts');
		ctx.run("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
	}
	function checks() {
		if (!ctx.state_get('checked', 60)) {
			ctx.state_put('checked', { at: time() });
			ensure_early();
		}
	}

	return {
		'GET /quota': function(req) {
			checks();
			let st = stat(CACHE), age = st ? time() - st.mtime : null;
			if (age == null || age >= TTL) refresh();
			let d = null;
			try { d = json(readfile(CACHE) ?? 'null'); } catch (e) {}
			if (!d) return { ok: true, accounts: null, loading: true };
			return { ok: true, at: st.mtime, age, accounts: d.accounts ?? [] };
		},
		'GET /notifications': function(req) {
			checks();
			return { notifications: [] };
		}
	};
};
