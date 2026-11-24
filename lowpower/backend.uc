'use strict';
// 低功耗 (lowpower): hold # to go into low power mode, again to come out -- for days at home.
// Nothing new runs: it switches off what keeps the E5 awake, and the system's own savers
// (the hotspotdata app: lowest clocks, cores off, Bluetooth off, deep sleep with the screen
// dark) do the rest:
//   助手「喊名字召唤」 off      e5-plugin-assistant.settings.wake 0 (wake.py lets go of the
//                              microphone and of its wake lock: no sleep while it listens)
//   录音 paused (setting)      e5-plugin-recorder.settings.enabled 0 (the same, while recording)
//   蜂窝射频 off               e5-plugin-hotspotdata.settings.mode radio: with the hotspot off
//                              that service turns the data card's radio off (AT+CFUN=4) within
//                              30 s -- no calls, no SMS; deep sleep waits for it (the modem
//                              would wake the system every few seconds) -- and back to data
//                              mode, the radio back on and the modem enabled again
//   Wi-Fi off                  wireless.radio0.disabled 1: the radio itself (the 在家 app leaves a
//                              radio switched off alone).  This E5's Wi-Fi driver (sprd_wlan_combo,
//                              sc2355) does not survive a suspend while joined to a network: its
//                              queue is not resumed, the disconnect times out, the suspend fails,
//                              and its work loops on a CPU until the kernel's soft lockup panic
//                              reboots the board -- measured 11-21, every few attempts.  With the
//                              radio off it is never in the way.  Offline meanwhile, then.
// Each value is kept as it was (saved_*) and given back on the way out -- unless it was
// changed by hand meanwhile: then the hand's stays.
// On a schedule (setting schedule, schedule_start/end, default 22:00-07:30): cron runs POST /tick
// every minute (while awake: a sleeping E5 runs nothing -- so a scheduled night also has the sleep
// service wake it every 10 minutes, sleep_wake_every, and the morning comes at most that late).
// Only the turn of day to night and night to day does anything: the night turns it on (if it is
// off), the morning turns it off -- only if the night turned it on (auto).  A hand in between --
// out at midnight, in before ten -- is left as it is.  And not while the hotspot has devices on
// it (away from home, the phone online through it): the night waits, minute by minute, until
// they are gone -- low power turns the Wi-Fi off, the hotspot with it.
//   GET  /state          {on, since, hold_ms, wake_lock, radio_off, usb_power, sleep, schedule}
//   POST /toggle         {on?}: in or out (no on: the other way) -> the new state
//   POST /tick           {now?: "HH:MM" (a test)}: the schedule looked at
//   GET  /notifications  (the minute check: early.html put in, if not)
import { readfile, writefile, rename, stat, unlink, glob, popen } from 'fs';

const APP = '/etc/e5-infoscreen/plugins/lowpower';
const CFG = 'e5-plugin-lowpower';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const ANCHOR = '<script src="power-menu.js"></script>\n';
const BEGIN = '<script>/* BEGIN e5-lowpower', END = '/* END e5-lowpower */</script>\n';
// what it sets: [ config, section, option, the value in low power, what it was if unset, saved as ]
const KNOBS = [
	[ 'e5-plugin-assistant', 'settings', 'wake', '0', '0', 'saved_wake' ],
	[ 'e5-plugin-recorder', 'settings', 'enabled', '0', '0', 'saved_enabled' ],
	[ 'e5-plugin-hotspotdata', 'settings', 'mode', 'radio', 'radio', 'saved_mode' ],
	[ 'wireless', 'radio0', 'disabled', '1', '0', 'saved_wifi' ],
	// (a scheduled night only: woken every 10 minutes, so that the morning is not missed)
	[ 'e5-plugin-hotspotdata', 'settings', 'sleep_wake_every', '10', '0', 'saved_wakeevery', 'auto' ],
];
const CRON = '* * * * * curl -s -m 20 -X POST http://127.0.0.1:8088/api/plugins/lowpower/tick >/dev/null 2>&1 # e5-lowpower schedule';

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	function get(conf, opt, sect) {
		return ctx.uci().get(conf, sect ?? 'settings', opt);
	}
	// (only values of the kind these options hold: they go into a shell command)
	const safe = (v) => v != null && match(v, /^[a-z0-9]*$/);
	// pairs: [ config, option, value ] in the settings section, or [ config, option, value, section ]
	function set(pairs) {
		let cmd = '';
		let confs = {};
		for (let p in pairs) {
			if (!safe(p[2])) continue;
			let sect = p[3] ?? 'settings';
			if (sect == 'settings')
				cmd += `uci -q get ${p[0]}.settings >/dev/null || uci set ${p[0]}.settings=settings; `;
			cmd += `uci set ${p[0]}.${sect}.${p[1]}='${p[2]}'; `;
			confs[p[0]] = true;
		}
		for (let c in keys(confs)) cmd += `uci commit ${c}; `;
		if (cmd) ctx.run(`touch /etc/config/${CFG}; ` + cmd);
	}
	function on() { return get(CFG, 'on') == '1'; }
	function enter(auto) {
		let pause_rec = get(CFG, 'pause_recorder') != '0';
		let pairs = [], saved = [];
		for (let k in KNOBS) {
			if (k[0] == 'e5-plugin-recorder' && !pause_rec) continue;
			if (k[6] == 'auto' && !auto) continue;
			let was = get(k[0], k[2], k[1]) ?? '';
			saved = [ ...saved, [ CFG, k[5], safe(was) ? (was == '' ? 'unset' : was) : 'unset' ] ];
			pairs = [ ...pairs, [ k[0], k[2], k[3], k[1] ] ];
		}
		set([ ...saved, [ CFG, 'on', '1' ], [ CFG, 'since', sprintf('%d', time()) ], [ CFG, 'auto', auto ? '1' : '0' ] ]);
		set(pairs);
		ctx.run('setsid sh -c "wifi down radio0; wifi reload" >/dev/null 2>&1 &');
		ctx.log(auto ? 'low power mode on (the schedule)' : 'low power mode on');
	}
	function leave() {
		let pairs = [];
		let wifi = false;
		for (let k in KNOBS) {
			let was = get(CFG, k[5]);
			if (was == null) continue;                 // (not ours to give back)
			// (changed by hand meanwhile: the hand's stays)
			if ((get(k[0], k[2], k[1]) ?? k[4]) != k[3]) continue;
			if (k[0] == 'wireless') wifi = true;
			if (was == 'unset') ctx.run(`uci -q delete ${k[0]}.${k[1]}.${k[2]}; uci commit ${k[0]}`);
			else pairs = [ ...pairs, [ k[0], k[2], was, k[1] ] ];
		}
		set(pairs);
		// (wifi reload does not bring up a radio taken down: up as well)
		if (wifi) ctx.run('setsid sh -c "wifi reload; sleep 2; wifi up radio0" >/dev/null 2>&1 &');
		ctx.run(`for o in saved_wake saved_enabled saved_mode saved_wifi saved_wakeevery since auto; do uci -q delete ${CFG}.settings.$o; done; ` +
			`uci set ${CFG}.settings.on='0'; uci commit ${CFG}`);
		ctx.log('low power mode off');
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
		let parts = split(src, ANCHOR), tmp = INDEX + '.lowpower-new';
		if (writefile(tmp, parts[0] + want + ANCHOR + parts[1]) == null || !rename(tmp, INDEX)) {
			unlink(tmp);
			return;
		}
		ctx.log('index.html patched (hold # for low power), the screen restarts');
		ctx.run("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
	}
	function ensure_cron() {
		let tab = readfile('/etc/crontabs/root') ?? '';
		if (index(tab, CRON) >= 0) return;
		let keep = join('\n', filter(split(tab, '\n'), (l) => l != '' && index(l, '# e5-lowpower schedule') < 0));
		writefile('/etc/crontabs/root', (keep ? keep + '\n' : '') + CRON + '\n');
		ctx.run('/etc/init.d/cron enable; /etc/init.d/cron restart >/dev/null 2>&1');
		ctx.log('schedule: cron installed');
	}
	function checks() {
		if (!ctx.state_get('checked', 60)) {
			ctx.state_put('checked', { at: time() });
			ensure_early();
			ensure_cron();
		}
	}
	function hhmm(v, def) {
		let m = match(v ?? '', /^([0-9]{1,2}):([0-9]{2})$/);
		return m && +m[1] < 24 && +m[2] < 60 ? +m[1] * 60 + +m[2] : def;
	}
	function schedule() {
		return {
			on: get(CFG, 'schedule') != '0',
			start: get(CFG, 'schedule_start') ?? '22:00',
			end: get(CFG, 'schedule_end') ?? '07:30',
		};
	}
	// -> 'night' or 'day' at NOW (minutes into the day)
	function phase(now) {
		let s = schedule();
		let a = hhmm(s.start, 22 * 60), b = hhmm(s.end, 7 * 60 + 30);
		let night = a < b ? (now >= a && now < b) : (now >= a || now < b);
		return night ? 'night' : 'day';
	}
	// devices on the hotspot (Wi-Fi up as an access point), 0 otherwise
	function hotspot_devices() {
		let f = popen('iw dev 2>/dev/null; for i in $(iw dev 2>/dev/null | sed -n "s/^[[:space:]]*Interface //p"); do iw dev $i station dump 2>/dev/null; done', 'r');
		if (!f) return 0;
		let out = f.read('all') ?? '';
		f.close();
		if (index(out, 'type AP') < 0) return 0;
		return length(match(out, /Station [0-9a-f:]+/g) ?? []);
	}
	function tick(now, devices) {
		if (!schedule().on) return 'off';
		let p = phase(now), last = get(CFG, 'sched_phase');
		if (p == last) return p;
		// (the night not marked yet: tried again next minute)
		if (p == 'night' && !on() && (devices ?? hotspot_devices()) > 0) return 'night (waiting: devices on the hotspot)';
		set([ [ CFG, 'sched_phase', p ] ]);
		if (p == 'night' && !on()) enter(true);
		else if (p == 'day' && last == 'night' && on() && get(CFG, 'auto') == '1') leave();
		return p + ' (turned)';
	}
	function hold_ms() {
		let v = int(get(CFG, 'hold_ms') ?? '1000');
		return v >= 300 && v <= 3000 ? v : 1000;
	}
	function state() {
		let locks = trim(readfile('/sys/power/wake_lock') ?? '');
		let usb = false;
		for (let p in glob('/sys/class/power_supply/*/online'))
			if (trim(readfile(p) ?? '') == '1') usb = true;
		let since = int(get(CFG, 'since') ?? '0');
		return {
			ok: true, on: on(), since: since || null, hold_ms: hold_ms(),
			wake_lock: locks == '' ? [] : split(locks, ' '),
			radio_off: length(glob('/var/run/hotspotdata.radio-off.*')) > 0,
			wifi_off: get('wireless', 'disabled', 'radio0') == '1',
			usb_power: usb,
			sleep: trim(readfile('/var/run/hotspotdata.sleep/state') ?? ''),
			pause_recorder: get(CFG, 'pause_recorder') != '0',
			auto: get(CFG, 'auto') == '1',
			schedule: schedule(),
		};
	}

	return {
		'GET /state': function(req) {
			return state();
		},
		'POST /toggle': function(req) {
			checks();
			let want = req.body?.on;
			if (type(want) != 'bool') want = !on();
			if (want && !on()) enter();
			else if (!want && on()) leave();
			// (what was just written: a uci cursor read now may still hold the old)
			return { ...state(), on: want };
		},
		'POST /tick': function(req) {
			let t = localtime();
			let now = t.hour * 60 + t.min;
			if (type(req.body?.now) == 'string') now = hhmm(req.body.now, now);
			let r = tick(now, type(req.body?.devices) == 'int' ? req.body.devices : null);
			return { ok: true, tick: r, on: on() };
		},
		'GET /notifications': function(req) {
			checks();
			return { notifications: [] };
		}
	};
};
