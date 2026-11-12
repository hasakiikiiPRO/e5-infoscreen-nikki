'use strict';
// 流量 (dataplan): the screen half.  service.uc (under /etc/init.d/e5-dataplan)
// asks the carrier and cuts data; this installs that service and keeps it
// installed (the notification poll checks once a minute), and serves
//   GET  /status          the ledger's plan, the last carrier answer, the cutoff
//   POST /query           ask the carrier now
//   POST /resume          no cutoff for the rest of this cycle (data comes back)
//   POST /protect         the cutoff on again for this cycle
//   GET  /notifications   plan running low, data cut
import { readfile, writefile, rename, stat, unlink, mkdir } from 'fs';

const DIR = '/etc/e5-dataplan', RUN = '/tmp/run/e5-dataplan';
const APP = '/etc/e5-infoscreen/plugins/dataplan';
const INIT = '/etc/init.d/e5-dataplan';
// the lock screen's 套餐剩余 row: lock-card.html, put into the screen's page
// right after app.js (as the hotspotdata app puts its CPU card); an image
// update brings back the page without it, and the minute check puts it back
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const INDEX_ANCHOR = '<script src="app.js"></script>\n';
const HTML_BEGIN = '<!-- BEGIN e5-dataplan', HTML_END = '<!-- END e5-dataplan -->\n';

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	function load(path, def) {
		try { return json(readfile(path) ?? 'null') ?? def; } catch (e) { return def; }
	}
	function setting(key, def) {
		return ctx.uci().get('e5-plugin-dataplan', 'settings', key) ?? def;
	}
	// the data SIM's ledger, as the screen's own traffic page picks it
	function ledger() {
		return ctx.ubus('unisoc.traffic', 'get', { slot: `${ctx.sim_card()}` });
	}
	function service_running() {
		let s = ctx.ubus('service', 'list', { name: 'e5-dataplan' })?.['e5-dataplan'];
		for (let k, inst in (s?.instances ?? {})) if (inst.running) return true;
		return false;
	}
	// the init script as this version of the app has it, enabled and running
	function ensure_service() {
		let want = readfile(`${APP}/dataplan.init`);
		if (!want) return;
		if (readfile(INIT) != want) {
			ctx.run(`cp '${APP}/dataplan.init' '${INIT}.new' && chmod 755 '${INIT}.new' && ` +
				`mv -f '${INIT}.new' '${INIT}' && '${INIT}' enable && '${INIT}' restart >/dev/null 2>&1`);
			ctx.log('service installed');
		} else if (!service_running()) {
			ctx.run(`'${INIT}' enable; '${INIT}' start >/dev/null 2>&1`);
		} else if (readfile(`${APP}/service.uc`) != readfile('/var/run/e5-dataplan.running')) {
			// (a reinstalled app: service.uc changed under the running one)
			ctx.run(`setsid '${INIT}' restart >/dev/null 2>&1 &`);
			ctx.log('service restarted for the new version');
		}
	}
	// device-local times, done here: the page's own clock is UTC
	function stamp(sec) {
		let t = localtime(sec);
		return sprintf('%02d-%02d %02d:%02d', t.mon, t.mday, t.hour, t.min);
	}
	// the ledger's cycle end is the next cycle's first day: the last day is the one before
	function last_day(end) {
		let m = match(end ?? '', /^([0-9]+)-([0-9]+)-([0-9]+)$/);
		if (!m) return null;
		let t = localtime(timelocal({ year: +m[1], mon: +m[2], mday: +m[3], hour: 12, min: 0, sec: 0 }) - 86400);
		return sprintf('%02d-%02d', t.mon, t.mday);
	}
	// src without the block from begin to end (end included); null if it is broken
	function cut(src, begin, end) {
		let a = index(src, begin);
		if (a < 0) return src;
		let rest = substr(src, a), b = index(rest, end);
		return b < 0 ? null : substr(src, 0, a) + substr(rest, b + length(end));
	}
	function ensure_lock_card() {
		let src = readfile(INDEX), want = readfile(`${APP}/lock-card.html`);
		if (src == null || want == null) return;
		if (index(src, want) >= 0) return;
		src = cut(src, HTML_BEGIN, HTML_END);
		if (src == null) { ctx.log('index.html: a broken lock card block, left alone'); return; }
		let parts = split(src, INDEX_ANCHOR);
		if (length(parts) != 2) { ctx.log('index.html: no single app.js script, no lock card'); return; }
		let tmp = INDEX + '.dataplan-new';
		if (writefile(tmp, parts[0] + INDEX_ANCHOR + want + parts[1]) == null || !rename(tmp, INDEX)) {
			unlink(tmp);
			return;
		}
		ctx.log('index.html patched (the lock screen\'s plan row), the screen restarts');
		// (detached: this request runs in the screen's own API server)
		ctx.run("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
	}
	function state() {
		let l = ledger(), s = l?.status ?? {};
		let last = load(`${DIR}/last.json`, null), cut = load(`${DIR}/cut.json`, null);
		let override = trim(readfile(`${DIR}/override`) ?? '');
		let busy = load(`${RUN}/querying`, null);
		let limit = s.monthly_limit ?? 0, left = s.remaining;
		let running = service_running();
		let warn = int(setting('warn_percent', '10')) || 10;
		return { ok: true, plan_name: l?.config?.plan_name, limit, used: s.cycle_used, remaining: left,
			today: s.today_used, cycle_start: s.cycle_start, cycle_end: s.cycle_end,
			percent_left: limit > 0 ? (left * 100.0 / limit) : null,
			low: limit > 0 && left != null && left * 100.0 / limit < warn, over: !!s.over_limit,
			available: !!s.available, clock_ok: !!s.clock_ok,
			last, last_at: last?.at ? stamp(last.at) : null,
			cycle_text: s.cycle_start ? `${substr(s.cycle_start, 5)} ~ ${last_day(s.cycle_end) ?? ''}` : null,
			// (a query takes up to ~7 min at worst; an asked-for one counts while the service can take it)
			querying: !!(busy && time() - busy.at < 600) || (running && !!stat(`${RUN}/query-now`)),
			cut: !!cut, cut_at: cut?.at,
			protect: setting('auto_cut', '1') == '1', paused: override != '' && override == s.cycle_start,
			auto_query: setting('auto_query', '1') == '1', query_hours: int(setting('query_hours', '6')) || 6,
			warn_percent: warn, service: running };
	}
	function gb(b) {
		return sprintf('%.2f GB', (b ?? 0) / 1e9);
	}

	return {
		'GET /status': function(req) {
			if (!ctx.state_get('checked', 60)) {
				ctx.state_put('checked', { at: time() });
				ensure_service();
				ensure_lock_card();
			}
			return state();
		},
		'POST /query': function(req) {
			ensure_service();
			let s = state();
			if (s.querying) return fail('正在查询，请稍等', 409);
			if (!s.service) return fail('后台服务没有运行，请稍后再试', 503);
			mkdir('/tmp/run'); mkdir(RUN);
			if (writefile(`${RUN}/query-now`, `${time()}\n`) == null) return fail('无法发起查询', 500);
			ctx.log('query asked for');
			return { ok: true };
		},
		'POST /resume': function(req) {
			let s = state();
			if (!s.cycle_start) return fail('流量统计暂不可用', 503);
			mkdir(DIR);
			let tmp = `${DIR}/override.${time()}`;
			if (writefile(tmp, s.cycle_start + '\n') == null || !rename(tmp, `${DIR}/override`))
				return fail('保存失败', 500);
			ctx.log(`cutoff paused for the cycle from ${s.cycle_start}`);
			return { ok: true };
		},
		'POST /protect': function(req) {
			unlink(`${DIR}/override`);
			ctx.log('cutoff on again');
			return { ok: true };
		},
		'GET /notifications': function(req) {
			if (!ctx.state_get('checked', 60)) {
				ctx.state_put('checked', { at: time() });
				ensure_service();
				ensure_lock_card();
			}
			let s = state(), out = [];
			if (s.cut)
				push(out, { id: 'dataplan-cut', wake: true, title: { zh: '流量已用完，已断开移动数据', en: 'Plan used up: mobile data off' },
					body: '打开「流量」可恢复上网' });
			else if (s.over)
				push(out, { id: 'dataplan-over', wake: true, title: { zh: '套餐流量已用完', en: 'Plan used up' },
					body: s.paused ? '本周期已关闭自动断网，继续使用会产生超额费用' : '' });
			else if (s.low)
				push(out, { id: 'dataplan-low', title: { zh: '套餐流量快用完了', en: 'Plan running low' },
					body: `剩余 ${gb(s.remaining)}（${sprintf('%.0f', s.percent_left)}%）` });
			return { notifications: out };
		}
	};
};
