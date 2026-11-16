#!/bin/sh
# Broadnet (card 1) self-test, run entirely on the E5 (the computer stays on
# another network): set card 1's NR band lock, switch to it, put real load on
# it, and go back to card 0 (Telecom) by itself on a baseband assert or no
# internet.  Passing, it stays on card 1.
#   sim-band-test.sh [NR bands, default 41] [load seconds, default 180]
# Results: /tmp/gd3.log (steps), /tmp/gd3.result (one line)
NR=${1:-41}; LOAD=${2:-180}
O=/tmp/gd3.log; R=/tmp/gd3.result; W=/tmp/gd3.asserts; B=/tmp/gd3.bytes; STOP=/tmp/gd3.stop
L=/usr/libexec/unisoc-modem/lock-control
: > $O; : > $W; : > $B; rm -f $STOP; echo running > $R
log() { echo "$(date +%T) $*" >> $O; }
helpers() { sleep 8; /etc/init.d/t551m-at restart; /etc/init.d/t551m-sim-status restart; } >/dev/null 2>&1
stop_load() { touch $STOP; killall curl 2>/dev/null; }
back() {
	stop_load
	log "FAIL: $1 -> back to Telecom"
	[ "$(cat /sys/module/sipc_wwan/parameters/card)" = 0 ] || { e5-sim 0 >> $O 2>&1; helpers; }
	echo "FAIL: $1" > $R
	log "on card $(cat /sys/module/sipc_wwan/parameters/card); asserts during the test: $(( $(wc -l < $W) - A0 ))"
	kill $WATCH $DOG 2>/dev/null; exit 0
}
# every baseband assert from now on, as it happens (the log itself rolls over)
logread -f -e "Modem Assert" > $W 2>/dev/null & WATCH=$!
# whatever happens, no more than 10 minutes away from Telecom
sleep 2; A0=$(wc -l < $W)   # (-f first prints the old ones still in the log)
( sleep 600; [ "$(cat $R)" = running ] && { touch $STOP; killall curl; e5-sim 0; echo "FAIL: watchdog" > $R; } ) >/dev/null 2>&1 & DOG=$!
log "start: NR lock for card 1 -> n$NR, then ${LOAD}s of load"

out=$(MU300_SIM_SLOT=1 timeout 170 $L apply nr "$NR" 2>&1); log "lock-control: $out"
case "$out" in *'"ok": 1'*) ;; *) back "band lock write failed";; esac
want="NR $(echo $NR | sed 's/^/n/; s/,/ n/g')"
n=0; while :; do
	line=$(e5-sim 2>/dev/null | grep '^card 1')
	case "$line" in *"$want") break;; esac
	n=$((n+1)); [ $n -ge 8 ] && back "card 1 band lock reads: $line"
	sleep 5
done
log "verified: $line"
sleep 15   # (the lock's radio restart settling, on Telecom)
A=$(wc -l < $W); log "asserts while writing the lock: $((A - A0))"

log "switching to card 1"
e5-sim 1 >> $O 2>&1; helpers &
ok=0; i=0
while [ $i -lt 20 ]; do
	sleep 5; i=$((i+1))
	[ "$(wc -l < $W)" -gt $A ] && back "baseband assert while connecting"
	ping -c1 -W3 223.5.5.5 >/dev/null 2>&1 && { ok=1; break; }
done
[ $ok = 1 ] || back "no internet within 100 s"
log "online after $((i*5)) s: $(mmcli -m any | grep -E 'access tech|signal quality' | tr -s ' ' | tr '\n' ' ')"

load() { while [ ! -f $STOP ]; do curl -sL -o /dev/null -m 60 -w '%{size_download}\n' "$1" >> $B 2>/dev/null || sleep 1; done; }
load https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso &
load https://mirrors.ustc.edu.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso &
load http://cachefly.cachefly.net/100mb.test &
load http://cachefly.cachefly.net/100mb.test &
log "load: 2 domestic + 2 overseas downloads"
t=0; f=0
while [ $t -lt $LOAD ]; do
	sleep 5; t=$((t+5))
	[ "$(wc -l < $W)" -gt $A ] && back "baseband assert after ${t}s of load"
	if ping -c1 -W3 223.5.5.5 >/dev/null 2>&1; then f=0; else f=$((f+1)); fi
	[ $f -ge 4 ] && back "internet gone under load (${t}s)"
	[ $((t % 30)) = 0 ] && log "load ${t}s ok: $(mmcli -m any | grep 'signal quality' | tr -s ' ')"
done
stop_load; sleep 2
mb=$(awk '{s+=$1} END {printf "%d", s/1048576}' $B)
log "PASS: ${LOAD}s of load, ${mb} MB, about $((mb*8/LOAD)) Mbps average; asserts: $(( $(wc -l < $W) - A ))"
log "$(mmcli -m any | grep -E 'access tech|signal quality|operator name' | tr -s ' ' | tr '\n' ' ')"
echo "PASS: stays on Broadnet (n$NR), ${mb} MB in ${LOAD}s" > $R
kill $WATCH $DOG 2>/dev/null
