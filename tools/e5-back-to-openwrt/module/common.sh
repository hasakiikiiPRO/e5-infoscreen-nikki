# shared by action.sh and service.sh (Android, root, from Magisk)
MODDIR=${MODDIR:-/data/adb/modules/e5_back_to_openwrt}
D=/data/e5linux
MISC=/dev/block/by-name/misc
BC_A=$MODDIR/misc-bc-slot-a.bin        # what misc holds with Android booting (after a fallback too)
BC_B=$MODDIR/misc-bc-slot-b-trial.bin  # slot b armed for a trial boot: OpenWrt next
LOG=$D/back-to-openwrt.log

hex() { od -An -tx1 -v "$@" | tr -d ' \n'; }
misc_hex() { dd if=$MISC bs=1 skip=2048 count=32 2>/dev/null | hex; }
battery() { cat /sys/class/power_supply/battery/capacity 2>/dev/null || echo 0; }
log() { echo "$(date '+%F %T') $*" >> $LOG; }

# installed?  0 ok, otherwise prints why
check() {
  [ -f $D/openwrt.ext4 ] || [ "$(cat $D/boot-os 2>/dev/null)" = sd ] || { echo "没有找到 OpenWrt（/data/e5linux/openwrt.ext4）"; return 1; }
  [ "$(wc -c < $BC_A 2>/dev/null)" = 32 ] && [ "$(wc -c < $BC_B 2>/dev/null)" = 32 ] || { echo "模块缺少引导块文件"; return 1; }
  return 0
}

# arm slot b (OpenWrt next) -- only from the one known Android state, read back after writing,
# the old block put back if it does not read back.  0 armed (or already armed)
arm() {
  cur=$(misc_hex)
  [ "$cur" = "$(hex $BC_B)" ] || [ "$cur" = "$(hex $BC_A)" ] || { echo "引导块不是已知的安卓状态，不动它：$cur"; return 1; }
  # OpenWrt's e5-boot-ok makes it the default again once it is up
  echo linux > $D/openwrt-default-boot
  sync
  [ "$cur" = "$(hex $BC_B)" ] && return 0
  if dd if=$BC_B of=$MISC bs=1 seek=2048 count=32 conv=notrunc,fsync 2>/dev/null && [ "$(misc_hex)" = "$(hex $BC_B)" ]; then
    return 0
  fi
  dd if=$BC_A of=$MISC bs=1 seek=2048 count=32 conv=notrunc,fsync 2>/dev/null
  rm -f $D/openwrt-default-boot
  echo "写引导块失败，已恢复原状：$(misc_hex)"
  return 1
}
