'use strict';
// 配额 (aiquota): quota-tray's dashboard on the E5.  The data is quota-tray's own payload, made
// on the NAS by quota-export.py (quota-tray.py's fetch, imported as it is) and served by
// e5rec_server.py GET /v1/quota -- through the 录音 app's NAS settings (upload_url,
// upload_token, the pinned /etc/e5rec/nas.pem).  The last answer is served at once and
// refreshed in the background, at most once a minute; only one with accounts in it replaces it.
// On the lock screen: early.html, put in the screen's index.html before power-menu.js (like
// the sidekey app's: mobile.js takes every other key on the lock screen).
//   GET /quota[?fresh=1]  {ok, age, accounts, recommend, sites, updated_at, error} (fresh: fetch now, not after TTL)
//   POST /refresh         the NAS to fetch everything again now -> {before: the generated_at_ms shown}
//   GET /notifications    (the minute check: early.html put in, if not)
import { readfile, writefile, rename, stat, unlink, mkdir } from 'fs';

const APP = '/etc/e5-infoscreen/plugins/aiquota';
const RUN = '/tmp/run/e5-aiquota', CACHE = RUN + '/quota.json', HDR = RUN + '/nas-auth.hdr';
const NAS_CERT = '/etc/e5rec/nas.pem';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const ANCHOR = '<script src="power-menu.js"></script>\n';
const BEGIN = '<script>/* BEGIN e5-aiquota', END = '/* END e5-aiquota */</script>\n';
const TTL = 60;

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	function nas() {
		let c = ctx.uci();
		let url = c.get('e5-plugin-recorder', 'settings', 'upload_url'), token = c.get('e5-plugin-recorder', 'settings', 'upload_token');
		if (!url || !token || !stat(NAS_CERT)) return null;
		// (it goes into a shell command: only https://host[:port])
		if (!match(url, /^https:\/\/[A-Za-z0-9.-]+(:[0-9]+)?$/)) return null;
		return { url, token };
	}
	function refresh(n) {
		mkdir(RUN);
		ctx.run(`chmod 700 '${RUN}'`);
		let f = stat(RUN + '/fetching');
		if (f && time() - (f.mtime ?? 0) < 45) return;
		writefile(RUN + '/fetching', '');
		// (the token in a header file only root reads, not on a command line)
		let hdr = 'Authorization: Bearer ' + n.token + '\n';
		if (readfile(HDR) != hdr) {
			writefile(HDR + '.new', hdr);
			rename(HDR + '.new', HDR);
		}
		ctx.run(`setsid sh -c "curl -sf -m 40 --noproxy '*' --cacert '${NAS_CERT}' -H @'${HDR}' -o '${CACHE}.new' '${n.url}/v1/quota' && jsonfilter -i '${CACHE}.new' -e '@.accounts[0]' >/dev/null && mv -f '${CACHE}.new' '${CACHE}'; rm -f '${CACHE}.new' '${RUN}/fetching'" </dev/null >/dev/null 2>&1 &`);
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
			let n = nas();
			if (!n) return fail('还没设置 NAS（在「录音」应用里）', 409);
			let st = stat(CACHE);
			if (!st || time() - st.mtime >= (req.query?.fresh ? 3 : TTL)) refresh(n);
			let d = null;
			try { d = json(readfile(CACHE) ?? 'null'); } catch (e) {}
			if (type(d) != 'object') return { ok: true, accounts: null, loading: true };
			// (how old the data is: from when the NAS made it, not when it came here)
			let made = type(d.generated_at_ms) == 'int' ? d.generated_at_ms / 1000 : st.mtime;
			return { ok: true, ...d, age: time() - made };
		},
		'POST /refresh': function(req) {
			let n = nas();
			if (!n) return fail('还没设置 NAS（在「录音」应用里）', 409);
			mkdir(RUN);
			let hdr = 'Authorization: Bearer ' + n.token + '\n';
			if (readfile(HDR) != hdr) { writefile(HDR + '.new', hdr); rename(HDR + '.new', HDR); }
			ctx.run(`setsid curl -sf -m 20 --noproxy '*' --cacert '${NAS_CERT}' -H @'${HDR}' -X POST '${n.url}/v1/quota/refresh' </dev/null >/dev/null 2>&1 &`);
			let d = null;
			try { d = json(readfile(CACHE) ?? 'null'); } catch (e) {}
			return { ok: true, before: type(d) == 'object' ? d.generated_at_ms ?? 0 : 0 };
		},
		'GET /notifications': function(req) {
			checks();
			return { notifications: [] };
		}
	};
};
