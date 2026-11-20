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
// Each value is kept as it was (saved_*) and given back on the way out -- unless it was
// changed by hand meanwhile: then the hand's stays.
//   GET  /state          {on, since, hold_ms, wake_lock, radio_off, usb_power, sleep}
//   POST /toggle         {on?}: in or out (no on: the other way) -> the new state
//   GET  /notifications  (the minute check: early.html put in, if not)
import { readfile, writefile, rename, stat, unlink, glob } from 'fs';

const APP = '/etc/e5-infoscreen/plugins/lowpower';
const CFG = 'e5-plugin-lowpower';
const INDEX = '/usr/share/e5-infoscreen/www/index.html';
const ANCHOR = '<script src="power-menu.js"></script>\n';
const BEGIN = '<script>/* BEGIN e5-lowpower', END = '/* END e5-lowpower */</script>\n';
// what it sets: [ config, option, the value in low power, what it was if unset ]
const KNOBS = [
	[ 'e5-plugin-assistant', 'wake', '0', '0' ],
	[ 'e5-plugin-recorder', 'enabled', '0', '0' ],
	[ 'e5-plugin-hotspotdata', 'mode', 'radio', 'radio' ],
];

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	function get(conf, opt) {
		return ctx.uci().get(conf, 'settings', opt);
	}
	// (only values of the kind these options hold: they go into a shell command)
	const safe = (v) => v != null && match(v, /^[a-z0-9]*$/);
	function set(pairs) {
		let cmd = '';
		let confs = {};
		for (let p in pairs) {
			if (!safe(p[2])) continue;
			cmd += `uci -q get ${p[0]}.settings >/dev/null || uci set ${p[0]}.settings=settings; ` +
				`uci set ${p[0]}.settings.${p[1]}='${p[2]}'; `;
			confs[p[0]] = true;
		}
		for (let c in keys(confs)) cmd += `uci commit ${c}; `;
		if (cmd) ctx.run(`touch /etc/config/${CFG}; ` + cmd);
	}
	function on() { return get(CFG, 'on') == '1'; }
	function enter() {
		let pause_rec = get(CFG, 'pause_recorder') != '0';
		let pairs = [], saved = [];
		for (let k in KNOBS) {
			if (k[0] == 'e5-plugin-recorder' && !pause_rec) continue;
			let was = get(k[0], k[1]) ?? '';
			saved = [ ...saved, [ CFG, `saved_${k[1]}`, safe(was) ? (was == '' ? 'unset' : was) : 'unset' ] ];
			pairs = [ ...pairs, [ k[0], k[1], k[2] ] ];
		}
		set([ ...saved, [ CFG, 'on', '1' ], [ CFG, 'since', sprintf('%d', time()) ] ]);
		set(pairs);
		ctx.log('low power mode on');
	}
	function leave() {
		let pairs = [];
		for (let k in KNOBS) {
			let was = get(CFG, `saved_${k[1]}`);
			if (was == null) continue;                 // (not ours to give back)
			// (changed by hand meanwhile: the hand's stays)
			if ((get(k[0], k[1]) ?? k[3]) != k[2]) continue;
			if (was == 'unset') ctx.run(`uci -q delete ${k[0]}.settings.${k[1]}; uci commit ${k[0]}`);
			else pairs = [ ...pairs, [ k[0], k[1], was ] ];
		}
		set(pairs);
		ctx.run(`for o in saved_wake saved_enabled saved_mode since; do uci -q delete ${CFG}.settings.$o; done; ` +
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
	function checks() {
		if (!ctx.state_get('checked', 60)) {
			ctx.state_put('checked', { at: time() });
			ensure_early();
		}
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
			usb_power: usb,
			sleep: trim(readfile('/var/run/hotspotdata.sleep/state') ?? ''),
			pause_recorder: get(CFG, 'pause_recorder') != '0',
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
		'GET /notifications': function(req) {
			checks();
			return { notifications: [] };
		}
	};
};
