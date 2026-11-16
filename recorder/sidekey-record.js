// (e5-recorder) the side key held: recording on or off -- one pulse to start,
// two to stop; the lock screen's red dot shows it too
function sidekeyRecord(m) {
	fetch('/api/plugins/recorder/status', { cache: 'no-store' })
		.then((r) => r.ok ? r.json() : Promise.reject(new Error('录音应用不在')))
		.then((s) => {
			const on = !s.enabled;
			return fetch('/api/plugins/recorder/record', { method: 'POST', cache: 'no-store',
				headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }) })
				.then((r) => r.ok ? on : Promise.reject(new Error('HTTP ' + r.status)));
		})
		.then((on) => {
			if (m.vibrate > 0) {
				post('vibrate', { ms: m.vibrate });
				if (!on) setTimeout(() => post('vibrate', { ms: m.vibrate }), m.vibrate + 180);
			}
			if (!blank) { resetIdle(); toast(on ? '开始录音' : '已停止录音'); }
		}, (e) => { if (!blank) toast('录音：' + (e.message || e)); });
}
