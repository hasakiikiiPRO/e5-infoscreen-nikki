'use strict';
// 代理 (Nikki): on/off through nikki's uci switch, the node of each Selector
// group through mihomo's external controller.  The controller's secret stays
// here; the page only ever sees group and node names.
const DELAY_URL = 'https://www.gstatic.com/generate_204';
// a switch is "starting" until mihomo answers or this many seconds pass
const PENDING_S = 90;

return function(ctx) {
	function fail(msg, code) {
		return { status: code ?? 502, type: 'application/json', body: sprintf('%J', { ok: false, error: msg }) };
	}
	// (single-quoted for the shell: the text is data)
	function q(s) {
		return "'" + replace(`${s}`, /'/g, "'\\''") + "'";
	}
	function enc(s) {
		let out = '';
		for (let i = 0; i < length(s); i++) {
			let b = ord(s, i), c = substr(s, i, 1);
			out += match(c, /^[A-Za-z0-9._~-]$/) ? c : sprintf('%%%02X', b);
		}
		return out;
	}
	function uci_get(sec, opt) {
		return ctx.uci().get('nikki', sec, opt);
	}
	function base() {
		let m = match(uci_get('mixin', 'api_listen') ?? '', /:([0-9]+)$/);
		return `http://127.0.0.1:${m ? m[1] : '9090'}`;
	}
	// curl with the controller's secret fed on stdin (a curl config line), so
	// it never shows on a command line in ps; printf is the shell's builtin
	function curl(args) {
		let secret = replace(uci_get('mixin', 'api_secret') ?? '', /["\\]/g, m => '\\' + m);
		return `printf '%s\\n' ${q('header = "Authorization: Bearer ' + secret + '"')} | curl -K - ${args}`;
	}
	// { code, data }: code 0 when the controller does not answer at all
	function api(path, timeout) {
		let out = ctx.sh(curl(`-s -m ${timeout ?? 4} -w '\\n%{http_code}' ${q(base() + path)} 2>/dev/null`)) ?? '';
		// (the status code is the last line: -w puts it after the body)
		let i = rindex(out, '\n'), code = (i < 0) ? '' : substr(out, i + 1), data = null;
		if (!match(code, /^[0-9][0-9][0-9]$/)) return { code: 0, data };
		try { data = json(substr(out, 0, i)); } catch (e) {}
		return { code: +code, data };
	}
	function api_get(path, timeout) {
		let r = api(path, timeout);
		return r.code == 200 ? r.data : null;
	}
	function mihomo_pid() {
		return trim(ctx.sh('pidof mihomo 2>/dev/null') ?? '');
	}
	function running() {
		return mihomo_pid() != '';
	}
	// the transparent proxy takes over a few seconds after mihomo answers:
	// nikki's own check is the same, its nft table
	function hijacked() {
		return uci_get('proxy', 'enabled') != '1' || ctx.run('nft list table inet nikki >/dev/null 2>&1') == 0;
	}
	function profile_name() {
		let p = uci_get('config', 'profile') ?? '', m = match(p, /^subscription:(.+)$/);
		if (m) return uci_get(m[1], 'name') ?? m[1];
		m = match(p, /^file:(.+)$/);
		return m ? m[1] : p;
	}
	// a group's node as far down as it goes: 节点选择 -> 美国家宽
	function leaf(proxies, name) {
		let seen = {};
		while (proxies[name]?.now && !seen[name]) {
			seen[name] = true;
			name = proxies[name].now;
		}
		return name;
	}
	function last_delay(p) {
		let h = p?.history;
		return (type(h) == 'array' && length(h)) ? h[length(h) - 1].delay : null;
	}
	function selectors(proxies, mode) {
		let order = proxies.GLOBAL?.all ?? keys(proxies), out = [];
		// in global mode everything goes by GLOBAL's choice: it leads the list
		if (mode == 'global' && proxies.GLOBAL)
			push(out, { name: 'GLOBAL', now: proxies.GLOBAL.now, leaf: leaf(proxies, 'GLOBAL'), count: length(proxies.GLOBAL.all ?? []) });
		for (let n in order) {
			let p = proxies[n];
			if (p?.type == 'Selector' && n != 'GLOBAL')
				push(out, { name: n, now: p.now, leaf: leaf(proxies, n), count: length(p.all ?? []) });
		}
		return out;
	}
	function state() {
		let enabled = uci_get('config', 'enabled') == '1', up = running();
		let pending = ctx.state_get('pending', PENDING_S);
		let switching_on = !!(pending && pending.on), switching_off = !!(pending && !pending.on);
		// a restart over a running core: the old mihomo (and its nft table) is
		// still there for a moment -- only a new one counts as started
		let old_core = switching_on && pending.pid && pending.pid == mihomo_pid();
		let r = { ok: true, enabled, running: up, profile: profile_name(),
			starting: switching_on && (!up || old_core), stopping: switching_off && up };
		if (old_core) return r;
		if (!up) return r;
		let a = api('/proxies'), px = a.data?.proxies;
		if (a.code != 200 || type(px) != 'object') {
			// a start in progress, else a controller that is really not right
			if (switching_on) r.starting = true;
			else if (!switching_off)
				r.api_error = a.code ? `控制接口返回 ${a.code}` : '控制接口没有响应';
			return r;
		}
		let ready = hijacked();
		r.starting = switching_on && !ready;
		if (switching_on && ready) ctx.forget('pending');
		if (!switching_on && !switching_off && enabled && !ready) r.api_error = '透明代理没有接管';
		r.mode = api_get('/configs')?.mode;
		r.groups = selectors(px, r.mode);
		let main = r.groups[0];
		if (main) r.main = { group: main.name, node: main.leaf, delay: last_delay(px[main.leaf]) };
		return r;
	}
	function group_of(px, name) {
		let g = px?.[name];
		return (g?.type == 'Selector') ? g : null;
	}
	function put_select(group, node) {
		return trim(ctx.sh(curl(`-s -m 5 -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' ` +
			`--data ${q(sprintf('%J', { name: node }))} ${q(base() + '/proxies/' + enc(group))} 2>/dev/null`)) ?? '');
	}
	// the restart that applies a switch, a profile change: one at a time
	function restart(on) {
		ctx.state_put('pending', { on, pid: mihomo_pid() });
		ctx.run('setsid /etc/init.d/nikki restart >/dev/null 2>&1 &');
	}
	function subs() {
		let c = ctx.uci(), list = [];
		let cur = match(c.get('nikki', 'config', 'profile') ?? '', /^subscription:(.+)$/);
		c.foreach('nikki', 'subscription', (s) => {
			push(list, { id: s['.name'], name: s.name ?? s['.name'], current: !!(cur && cur[1] == s['.name']),
				expire: s.expire, used: s.used, total: s.total, left: s.avaliable, updated: s.update,
				ok: s.success == '1' });
		});
		return list;
	}
	function sub_exists(id) {
		return type(id) == 'string' && match(id, /^[A-Za-z0-9_]+$/) && ctx.uci().get('nikki', id) == 'subscription';
	}

	return {
		'GET /status': function(req) { return state(); },
		'GET /group': function(req) {
			let name = req.query?.name;
			let px = api_get('/proxies')?.proxies;
			if (type(px) != 'object') return fail('代理没有运行', 503);
			let g = group_of(px, name);
			if (!g) return fail('没有这个分组', 404);
			return { ok: true, name, now: g.now,
				nodes: map(g.all ?? [], n => ({ name: n, type: px[n]?.type, leaf: leaf(px, n), delay: last_delay(px[leaf(px, n)]) })) };
		},
		'POST /select': function(req) {
			let name = req.body?.group, node = req.body?.node;
			if (type(name) != 'string' || type(node) != 'string') return fail('参数不对', 400);
			let px = api_get('/proxies')?.proxies;
			if (type(px) != 'object') return fail('代理没有运行', 503);
			let g = group_of(px, name);
			if (!g) return fail('没有这个分组', 404);
			if (index(g.all ?? [], node) < 0) return fail('分组里没有这个节点', 400);
			let code = put_select(name, node);
			if (code != '204') return fail(`切换失败（${code || '无响应'}）`);
			ctx.log(`select ${name} -> ${node}`);
			return { ok: true };
		},
		'POST /delay': function(req) {
			let node = req.body?.node;
			if (type(node) != 'string') return fail('参数不对', 400);
			let px = api_get('/proxies')?.proxies;
			if (type(px) != 'object') return fail('代理没有运行', 503);
			if (!px[node]) return fail('没有这个节点', 404);
			let r = api_get(`/proxies/${enc(node)}/delay?timeout=5000&url=${enc(DELAY_URL)}`, 8);
			return (type(r?.delay) == 'int') ? { ok: true, delay: r.delay } : { ok: true, delay: 0 };
		},
		'POST /power': function(req) {
			let on = req.body?.on;
			if (type(on) != 'bool') return fail('参数不对', 400);
			// one switch at a time: two restarts at once race procd and the nft setup
			let s = state();
			if (s.starting || s.stopping) return fail('上一次开关还没完成，请稍等', 409);
			let c = ctx.uci();
			c.set('nikki', 'config', 'enabled', on ? '1' : '0');
			if (!c.commit('nikki')) return fail('保存设置失败', 500);
			ctx.log(on ? 'proxy on' : 'proxy off');
			// (restart takes seconds: the subscription check and the profile test)
			restart(on);
			return { ok: true };
		},
		'POST /mode': function(req) {
			let mode = req.body?.mode;
			if (index([ 'rule', 'global', 'direct' ], mode) < 0) return fail('参数不对', 400);
			let px = api_get('/proxies')?.proxies;
			if (type(px) != 'object') return fail('代理没有运行', 503);
			// global follows GLOBAL's choice: one on DIRECT would send everything out
			// through the local line -- point it at the main proxy group first
			if (mode == 'global' && index([ 'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS' ], leaf(px, 'GLOBAL')) >= 0) {
				let main = selectors(px, 'rule')[0];
				if (!main || put_select('GLOBAL', main.name) != '204') return fail('全局模式没有可用的代理分组', 502);
			}
			let code = trim(ctx.sh(curl(`-s -m 5 -o /dev/null -w '%{http_code}' -X PATCH -H 'Content-Type: application/json' ` +
				`--data ${q(sprintf('%J', { mode }))} ${q(base() + '/configs')} 2>/dev/null`)) ?? '');
			if (code != '204') return fail(`切换模式失败（${code || '无响应'}）`);
			// and kept for the next start (nikki writes mixin's mode into the profile)
			let c = ctx.uci();
			c.set('nikki', 'mixin', 'mode', mode);
			c.commit('nikki');
			ctx.log(`mode ${mode}`);
			return { ok: true };
		},
		'GET /subs': function(req) {
			return { ok: true, subs: subs(), updating: ctx.state_get('updating', 90)?.id };
		},
		'POST /sub': function(req) {
			let id = req.body?.id;
			if (!sub_exists(id)) return fail('没有这个订阅', 404);
			let s = state();
			if (s.starting || s.stopping) return fail('上一次开关还没完成，请稍等', 409);
			let c = ctx.uci();
			c.set('nikki', 'config', 'profile', 'subscription:' + id);
			if (!c.commit('nikki')) return fail('保存设置失败', 500);
			ctx.log(`profile ${id}`);
			if (s.enabled) restart(true);
			return { ok: true, restarting: s.enabled };
		},
		'POST /sub-update': function(req) {
			let id = req.body?.id;
			if (!sub_exists(id)) return fail('没有这个订阅', 404);
			if (ctx.state_get('updating', 90)) return fail('正在更新另一个订阅，请稍等', 409);
			let s = state();
			let current = !!filter(subs(), x => x.id == id && x.current)[0];
			// the one in use is applied by the restart (it downloads it first:
			// prefer remote); another is only downloaded
			if (current && s.enabled) {
				if (s.starting || s.stopping) return fail('上一次开关还没完成，请稍等', 409);
				restart(true);
				return { ok: true, restarting: true };
			}
			ctx.state_put('updating', { id });
			ctx.run(`setsid sh -c ${q(`/etc/init.d/nikki update_subscription ${id}; rm -f /tmp/run/e5-infoscreen/api/plugin-nikki-updating.json`)} >/dev/null 2>&1 &`);
			return { ok: true, restarting: false };
		}
	};
};
