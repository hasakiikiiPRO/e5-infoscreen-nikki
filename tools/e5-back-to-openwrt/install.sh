#!/bin/sh
# Puts "切回 OpenWrt" on the E5 from the PC, while it runs OpenWrt (USB LAN 192.168.9.1):
#  - the Magisk module into Android's /data/adb/modules (Magisk -> 模块: its 「操作」 button
#    switches to OpenWrt; after a fallback it tries OpenWrt once by itself at 25 %), with this
#    device's own boot-control blocks (the ones e5-next-boot writes);
#  - two lines in OpenWrt: e5-boot-ok marks "OpenWrt is the default and came up"
#    (/mnt/e5-data/e5linux/auto-return), e5-next-boot android takes the mark away -- a chosen
#    Android stays, a fallback does not.  Again after a firmware update (they are its files).
#
#   tools/e5-back-to-openwrt/install.sh [root@192.168.9.1]
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
E5=${1:-root@192.168.9.1}
SSH="ssh -i $HOME/.ssh/e5_rec -o IdentitiesOnly=yes -o UserKnownHostsFile=$HOME/.config/e5rec/known_hosts -o HostKeyAlias=e5rec-e5 -o LogLevel=ERROR -o ConnectTimeout=8 $E5"
M=/mnt/e5-data/adb/modules/e5_back_to_openwrt
tar -C "$HERE/module" -cf - . | $SSH "set -e
  rm -rf $M.new && mkdir -p $M.new && tar -C $M.new -xf -
  A=/run/e5linux/misc-bc-slot-a.bin; [ -f \$A ] || A=/etc/e5linux/misc-bc-slot-a.bin
  B=/run/e5linux/misc-bc-slot-b-trial.bin; [ -f \$B ] || B=/etc/e5linux/misc-bc-slot-b-trial.bin
  [ \$(wc -c < \$A) = 32 ] && [ \$(wc -c < \$B) = 32 ]
  cp \$A \$B $M.new/
  chown -R 0:0 $M.new; chmod 755 $M.new; chmod 644 $M.new/*; chmod 755 $M.new/*.sh
  rm -rf $M && mv $M.new $M
  # e5-boot-ok: the mark, every good boot with OpenWrt the default
  f=/etc/init.d/e5-boot-ok
  grep -q auto-return \$f || sed -i 's|^\(\t*\)/opt/e5/e5-next-boot --rearm 2>&1 \| logger -t e5-boot-ok\$|&\n\1# (Android back-to-openwrt module: after a fallback it tries OpenWrt once)\n\1[ \"\$(cat /etc/e5linux/default-boot 2>/dev/null)\" = linux ] \&\& touch /mnt/e5-data/e5linux/auto-return|' \$f
  grep -q 'auto-return' \$f
  # the linux branch (after flash.py --boot-openwrt) returns before it: mark there too
  grep -q 'e5-next-boot linux 2>&1 | logger -t e5-boot-ok; touch' \$f || sed -i 's|/opt/e5/e5-next-boot linux 2>&1 \| logger -t e5-boot-ok\$|& \&\& touch /mnt/e5-data/e5linux/auto-return|' \$f
  # e5-next-boot android: a chosen Android, no mark
  n=/opt/e5/e5-next-boot
  grep -q auto-return \$n || sed -i 's|^    android) set_default android; |    android) set_default android; rm -f /mnt/e5-data/e5linux/auto-return; |' \$n
  grep -q auto-return \$n
  [ \"\$(cat /etc/e5linux/default-boot 2>/dev/null)\" = linux ] && touch /mnt/e5-data/e5linux/auto-return
  sync
  echo installed; ls -la $M; grep -n auto-return \$f \$n; ls -la /mnt/e5-data/e5linux/auto-return"
