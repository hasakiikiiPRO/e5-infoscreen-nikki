'use strict';
// 录音 (recorder): the screen half.  recorder.py (under /etc/init.d/e5-recorder)
// records; this installs that service and keeps it installed, puts the lock
// screen's "● 录音中" mark into the screen's page (the minute check), and serves
//   GET  /status   what the service last wrote, and the finished files
//   POST /record   {"on": true|false}
import { readfile, writefile, rename, stat, unlink, lsdir, popen, mkdir } from 'fs';

const OUT = '/mnt/e5-data/e5rec', RUN = '/tmp/run/e5-recorder';
const APP = '/etc/e5-infoscreen/plugins/recorder';
const INIT = '/etc/init.d/e5-recorder';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const INDEX_ANCHOR = '<script src="app.js"></script>\n';
const HTML_BEGIN = '<!-- BEGIN e5-recorder', HTML_END = '<!-- END e5-recorder -->\n';
// The side key (the image's own sidekey app, built in and locked): a third
// choice for holding it, "开关录音".  Its files are the image's, so an image
// update brings them back without it -- the minute check puts it in again,
// each edit only where its anchor is (another sidekey version: left alone).
const SK = '/usr/share/e5-infoscreen/www/plugins/sidekey';
// Reading what the NAS made of the recordings (e5rec_server.py): its read API,
// with the same pinned certificate and token the uploads use; the last answer
// of each kept, to show something offline.
const NAS_CERT = '/etc/e5rec/nas.pem', NAS_HDR = RUN + '/nas-auth.hdr', NAS_CACHE = RUN + '/nas-cache';

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	function load(path, def) {
		try { return json(readfile(path) ?? 'null') ?? def; } catch (e) { return def; }
	}
	function service_running() {
		let s = ctx.ubus('service', 'list', { name: 'e5-recorder' })?.['e5-recorder'];
		for (let k, inst in (s?.instances ?? {})) if (inst.running) return true;
		return false;
	}
	function ensure_service() {
		let want = readfile(`${APP}/recorder.init`);
		if (!want) return;
		if (readfile(INIT) != want) {
			ctx.run(`cp '${APP}/recorder.init' '${INIT}.new' && chmod 755 '${INIT}.new' && ` +
				`mv -f '${INIT}.new' '${INIT}' && '${INIT}' enable && '${INIT}' restart >/dev/null 2>&1`);
			ctx.log('service installed');
		} else if (!service_running()) {
			ctx.run(`'${INIT}' enable; '${INIT}' start >/dev/null 2>&1`);
		} else if (readfile(`${APP}/recorder.py`) != readfile('/var/run/e5-recorder.running')) {
			// (a new version: the running segment is closed cleanly on SIGTERM, then it goes on)
			ctx.run(`setsid '${INIT}' restart >/dev/null 2>&1 &`);
			ctx.log('service restarted for the new version');
		}
	}
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
		if (src == null) return;
		let parts = split(src, INDEX_ANCHOR);
		if (length(parts) != 2) return;
		let tmp = INDEX + '.recorder-new';
		if (writefile(tmp, parts[0] + INDEX_ANCHOR + want + parts[1]) == null || !rename(tmp, INDEX)) {
			unlink(tmp);
			return;
		}
		ctx.log('index.html patched (the lock screen\'s recording mark), the screen restarts');
		ctx.run("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
	}
	function edit(path, from, to, done) {
		let src = readfile(path);
		if (src == null || index(src, done) >= 0) return false;
		if (length(split(src, from)) != 2) return false;
		let tmp = path + '.recorder-new';
		if (writefile(tmp, replace(src, from, to)) == null || !rename(tmp, path)) { unlink(tmp); return false; }
		return true;
	}
	function ensure_sidekey() {
		let fn = readfile(`${APP}/sidekey-record.js`);
		if (fn == null || !stat(`${SK}/patch.js`)) return;
		let n = 0;
		n += edit(`${SK}/backend.uc`, "const MODES = [ 'toggle', 'page' ];",
			"const MODES = [ 'toggle', 'page', 'record' ];", "'record' ]");
		n += edit(`${SK}/backend.uc`, "return { mode, toggle: mode == 'toggle', hold,",
			"return { mode, toggle: mode == 'toggle' || mode == 'record', hold,", "mode == 'record', hold,");
		n += edit(`${SK}/patch.js`, "function sidekeyToggle(m) {\n",
			fn + "function sidekeyToggle(m) {\n", 'function sidekeyRecord(m)');
		n += edit(`${SK}/patch.js`, "\tif (!last || !last.wifi) return;              // no status yet\n",
			"\tif (m.mode == 'record') { sidekeyRecord(m); return; }\n\tif (!last || !last.wifi) return;              // no status yet\n",
			"m.mode == 'record'");
		let man = null;
		try { man = json(readfile(`${SK}/manifest.json`)); } catch (e) {}
		let modes = man?.settings?.[0];
		if (modes?.id == 'mode' && !length(filter(modes.options ?? [], o => o.value == 'record'))) {
			push(modes.options, { value: 'record', label: { zh: '开关录音（「录音」应用）', en: 'Recording on / off' } });
			let tmp = `${SK}/manifest.json.recorder-new`;
			if (writefile(tmp, sprintf('%.J\n', man)) != null && rename(tmp, `${SK}/manifest.json`)) n++;
		}
		if (n) ctx.log(`side key: recording choice put in (${n} edits); sidekey re-patches the screen`);
	}
	function nas_get(path, key) {
		let c = ctx.uci();
		let url = c.get('e5-plugin-recorder', 'settings', 'upload_url'), token = c.get('e5-plugin-recorder', 'settings', 'upload_token');
		if (!url || !token || !stat(NAS_CERT)) return { status: 409, type: 'application/json',
			body: sprintf('%J', { ok: false, error: '还没设置 NAS（upload_url / upload_token / 证书）' }) };
		// (it goes into a shell command: only https://host[:port])
		if (!match(url, /^https:\/\/[A-Za-z0-9.-]+(:[0-9]+)?$/)) return fail('upload_url 格式不对', 409);
		// (the token and the cached transcripts: root's only)
		mkdir(RUN); ctx.run(`chmod 700 '${RUN}'`); mkdir(NAS_CACHE);
		// (the token in a header file, not on a command line anyone can read)
		let hdr = 'Authorization: Bearer ' + token + '\n';
		if (readfile(NAS_HDR) != hdr) {
			writefile(NAS_HDR + '.new', hdr);
			ctx.run(`chmod 600 '${NAS_HDR}.new'`);
			rename(NAS_HDR + '.new', NAS_HDR);
		}
		let p = popen(`curl -s -m 15 --noproxy '*' --cacert '${NAS_CERT}' -H @'${NAS_HDR}' -w '\n%{http_code}' '${url}${path}' 2>/dev/null`, 'r');
		let out = p ? p.read('all') : '';
		if (p) p.close();
		let nl = rindex(out ?? '', '\n');
		let code = nl >= 0 ? int(substr(out, nl + 1)) : 0, body = nl >= 0 ? substr(out, 0, nl) : '';
		let cache = `${NAS_CACHE}/${key}.json`;
		if (code == 200) {
			writefile(cache + '.new', body);
			rename(cache + '.new', cache);
			return { status: 200, type: 'application/json', body: body };
		}
		let old = readfile(cache);
		if (old != null) {
			let d = null;
			try { d = json(old); } catch (e) {}
			if (d) return { ok: true, ...d, stale: true, error: code ? `NAS 返回 ${code}` : '连不上 NAS，显示的是上次的内容' };
		}
		return fail(code ? `NAS 返回 ${code}` : '连不上 NAS（没网？）', 502);
	}
	function checks() {
		if (!ctx.state_get('checked', 60)) {
			ctx.state_put('checked', { at: time() });
			ensure_service();
			ensure_lock_mark();
			ensure_sidekey();
		}
	}
	function stamp(sec) {
		if (!sec) return null;
		let t = localtime(sec);
		return sprintf('%02d-%02d %02d:%02d', t.mon, t.mday, t.hour, t.min);
	}
	function files() {
		let out = [];
		for (let f in (lsdir(OUT) ?? [])) {
			let m = match(f, /^(rec-[0-9]{8}-[0-9]{6}(-[0-9]+)?)\.mp3$/);
			if (!m) continue;
			let meta = load(`${OUT}/${m[1]}.json`, {});
			push(out, { name: m[1], seconds: meta.seconds, start: stamp(meta.start), mb: (stat(`${OUT}/${f}`)?.size ?? 0) / 1048576.0 });
		}
		return sort(out, (a, b) => a.name < b.name ? 1 : -1);
	}

	return {
		'GET /status': function(req) {
			checks();
			let s = load(`${RUN}/state.json`, {});
			let ls = s.last_sync;
			return { ok: true, ...s, enabled: ctx.uci().get('e5-plugin-recorder', 'settings', 'enabled') == '1',
				fresh: time() - (s.at ?? 0) < 30, service: service_running(),
				last_sync_at: stamp(ls?.at), last_sync_files: ls?.files, files: slice(files(), 0, 20) };
		},
		'POST /record': function(req) {
			let on = req.body?.on;
			if (type(on) != 'bool') return fail('参数不对', 400);
			checks();
			let c = ctx.uci();
			if (!c.get('e5-plugin-recorder', 'settings')) {
				if (!stat('/etc/config/e5-plugin-recorder')) writefile('/etc/config/e5-plugin-recorder', '');
				c.set('e5-plugin-recorder', 'settings', 'settings');
			}
			c.set('e5-plugin-recorder', 'settings', 'enabled', on ? '1' : '0');
			if (!c.commit('e5-plugin-recorder')) return fail('保存失败', 500);
			ctx.log(on ? 'recording on' : 'recording off');
			return { ok: true };
		},
		'GET /nas/recent': function(req) {
			return nas_get('/v1/recent?limit=40', 'recent');
		},
		'GET /nas/day': function(req) {
			let d = req.query?.d ?? '';
			if (!match(d, /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/)) return fail('日期不对', 400);
			return nas_get(`/v1/day/${d}`, `day-${d}`);
		},
		'GET /nas/rec': function(req) {
			let n = req.query?.name ?? '';
			if (!match(n, /^rec-[0-9]{8}-[0-9]{6}(-[0-9]+)?(-dup[0-9]+)?$/)) return fail('录音名不对', 400);
			return nas_get(`/v1/rec/${n}`, n);
		},
		'GET /notifications': function(req) {
			checks();
			return { notifications: [] };
		}
	};
};
