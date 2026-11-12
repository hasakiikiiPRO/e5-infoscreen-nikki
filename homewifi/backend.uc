'use strict';
// 在家 (homewifi): the screen half.  service.uc (under /etc/init.d/e5-homewifi)
// switches between the home WiFi and the hotspot; this installs that service
// and keeps it installed, puts the lock screen's 在家 / 在外 mark into the
// screen's page (the notification poll checks once a minute), and serves
//   GET  /status   what the service last saw
//   POST /check    look for the home WiFi now (the hotspot off for a moment)
//   POST /mode     {"mode": "auto" | "home" | "away"}
import { readfile, writefile, rename, stat, unlink, mkdir } from 'fs';

const DIR = '/etc/e5-homewifi', RUN = '/tmp/run/e5-homewifi';
const APP = '/etc/e5-infoscreen/plugins/homewifi';
const INIT = '/etc/init.d/e5-homewifi';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const INDEX_ANCHOR = '<script src="app.js"></script>\n';
const HTML_BEGIN = '<!-- BEGIN e5-homewifi', HTML_END = '<!-- END e5-homewifi -->\n';

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	function load(path, def) {
		try { return json(readfile(path) ?? 'null') ?? def; } catch (e) { return def; }
	}
	function service_running() {
		let s = ctx.ubus('service', 'list', { name: 'e5-homewifi' })?.['e5-homewifi'];
		for (let k, inst in (s?.instances ?? {})) if (inst.running) return true;
		return false;
	}
	function ensure_service() {
		let want = readfile(`${APP}/homewifi.init`);
		if (!want) return;
		if (readfile(INIT) != want) {
			ctx.run(`cp '${APP}/homewifi.init' '${INIT}.new' && chmod 755 '${INIT}.new' && ` +
				`mv -f '${INIT}.new' '${INIT}' && '${INIT}' enable && '${INIT}' restart >/dev/null 2>&1`);
			ctx.log('service installed');
		} else if (!service_running()) {
			ctx.run(`'${INIT}' enable; '${INIT}' start >/dev/null 2>&1`);
		} else if (readfile(`${APP}/service.uc`) != readfile('/var/run/e5-homewifi.running') &&
		           !stat(`${RUN}/looking`)) {
			// (not in the middle of a look: the hotspot is down then)
			ctx.run(`setsid '${INIT}' restart >/dev/null 2>&1 &`);
			ctx.log('service restarted for the new version');
		}
	}
	// src without the block from begin to end (end included); null if it is broken
	function cut(src, begin, end) {
		let a = index(src, begin);
		if (a < 0) return src;
		let rest = substr(src, a), b = index(rest, end);
		return b < 0 ? null : substr(src, 0, a) + substr(rest, b + length(end));
	}
	function ensure_lock_mark() {
		let src = readfile(INDEX), want = readfile(`${APP}/lock-mark.html`);
		if (src == null || want == null || index(src, want) >= 0) return;
		src = cut(src, HTML_BEGIN, HTML_END);
		if (src == null) { ctx.log('index.html: a broken lock mark block, left alone'); return; }
		let parts = split(src, INDEX_ANCHOR);
		if (length(parts) != 2) { ctx.log('index.html: no single app.js script, no lock mark'); return; }
		let tmp = INDEX + '.homewifi-new';
		if (writefile(tmp, parts[0] + INDEX_ANCHOR + want + parts[1]) == null || !rename(tmp, INDEX)) {
			unlink(tmp);
			return;
		}
		ctx.log('index.html patched (the lock screen\'s home mark), the screen restarts');
		ctx.run("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
	}
	function checks() {
		if (!ctx.state_get('checked', 60)) {
			ctx.state_put('checked', { at: time() });
			ensure_service();
			ensure_lock_mark();
		}
	}
	function stamp(sec) {
		if (!sec) return null;
		let t = localtime(sec);
		return sprintf('%02d-%02d %02d:%02d', t.mon, t.mday, t.hour, t.min);
	}
	function state() {
		let s = load(`${RUN}/state.json`, {}), c = ctx.uci();
		let cells = load(`${DIR}/cells.json`, { cells: {} }).cells;
		return { ok: true, mode: s.mode ?? 'unknown', state: s.state, ssid: c.get('wireless', 'homewifi_sta', 'ssid'),
			setting: c.get('e5-plugin-homewifi', 'settings', 'mode') ?? 'auto',
			signal: s.signal, lost: s.lost ?? 0, near: s.near, reason: s.reason,
			since: stamp(s.since), last_look: stamp(s.last_look), last_signal: s.last_signal,
			held_until: stamp(s.held_until), cells_known: length(keys(cells ?? {})),
			checking: !!stat(`${RUN}/check-now`), service: service_running(), fresh: time() - (s.at ?? 0) < 60,
			sidegw: c.get('e5-plugin-homewifi', 'settings', 'sidegw') == '1', sidegw_ip: s.sidegw_ip, sidegw_net: s.sidegw_net };
	}

	return {
		'GET /status': function(req) {
			checks();
			return state();
		},
		'POST /check': function(req) {
			checks();
			if (!service_running()) return fail('后台服务没有运行，请稍后再试', 503);
			mkdir('/tmp/run'); mkdir(RUN);
			if (writefile(`${RUN}/check-now`, `${time()}\n`) == null) return fail('无法发起检查', 500);
			ctx.log('look asked for');
			return { ok: true };
		},
		'POST /mode': function(req) {
			let m = req.body?.mode;
			if (index([ 'auto', 'home', 'away' ], m) < 0) return fail('参数不对', 400);
			let c = ctx.uci();
			if (!c.get('e5-plugin-homewifi', 'settings')) {
				if (!stat('/etc/config/e5-plugin-homewifi')) writefile('/etc/config/e5-plugin-homewifi', '');
				c.set('e5-plugin-homewifi', 'settings', 'settings');
			}
			c.set('e5-plugin-homewifi', 'settings', 'mode', m);
			if (!c.commit('e5-plugin-homewifi')) return fail('保存失败', 500);
			ctx.log(`mode ${m}`);
			return { ok: true };
		},
		'POST /sidegw': function(req) {
			let on = req.body?.on;
			if (type(on) != 'bool') return fail('参数不对', 400);
			let c = ctx.uci();
			if (!c.get('e5-plugin-homewifi', 'settings')) {
				if (!stat('/etc/config/e5-plugin-homewifi')) writefile('/etc/config/e5-plugin-homewifi', '');
				c.set('e5-plugin-homewifi', 'settings', 'settings');
			}
			c.set('e5-plugin-homewifi', 'settings', 'sidegw', on ? '1' : '0');
			if (!c.commit('e5-plugin-homewifi')) return fail('保存失败', 500);
			ctx.log(`side gateway ${on ? 'on' : 'off'}`);
			return { ok: true };
		},
		'GET /notifications': function(req) {
			checks();
			return { notifications: [] };
		}
	};
};
