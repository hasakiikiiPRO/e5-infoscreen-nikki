# 切回 OpenWrt（安卓里不用电脑）

OpenWrt 连续两次没起来（最常见是**没电**），E5 的引导程序会自己退回安卓。以前只能接电脑跑
`flash.sh --boot-openwrt`；装了这个之后：

- **手动**：安卓里打开 **Magisk → 模块 →「切回 OpenWrt」→ 操作**，3 秒后重启进 OpenWrt。
  电量低于 10% 时会拒绝（起不来又会退回），先充电。
- **自动**：退回安卓后，充到 **25%** 自动再试 OpenWrt **一次**（会先发通知，再等 60 秒）。
  OpenWrt 每次正常开机都会重新允许下一次自动重试；再失败就停在安卓，不会来回重启。
  在 OpenWrt 里**主动选「进安卓」**则不会自动切回；想留在安卓，在 Magisk 里关掉本模块即可（等待中也立即生效）。

它只在引导块是已知的「安卓」状态时才写入「B 槽试启动」块（都是这台 E5 自己的，
从 OpenWrt 的 `/run/e5linux` 复制），写后读回核对，不一致就恢复原样。

## 安装（E5 在 OpenWrt、USB 连着电脑）

    tools/e5-back-to-openwrt/install.sh

它同时给 OpenWrt 加两行：`/etc/init.d/e5-boot-ok` 在正常开机时打标记
`/mnt/e5-data/e5linux/auto-return`，`/opt/e5/e5-next-boot android` 去掉标记。
这两个文件属于刷机包，**刷机/升级后要重新跑一次**。记录在 `/data/e5linux/back-to-openwrt.log`。
