#!/system/bin/sh
# Magisk -> 模块 -> 「切回 OpenWrt」-> 操作
MODDIR=${0%/*}
# (written from OpenWrt, the files have no SELinux label: as the other modules have)
chcon -R u:object_r:system_file:s0 $MODDIR 2>/dev/null
. $MODDIR/common.sh
echo "电量 $(battery)%"
if ! why=$(check); then echo "$why"; exit 1; fi
if [ "$(battery)" -lt 10 ]; then
  echo "电量太低（低于 10%），OpenWrt 起不来又会退回安卓。先充一会儿电再按。"
  exit 1
fi
if ! why=$(arm); then echo "$why"; log "action: $why"; exit 1; fi
log "action: armed, rebooting (battery $(battery)%)"
echo "已设置，3 秒后重启进 OpenWrt…"
sleep 3
reboot
