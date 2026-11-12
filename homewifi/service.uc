'use strict';
// 在家 (homewifi) -- the background half of the info screen app: the E5 joins
// the home WiFi when it is in reach (hotspot off: the hotspotdata app then
// takes mobile data down and lets the device sleep), and puts the hotspot back
// once the home WiFi is gone.  /etc/init.d/e5-homewifi runs this under procd;
// the app's backend installs that service and keeps it installed.
//
// The radio does one thing at a time (no AP+STA): while away it cannot see
// the home WiFi, so it looks -- the hotspot off for 3-4 s -- only when the
// cells around are ones it has seen at home, every SCAN_S.  The cells are
// learnt while it is at home; far from home it never looks.
//
// wireless: default_radio0 is the hotspot (mode ap), homewifi_sta the client
// (mode sta, network wwan, ifname wlan0); one of them enabled at a time.
// The radio itself disabled is the hotspot switched off by hand (hotspot.uc):
// left alone.
//
// Never stranded: whatever goes wrong, the hotspot comes back -- except in
// "always home", which keeps to the home WiFi as asked.
import { readfile, writefile, rename, mkdir, stat, popen, unlink } from 'fs';
import { cursor } from 'uci';

const APP = '/etc/e5-infoscreen/plugins/homewifi';
const DIR = '/etc/e5-homewifi', RUN = '/tmp/run/e5-homewifi';
const CELLS = DIR + '/cells.json';      // cells seen at home: written here only
const STATE = RUN + '/state.json';      // for the screen: written here only
const REQ = RUN + '/check-now';         // a look asked for from the screen
const LOOKING = RUN + '/looking';       // a look in progress: the backend waits with a version restart
const RUNNING = '/var/run/e5-homewifi.running';
const AP = 'default_radio0', STA = 'homewifi_sta', RADIO = 'radio0', IFACE = 'wlan0';
const LOOP_S = 15;
const LOST_S = 60;                      // home WiFi gone this long, awake: left home
const SCAN_S = 150;                     // near home: a look this often ...
const SCAN_MISS_S = 600;                // ... after 3 misses in a row, this often
const MISS_RESET_S = 900;               // misses forgotten after this long away from the home cells
const JOIN_WAIT_S = 40;                 // joining the home WiFi (link and an address): given up after this
const JOIN_FAIL_S = 600;                // a failed join: no new look for this long
const FLAP_S = 600;                     // home WiFi lost this soon after joining it ...
const FLAP_HOLD_S = 1800;               // ... no going home for this long
const MANUAL_HOLD_S = 1800;             // the hotspot turned on by hand at home: no going home for this long
const LEARN_S = 300;                    // at home: cells recorded this often
const CELLS_MAX = 64;
const GONE_LOOPS = 3;

function sh(cmd) {
	let p = popen(cmd, 'r');
	if (!p) return '';
	let out = p.read('all');
	p.close();
	return out ?? '';
}
function q(s) {
	return "'" + replace(`${s}`, /'/g, "'\\''") + "'";
}
function log(msg) {
	system(`logger -t e5-homewifi -- ${q(msg)}`);
}
function load(path, def) {
	try { return json(readfile(path) ?? 'null') ?? def; } catch (e) { return def; }
}
function save(path, obj) {
	let tmp = `${path}.${time()}`;
	if (writefile(tmp, sprintf('%J\n', obj)) == null || !rename(tmp, path)) unlink(tmp);
}
function setting(key, def) {
	return cursor().get('e5-plugin-homewifi', 'settings', key) ?? def;
}
function wget(sec, opt) {
	return cursor().get('wireless', sec, opt);
}

// hotspot | home | off (the radio off by hand) | both (the hotspot turned on at home) | none
function wifi_state() {
	if (wget(RADIO, 'disabled') == '1') return 'off';
	let ap = wget(AP, 'disabled') != '1', sta = wget(STA, 'disabled') != '1';
	return ap && sta ? 'both' : ap ? 'hotspot' : sta ? 'home' : 'none';
}
function set_wifi(home) {
	let c = cursor();
	c.set('wireless', RADIO, 'disabled', '0');
	c.set('wireless', AP, 'disabled', home ? '1' : '0');
	c.set('wireless', STA, 'disabled', home ? '0' : '1');
	c.commit('wireless');
	// (reload alone leaves a radio that "wifi down" took down where it is: up as well)
	system(`wifi reload >/dev/null 2>&1; wifi up ${RADIO} >/dev/null 2>&1`);
}
function iface_type() {
	let m = match(sh(`iw dev ${IFACE} info 2>/dev/null`), /type ([A-Za-z_]+)/);
	return m ? m[1] : null;
}
function beaconing() {
	return iface_type() == 'AP' && sh(`ubus call hostapd.${IFACE} get_status 2>/dev/null`) != '';
}
// a managed wlan0 left from a look that was cut short (netifd does not own it,
// and the hotspot's wlan0 cannot come up beside it): away with it
function clear_stray() {
	if (iface_type() == 'managed' && wifi_state() == 'hotspot') {
		log('a client wlan0 in the hotspot\'s way: removed');
		system(`iw dev ${IFACE} del 2>/dev/null`);
		return true;
	}
	return false;
}
// the hotspot as configured, really on the air: once more if not
function hotspot_back() {
	for (let attempt = 0; attempt < 2; attempt++) {
		for (let i = 0; i < 20; i++) {
			if (beaconing()) return true;
			// (switched off by hand meanwhile: not ours to bring back)
			if (wifi_state() == 'off') return false;
			system('sleep 1');
		}
		log('hotspot not on the air: radio down and up again');
		clear_stray();
		system(`wifi down ${RADIO} >/dev/null 2>&1; sleep 2; wifi up ${RADIO} >/dev/null 2>&1`);
	}
	return beaconing();
}

// the SSID as iw prints it (util.c print_ssid_escaped): what is not printable
// ASCII, a backslash, and a space at either end as \xNN
function iw_ssid(s) {
	let out = '', n = length(s);
	for (let i = 0; i < n; i++) {
		let b = ord(s, i);
		if (b > 32 && b < 127 && b != 92) out += chr(b);
		else if (b == 32 && i != 0 && i != n - 1) out += ' ';
		else out += sprintf('\\x%02x', b);
	}
	return out;
}
function home_ssid() {
	return wget(STA, 'ssid');
}
// on the home WiFi, past the handshake and with an address: { signal } or null
function at_home_link() {
	let out = sh(`iw dev ${IFACE} link 2>/dev/null`), want = home_ssid();
	let m = match(out, /SSID: ([^\n]*)/), s = match(out, /signal: (-?[0-9]+)/);
	if (index(out, 'Connected') < 0 || !m || !want || m[1] != iw_ssid(want)) return null;
	let wpa = match(sh(`wpa_cli -p /var/run/wpa_supplicant -i ${IFACE} status 2>/dev/null`), /wpa_state=([A-Z_]+)/);
	if (wpa && wpa[1] != 'COMPLETED') return null;
	let up = match(sh('ifstatus wwan 2>/dev/null'), /"up": (true|false)/);
	if (!up || up[1] != 'true') return null;
	return { signal: s ? +s[1] : null };
}

// the cells around, as tokens: "ci:<id>" for the serving one, "pci:<arfcn>:<pci>" for every one heard
function cells() {
	let lines = null;
	try { lines = json(sh('mmcli -J -m any --timeout=5 --get-cell-info 2>/dev/null'))?.modem?.generic?.['cell-info']; } catch (e) {}
	let out = [];
	for (let l in (lines ?? [])) {
		let pci = match(l, /physical ci: ([0-9A-Fa-f]+)/), f = match(l, /(nrarfcn|earfcn|arfcn|uarfcn): ([0-9]+)/);
		// (", ci:" -- not the " ci:" of "physical ci:")
		let ci = match(l, /(^|, )ci: ([0-9A-Fa-f]+)/), rsrp = match(l, /rsrp: (-?[0-9.]+)/);
		if (index(l, 'serving: yes') >= 0 && ci) push(out, 'ci:' + uc(ci[2]));
		if (pci && f && (!rsrp || +rsrp[1] > -115)) push(out, `pci:${f[2]}:${uc(pci[1])}`);
	}
	return out;
}
// (written only when a cell is new or an hour has gone by: flash)
function learn(seen) {
	if (!length(seen)) return;
	let db = load(CELLS, { cells: {} }), now = time(), changed = now - (db.saved ?? 0) >= 3600;
	for (let t in seen) {
		if (!db.cells[t]) changed = true;
		db.cells[t] = now;
	}
	if (!changed) return;
	let ks = sort(keys(db.cells), (a, b) => db.cells[b] - db.cells[a]);
	for (let i = CELLS_MAX; i < length(ks); i++) delete db.cells[ks[i]];
	db.saved = now;
	save(CELLS, db);
}
// the serving cell one seen at home, or two of the cells heard
function near_home(seen) {
	let db = load(CELLS, { cells: {} }).cells, hits = 0;
	for (let t in seen) {
		if (!db[t]) continue;
		if (substr(t, 0, 3) == 'ci:') return true;
		hits++;
	}
	return hits >= 2;
}

// the hotspot off for a moment, a look for the home WiFi: its best signal, or null.
// The caller brings the hotspot back (or joins).
function look() {
	let want = iw_ssid(home_ssid() ?? '');
	if (want == '') return null;
	writefile(LOOKING, `${time()}\n`);
	system(`wifi down ${RADIO} >/dev/null 2>&1`);
	for (let i = 0; i < 10; i++) {
		if (iface_type() != 'AP') break;
		system('sleep 1');
	}
	// (an interface of our own when the hotspot's went with it: taken away again after the look)
	let made = system(`ip link show ${IFACE} >/dev/null 2>&1`) != 0;
	if (made) system(`iw phy phy0 interface add ${IFACE} type managed 2>/dev/null`);
	system(`iw dev ${IFACE} set type managed 2>/dev/null; ip link set ${IFACE} up 2>/dev/null`);
	let best = null, sig = null;
	for (let l in split(sh(`iw dev ${IFACE} scan 2>/dev/null`), '\n')) {
		if (match(l, /^BSS /)) sig = null;
		let s = match(l, /^[[:space:]]*signal: (-?[0-9.]+)/);
		if (s) sig = +s[1];
		let m = match(l, /^[[:space:]]*SSID: (.*)$/);
		if (m && m[1] == want && sig != null && (best == null || sig > best)) best = sig;
	}
	system(`ip link set ${IFACE} down 2>/dev/null`);
	if (made) system(`iw dev ${IFACE} del 2>/dev/null`);
	unlink(LOOKING);
	return best;
}

let st = load(STATE, {});
function status(patch) {
	st = { ...st, ...patch, at: time() };
	save(STATE, st);
}

let lost_since = null, misses = 0, last_look = 0, last_learn = 0, held_until = 0, last_near = 0;
let joined_at = 0, last_step = 0;

function go_away(why, hold) {
	log(`${why}: hotspot on`);
	lost_since = null;
	set_wifi(false);
	hotspot_back();
	// (no look straight away: a home WiFi that will not hold would flap)
	last_look = time();
	if (hold) held_until = time() + hold;
	status({ mode: 'away', since: time(), reason: why, lost: 0, held_until: held_until > time() ? held_until : null });
}
// join the home WiFi; keep: stay on it even if it does not come (always home)
function go_home(why, seen, keep) {
	log(`${why}: joining ${home_ssid()}`);
	learn(seen ?? []);
	lost_since = null;
	set_wifi(true);
	for (let i = 0; i < JOIN_WAIT_S; i += 2) {
		system('sleep 2');
		let l = at_home_link();
		if (l) {
			joined_at = time();
			status({ mode: 'home', since: joined_at, reason: why, signal: l.signal, lost: 0, join_failed_at: null });
			learn(cells());
			return true;
		}
	}
	if (keep) {
		log(`${home_ssid()} not joined in ${JOIN_WAIT_S}s: staying on it (always home)`);
		status({ mode: 'home', since: time(), reason: '家里 WiFi 还没连上', join_failed_at: time() });
		return false;
	}
	// (switched off by hand meanwhile: left off)
	if (wifi_state() == 'off') return false;
	log(`could not join ${home_ssid()} in ${JOIN_WAIT_S}s: hotspot back`);
	set_wifi(false);
	hotspot_back();
	last_look = time();
	status({ mode: 'away', since: time(), reason: '没能连上家里 WiFi', join_failed_at: time() });
	return false;
}

let heal_fails = 0;
function step() {
	let want = setting('mode', 'auto'), ws = wifi_state(), now = time(), asked = !!stat(REQ);
	// (the wall clock runs on in suspend, the loop does not: a long gap is a resume)
	let resumed = last_step && now - last_step > LOOP_S + 10;
	last_step = now;
	if (asked) unlink(REQ);
	if (!home_ssid()) { status({ mode: 'unset', state: ws }); return; }
	if (ws == 'off') { lost_since = null; status({ mode: 'off', state: ws }); return; }
	if (ws == 'both') {
		// the hotspot turned on by hand while at home: the user wants it
		go_away('hotspot turned on by hand at home', want == 'home' ? null : MANUAL_HOLD_S);
		return;
	}
	if (want == 'away') {
		if (ws != 'hotspot') go_away('forced away');
		else status({ mode: 'away', state: ws, forced: true, held_until: null });
		return;
	}
	if (ws == 'home') {
		let l = at_home_link();
		if (l) {
			lost_since = null;
			if (now - last_learn >= LEARN_S) { learn(cells()); last_learn = now; }
			status({ mode: 'home', state: ws, signal: l.signal, lost: 0, forced: want == 'home' });
			return;
		}
		// gone: counted in awake time only -- a resume starts the count afresh,
		// the link drops for a moment then
		if (lost_since == null || resumed) lost_since = now;
		let lost = now - lost_since;
		status({ mode: 'home', state: ws, signal: null, lost, forced: want == 'home' });
		if (want != 'home' && lost >= LOST_S) {
			let flap = now - joined_at < FLAP_S;
			go_away(`${home_ssid()} gone for ${lost}s`, flap ? FLAP_HOLD_S : null);
		}
		return;
	}
	if (want == 'home') {
		// (always home: on the home WiFi, whether or not it is there)
		go_home('forced home', null, true);
		return;
	}
	if (ws == 'none') { go_away('neither the hotspot nor the home WiFi was on'); return; }
	// away, the hotspot on
	if (!beaconing()) {
		// (configured on but not on the air, twice in a row: bring it up)
		if (++heal_fails >= 2) {
			log('hotspot configured on but not on the air: bringing it up');
			clear_stray();
			system(`wifi up ${RADIO} >/dev/null 2>&1`);
			hotspot_back();
			heal_fails = 0;
		}
		return;
	}
	heal_fails = 0;
	let seen = cells(), near = near_home(seen);
	if (near) last_near = now;
	else if (now - last_near > MISS_RESET_S) misses = 0;
	status({ mode: 'away', state: ws, near, cells_known: length(keys(load(CELLS, { cells: {} }).cells)),
		held_until: held_until > now ? held_until : null, misses });
	if (!asked) {
		if (held_until > now || !near) return;
		if (st.join_failed_at && now - st.join_failed_at < JOIN_FAIL_S) return;
		if (now - last_look < (misses >= 3 ? SCAN_MISS_S : SCAN_S)) return;
	}
	last_look = now;
	let sig = look();
	status({ last_look: now, last_signal: sig });
	if (sig != null && sig > -85) {
		misses = 0;
		held_until = 0;
		go_home(asked ? 'asked from the screen' : 'home WiFi found', seen, false);
	} else {
		misses++;
		system(`wifi up ${RADIO} >/dev/null 2>&1`);
		hotspot_back();
		log(`looked for ${home_ssid()}: ${sig == null ? 'not there' : sig + ' dBm, too weak'}`);
	}
}

// the lock screen's mark out of the screen's page, the screen restarted
function unpatch_index() {
	const INDEX = '/usr/share/e5-infoscreen/www/index.html';
	let src = readfile(INDEX), a = index(src ?? '', '<!-- BEGIN e5-homewifi');
	if (a < 0) return;
	let rest = substr(src, a), end = '<!-- END e5-homewifi -->\n', b = index(rest, end);
	if (b < 0) return;
	let tmp = INDEX + '.homewifi-new';
	if (writefile(tmp, substr(src, 0, a) + substr(rest, b + length(end))) != null && rename(tmp, INDEX))
		system("setsid sh -c 'sleep 2; /etc/init.d/e5-infoscreen restart' >/dev/null 2>&1 &");
}

function removed() {
	unpatch_index();
	// no stranding: the hotspot as the app found it, the client off
	if (wifi_state() != 'off' && wifi_state() != 'hotspot') { set_wifi(false); hotspot_back(); }
	unlink(STATE);
	log('app removed: stopping');
	// (the init script goes first: the service's delete ends this process)
	system(`/etc/init.d/e5-homewifi disable; rm -f /etc/init.d/e5-homewifi ${RUNNING}; ubus call service delete '{"name":"e5-homewifi"}'`);
	exit(0);
}

mkdir(DIR); mkdir('/tmp/run'); mkdir(RUN);
unlink(LOOKING);
// (a look cut short by a stop leaves the hotspot down: up again first thing)
if (clear_stray() || wifi_state() == 'hotspot' && !beaconing()) { system(`wifi up ${RADIO} >/dev/null 2>&1`); hotspot_back(); }
writefile(RUNNING, readfile(`${APP}/service.uc`) ?? '');
log('started');
let gone = 0;
while (true) {
	gone = stat(`${APP}/manifest.json`) ? 0 : gone + 1;
	if (gone >= GONE_LOOPS) removed();
	if (!gone) step();
	system(`sleep ${LOOP_S}`);
}
