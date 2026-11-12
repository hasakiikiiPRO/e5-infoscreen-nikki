'use strict';
// 流量 (dataplan) -- the background half of the info screen app: asks the
// carrier for the plan by SMS, calibrates the shared traffic ledger
// (unisoc.traffic) with the answer, and takes mobile data down once the
// plan is used up.  /etc/init.d/e5-dataplan runs this under procd; the
// app's backend installs that service and keeps it installed.
//
// The carrier's "GB" is taken as 10^9 bytes, the ledger's unit: the screen,
// LuCI and the SMS then show the same numbers, and the remainder errs low
// (a GiB-counting carrier has ~7% more), which is the safe side for a cutoff.
//
// Failing safe: when the ledger cannot be read, nothing changes (a cut stays
// cut, nothing is cut anew); only a cut this service made is undone.
import { readfile, writefile, rename, mkdir, stat, popen, unlink } from 'fs';
import { cursor } from 'uci';
import * as ubus from 'ubus';

const APP = '/etc/e5-infoscreen/plugins/dataplan';
const DIR = '/etc/e5-dataplan', RUN = '/tmp/run/e5-dataplan';
const LAST = DIR + '/last.json';        // the last query: written here only
const CUT = DIR + '/cut.json';          // the cutoff this service made: written here only
const OVERRIDE = DIR + '/override';     // "no cutoff this cycle": the backend writes it
const MANUAL = DIR + '/manual.json';    // a plan set by hand (no SMS query): its cycle, base, carry-over
const REQ = RUN + '/query-now';         // a query asked for from the screen
const BUSY = RUN + '/querying';
const RUNNING = '/var/run/e5-dataplan.running';  // this script as started: the backend restarts a stale one
const API = 'http://127.0.0.1:8088/api';
const LOOP_S = 20, REPLY_WAIT_S = 120;
const RETRY_S = 900;                    // a failed query again after 15 min, doubling up to the interval
const NEW_CYCLE_WAIT_S = 3 * 3600;      // carriers' figures lag at rollover: ask 3 h into a new cycle
const GONE_LOOPS = 3;                   // the app missing this many loops in a row: removed, not updating

// operator code -> where to ask, and what
const CARRIERS = [
	{ codes: [ '46003', '46005', '46011', '46012' ], name: '中国电信', number: '10001', text: '108' },
	{ codes: [ '46000', '46002', '46004', '46007', '46008', '46013' ], name: '中国移动', number: '10086', text: 'CXLL' },
	{ codes: [ '46001', '46006', '46009', '46010' ], name: '中国联通', number: '10010', text: 'CXLL' },
	// (no SMS query: the app and the hotline only -- the plan is set by hand, manual_gb)
	{ codes: [ '46015' ], name: '中国广电', number: null }
];

let bus = ubus.connect();

function sh(cmd) {
	let p = popen(cmd, 'r');
	if (!p) return '';
	let out = p.read('all');
	p.close();
	return out ?? '';
}
// (single-quoted for the shell: the text is data)
function q(s) {
	return "'" + replace(`${s}`, /'/g, "'\\''") + "'";
}
function log(msg) {
	system(`logger -t e5-dataplan -- ${q(msg)}`);
}
function load(path, def) {
	try { return json(readfile(path) ?? 'null') ?? def; } catch (e) { return def; }
}
function save(path, obj) {
	let tmp = `${path}.${time()}`;
	if (writefile(tmp, sprintf('%J\n', obj)) != null) rename(tmp, path);
}
function setting(key, def) {
	return cursor().get('e5-plugin-dataplan', 'settings', key) ?? def;
}
function api_get(path) {
	try { return json(sh(`curl -s -m 10 ${q(API + path)} 2>/dev/null`)); } catch (e) { return null; }
}
function api_post(path, body) {
	try {
		return json(sh(`curl -s -m 30 -X POST -H 'Content-Type: application/json' ` +
			`--data ${q(sprintf('%J', body))} ${q(API + path)} 2>/dev/null`));
	} catch (e) { return null; }
}
// a ubus call, once more on a fresh connection (ubusd restarted under us)
function call(obj, method, args) {
	let r = bus?.call(obj, method, args);
	if (r == null) {
		bus = ubus.connect();
		r = bus?.call(obj, method, args);
	}
	return r;
}

function data_iface() {
	return cursor().get('unisoc_modem', 'main', 'data_interface') || 'wan';
}
function data_active(i) {
	let st = call('network.interface', 'status', { interface: i });
	return st?.up === true || st?.pending === true;
}
// the hotspot app holds data off with auto=0 while the hotspot is off: not ours to undo
function held_off(i) {
	return cursor().get('network', i, 'auto') == '0';
}
function data_up(i, why) {
	if (held_off(i) || data_active(i)) return;
	log(`${why}: mobile data (${i}) up`);
	call('network.interface', 'up', { interface: i });
}
// the data SIM, as the screen picks it (api.uc sim_card)
function data_slot() {
	return trim(readfile('/sys/module/sipc_wwan/parameters/card') ?? '') == '1' ? 1 : 0;
}
function ledger(slot) {
	return call('unisoc.traffic', 'get', { slot: `${slot ?? data_slot()}` });
}
function day_epoch(date) {
	let m = match(date ?? '', /^([0-9]+)-([0-9]+)-([0-9]+)$/);
	return m ? timelocal({ year: +m[1], mon: +m[2], mday: +m[3], hour: 0, min: 0, sec: 0 }) : null;
}

function carrier() {
	let op = trim(sh("mmcli -m any -K 2>/dev/null | sed -n 's/^modem\\.3gpp\\.operator-code *: *//p' | head -n 1"));
	for (let c in CARRIERS)
		if (index(c.codes, op) >= 0) return { ...c, op };
	return null;
}

// "146.42GB" -> bytes, the carrier's GB as 10^9
function to_bytes(num, unit) {
	let f = { T: 1e12, G: 1e9, M: 1e6, K: 1e3 }[uc(substr(unit, 0, 1))];
	return f ? int(+num * f) : null;
}
const NUM = '([0-9]+(\\.[0-9]+)?) *([GgMmKkTt][Bb])';
function amount(text, label) {
	// (POSIX regex: the label, up to 40 bytes that are no digits, the amount; a
	// label like "(共有|总共)" is a group of its own: the amount's are the last three)
	let m = match(text, regexp(label + '[^0-9]{0,40}' + NUM)), n = length(m ?? []);
	return m ? to_bytes(m[n - 3], m[n - 1]) : null;
}
// the general (通用) allowance of one reply, or null.  Asides in brackets go
// first -- "(含上月结转15.42GB)" is part of the total, not what is left -- and
// a reply about directed (定向) data alone is not the plan.
function parse(text) {
	if (index(text, '定向') >= 0 && index(text, '通用') < 0) return null;
	let t = replace(text, /[(（][^)）]*[)）]/g, '');
	let total = amount(t, '(通用|共有|总共|总量)'), left = amount(t, '剩余');
	let used = amount(t, '(已使用|已用)');
	if (total != null && left != null && used != null) {
		// all three: they have to add up (to 1%), or it is not one allowance
		let gap = total - used - left;
		if (gap > total / 100 || -gap > total / 100) return null;
	}
	else if (total != null && left != null) used = total - left;
	else if (total != null && used != null) left = total - used;
	if (total == null || used == null || left == null || total <= 0 || used < 0 || left < 0) return null;
	return { total, used, remaining: left, general: index(text, '通用') >= 0 };
}

function calibrate(slot, plan) {
	let l = ledger(slot), c = l?.config;
	if (!c) return '流量统计不可用';
	// the adjustment belongs to the cycle it is set in: a wrong clock would put it in the wrong one
	if (!l.status?.clock_ok) return '系统时间还没同步';
	let gb = (b) => int(b / 1e6 + 0.5) / 1000.0;
	// the billing day and the rest as the user set them; the carrier counts
	// both directions, so the cutoff has to as well
	let payload = { plan_name: c.plan_name || '运营商套餐', monthly_gb: gb(plan.total),
		daily_gb: c.daily_gb ?? 0, reset_day: c.reset_day ?? 1, count_mode: 'total',
		device: c.device ?? '', used_gb: gb(plan.used) };
	let r = call('unisoc.traffic', 'set', { slot: `${slot}`, payload: sprintf('%J', payload) });
	return r?.ok ? null : `${r?.error ?? '写入失败'}`;
}

// cut data once the plan is used up; give it back only if this service took it
function guard() {
	let s = ledger()?.status;
	// nothing known, nothing changed
	if (!s || !s.clock_ok || !s.cycle_start) return;
	let cut = load(CUT, null), i = cut?.iface ?? data_iface();
	let over = !!(s.monthly_limit > 0 && s.over_limit);
	let override = trim(readfile(OVERRIDE) ?? '');
	let want_cut = over && setting('auto_cut', '1') == '1' && override != s.cycle_start;
	if (want_cut) {
		i = data_iface();
		if (data_active(i)) {
			if (!cut) log(`plan used up (${s.cycle_used} of ${s.monthly_limit} bytes): mobile data (${i}) down`);
			system(`ifdown ${q(i)}`);
			if (!cut) save(CUT, { at: time(), cycle: s.cycle_start, iface: i });
		}
		return;
	}
	if (cut) {
		unlink(CUT);
		data_up(i, 'cutoff lifted');
	}
}

// A carrier without an SMS query: the plan from the settings (manual_gb a
// month), the usage the ledger's own.  A new cycle gets the base plus what
// was left of the last one's base (carried over; what is carried in is used
// first, so no more than one base carries).  The usage is kept in RUN every
// loop and on flash once an hour: the carry-over needs the last cycle's end.
let manual_mem = null;
function manual_apply(car) {
	let slot = data_slot(), l = ledger(slot), s = l?.status, c = l?.config;
	if (!s?.clock_ok || !s.cycle_start || !c) return;
	let base = +setting('manual_gb', '0');
	let m = manual_mem ?? load(RUN + '/manual.json', null) ?? load(MANUAL, null);
	if (m && m.slot != slot) m = null;
	let set = (gb) => call('unisoc.traffic', 'set', { slot: `${slot}`, payload: sprintf('%J', {
		plan_name: c.plan_name || `${car.name}套餐`, monthly_gb: int(gb * 1000 + 0.5) / 1000.0,
		daily_gb: c.daily_gb ?? 0, reset_day: c.reset_day ?? 1, count_mode: c.count_mode ?? 'total', device: c.device ?? '' }) });
	if (!m) {
		// first time: a quota already in the ledger stands (a prorated first month set by hand)
		if (!(s.monthly_limit > 0)) {
			if (!(base > 0)) return;
			set(base);
			log(`${car.name}: no SMS query, plan ${base} GB a month (manual_gb)`);
			s = ledger(slot)?.status ?? s;
		}
		m = { slot, cycle: s.cycle_start, base: s.monthly_limit / 1e9, carry: 0 };
		save(MANUAL, m);
	} else if (m.cycle != s.cycle_start) {
		let left = (m.limit ?? 0) - (m.used ?? 0), carry = 0;
		if (setting('manual_carry', '1') == '1' && left > 0) carry = min(left, (m.base ?? 0) * 1e9) / 1e9;
		if (base > 0) {
			set(base + carry);
			log(sprintf('%s: new cycle %s, %.2f GB + %.2f GB carried over', car.name, s.cycle_start, base, carry));
		}
		m = { slot, cycle: s.cycle_start, base, carry };
		s = ledger(slot)?.status ?? s;
		save(MANUAL, m);
	} else if (base > 0 && !m.prorated && m.base != base && s.monthly_limit > 0) {
		// (the setting changed mid-cycle: the new base from now, the carry-over kept)
		set(base + (m.carry ?? 0));
		m.base = base;
		s = ledger(slot)?.status ?? s;
	}
	m.limit = s.monthly_limit; m.used = s.cycle_used;
	manual_mem = m;
	writefile(RUN + '/manual.json', sprintf('%J', m));
	if (time() - (m.saved ?? 0) >= 3600) { m.saved = time(); save(MANUAL, m); }
	// for the screen: the plan in place of a carrier answer
	let last = load(LAST, {});
	if (!last.manual || last.carrier != car.name || time() - (last.at ?? 0) >= 3600) {
		save(LAST, { ok: true, manual: true, carrier: car.name, at: time(), total: s.monthly_limit,
			used: s.cycle_used, remaining: s.remaining, ok_at: time(), fails: 0, ok_cycle: s.cycle_start,
			text: sprintf('%s不支持短信查询：按套餐每月 %s GB 计算%s，用量为本机统计', car.name,
				base > 0 ? sprintf('%g', base) : '?', (m.carry ?? 0) > 0 ? sprintf('（本月含结转 %.2f GB）', m.carry) : '') });
	}
}

function finish(result) {
	let last = load(LAST, {}), now = time();
	if (result.ok) {
		last = { ...result, at: now, ok_at: now, fails: 0, ok_cycle: ledger()?.status?.cycle_start };
	} else {
		// a failed query keeps the last good answer to show
		last.at = now; last.ok = false; last.error = result.error;
		last.fails = (last.fails ?? 0) + 1;
		log('query failed: ' + result.error);
	}
	save(LAST, last);
	unlink(BUSY);
	return result;
}

// the newest reply that is the general allowance, among the carrier's new messages
function pick(msgs, number, after) {
	let best = null;
	for (let m in msgs) {
		if (m.number != number || !(m.id > after) || m.direction == 'out') continue;
		let plan = parse(m.text ?? '');
		if (!plan) continue;
		if (!best || (plan.general && !best.plan.general) || (plan.general == best.plan.general && m.id > best.m.id))
			best = { m, plan };
	}
	return best;
}

function query() {
	writefile(BUSY, sprintf('%J', { at: time() }));
	let car = carrier();
	if (!car) return finish({ ok: false, error: '没有识别出运营商（没插卡或没注册上网络）' });
	if (!car.number) { unlink(BUSY); return { ok: true, manual: true }; }
	let msgs = api_get('/sms')?.messages;
	if (type(msgs) != 'array') return finish({ ok: false, error: '读不到短信' });
	let before = 0;
	for (let m in msgs) if (m.number == car.number && m.id > before) before = m.id;
	let sent = api_post('/sms-send', { number: car.number, text: car.text });
	if (!sent?.ok) return finish({ ok: false, error: '查询短信没发出去：' + (sent?.error ?? '无响应') });
	let found = null;
	for (let waited = 0; waited < REPLY_WAIT_S; waited += 5) {
		system('sleep 5');
		guard();
		let best = pick(api_get('/sms')?.messages ?? [], car.number, before);
		if (!best) continue;
		// a reply comes in parts and in several messages: a few seconds more for the rest
		if (!found) { found = best; continue; }
		found = best;
		break;
	}
	if (!found) return finish({ ok: false, error: `${REPLY_WAIT_S} 秒内没有收到${car.name}能看懂的回复` });
	let slot = data_slot(), err = calibrate(slot, found.plan);
	if (err) return finish({ ok: false, error: '校准流量统计失败：' + err });
	let p = found.plan;
	log(sprintf('%s: total %d, used %d, left %d bytes (slot %d)', car.name, p.total, p.used, p.remaining, slot));
	return finish({ ok: true, carrier: car.name, total: p.total, used: p.used, remaining: p.remaining,
		text: found.m.text, slot });
}

function query_due() {
	if (stat(REQ)) { unlink(REQ); return true; }
	if (setting('auto_query', '1') != '1') return false;
	let hours = int(setting('query_hours', '6')) || 6, now = time();
	let last = load(LAST, {}), s = ledger()?.status;
	// (back from a card set by hand: the SMS carrier's own figures at once)
	if (last.manual) return true;
	let stale = now - (last.ok_at ?? 0) >= hours * 3600;
	// a new cycle: the carrier's new allowance (carry-over too), once its figures have caught up
	let start = day_epoch(s?.cycle_start);
	if (s?.clock_ok && start && last.ok_cycle != null && s.cycle_start != last.ok_cycle &&
	    now >= start + NEW_CYCLE_WAIT_S)
		stale = true;
	if (!stale) return false;
	// failures back off: 15 min, 30, 60 ... up to the interval -- never a stream of SMS
	let fails = last.fails ?? 0;
	if (fails > 0) {
		let wait = RETRY_S;
		for (let i = 1; i < fails && wait < hours * 3600; i++) wait *= 2;
		if (now - (last.at ?? 0) < min(wait, hours * 3600)) return false;
	}
	return true;
}

// the lock screen's row out of the screen's page, the screen restarted
function unpatch_index() {
	const INDEX = '/usr/share/e5-infoscreen/www/index.html';
	let src = readfile(INDEX), a = index(src ?? '', '<!-- BEGIN e5-dataplan');
	if (a < 0) return;
	let rest = substr(src, a), end = '<!-- END e5-dataplan -->\n', b = index(rest, end);
	if (b < 0) return;
	let tmp = INDEX + '.dataplan-new';
	if (writefile(tmp, substr(src, 0, a) + substr(rest, b + length(end))) != null && rename(tmp, INDEX))
		system("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
}

function removed() {
	unpatch_index();
	let cut = load(CUT, null);
	if (cut) { unlink(CUT); data_up(cut.iface ?? data_iface(), 'app removed'); }
	log('app removed: stopping');
	// (procd respawns an instance that just exits: the service goes from procd first)
	// (the init script goes first: the service's delete ends this process)
	system(`/etc/init.d/e5-dataplan disable; rm -f /etc/init.d/e5-dataplan ${RUNNING}; ubus call service delete '{"name":"e5-dataplan"}'`);
	exit(0);
}

mkdir(DIR); mkdir('/tmp/run'); mkdir(RUN);
unlink(BUSY);
writefile(RUNNING, readfile(`${APP}/service.uc`) ?? '');
log('started');
let gone = 0;
while (true) {
	gone = stat(`${APP}/manifest.json`) ? 0 : gone + 1;
	if (gone >= GONE_LOOPS) removed();
	if (!gone) {
		guard();
		let car = carrier();
		if (car && !car.number) { if (stat(REQ)) unlink(REQ); manual_apply(car); }
		else if (query_due()) query();
		guard();
	}
	system(`sleep ${LOOP_S}`);
}
