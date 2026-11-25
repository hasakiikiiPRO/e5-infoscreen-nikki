#!/system/bin/sh
# Back in Android though OpenWrt is the default (it fell back: two boots that did not come up,
# a flat battery most often): once charged enough, try OpenWrt again -- once.  OpenWrt's
# e5-boot-ok puts $D/auto-return back on every good boot; its "boot Android" takes it away,
# so a chosen Android stays.  Turning this module off in Magisk stops it, even mid-wait.
MODDIR=${0%/*}
# (written from OpenWrt, the files have no SELinux label: as the other modules have)
chcon -R u:object_r:system_file:s0 $MODDIR 2>/dev/null
. $MODDIR/common.sh
MIN=25
[ -f $D/auto-return ] || exit 0
check >/dev/null || exit 0
[ "$(misc_hex)" = "$(hex $BC_A)" ] || exit 0
until [ "$(getprop sys.boot_completed)" = 1 ]; do sleep 5; done
log "service: back in Android with OpenWrt the default; waiting for ${MIN}% (now $(battery)%)"
notify() { cmd notification post -S bigtext -t "切回 OpenWrt" e5back "$1" >/dev/null 2>&1; }
said=
while [ "$(battery)" -lt $MIN ]; do
  [ -f $MODDIR/disable ] && { log "service: module turned off, stays in Android"; exit 0; }
  [ -n "$said" ] || { notify "OpenWrt 没起来（多半是没电），已退回安卓。充到 ${MIN}% 会自动切回。要留在安卓：Magisk → 模块 → 关闭「切回 OpenWrt」。"; said=1; }
  sleep 60
done
notify "电量 $(battery)%，60 秒后自动切回 OpenWrt。要留在安卓：Magisk → 模块 → 关闭「切回 OpenWrt」。"
sleep 60
[ -f $MODDIR/disable ] && { log "service: module turned off, stays in Android"; exit 0; }
[ -f $D/auto-return ] || exit 0
# one try: if OpenWrt fails again it falls back, and without the mark this stays put
rm -f $D/auto-return
if why=$(arm); then
  log "service: armed, rebooting (battery $(battery)%)"
  sync; reboot
else
  log "service: $why"
  notify "没能切回 OpenWrt：$why"
fi
