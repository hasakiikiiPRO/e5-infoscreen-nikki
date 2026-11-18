# e5-dumbres：防止信息屏黑屏（背光亮着、画面全黑）

**症状**：E5 开机一两天后，信息屏背光亮着但画面全黑；`logread` 刷满 `DRM_IOCTL_MODE_CREATE_DUMB failed: Out of memory`，`dmesg` 是 `sprd_gem_dumb_create ... failed to allocate buffer with size 614400/626688`。

**根因（2026-11-18 实测）**：这台 E5 的内核**没开 CMA、没开内存压缩**（`CONFIG_CMA`、`CONFIG_COMPACTION`、`CONFIG_MIGRATION` 都没设）。屏幕缓冲区（约 600 KB）只能直接从普通内存里拿一整块空闲的 1 MB（order-8）；开机久了、程序反复启停后，物理内存碎成小块（`/proc/buddyinfo` 第 9 列为 0），内核又不能整理。总内存还剩几百 MB 也没用。屏幕程序（cage）**启动时一次要 3 块**，少一块就「Swapchain failed test」退出、被 procd 反复拉起，一直黑屏。清文件缓存（`drop_caches`）只能拼出 512 KB 的块，不够。

**修法**：
- `e5-dumbres`（常驻，`/etc/init.d/e5-dumbres`，START=19 早于屏幕）：开机时内存还完整，先占住 6 块屏幕大小的显存（DRM dumb buffer，什么也不画）；盯着内核日志，一出现屏幕申请失败就**全部放出来**，屏幕程序重试时就能拿到；之后每分钟在内存允许时再补回来。实测：屏幕启动失败 → 它立刻放出 → 屏幕起来。
- **坑：DRM 主控**。第一个打开 `/dev/dri/card0` 的程序自动成为显示主控（DRM master）；`e5-dumbres` 开机比屏幕早启动，不放掉主控的话屏幕程序设不了显示，`Swapchain for output 'DSI-1' failed test` 无限重启——和缺显存的症状一样，但内核日志里没有 `failed to allocate buffer`。所以它打开设备后立刻 `DRM_IOCTL_DROP_MASTER`（建缓冲区不需要主控）。查主控：`grep -E ' y +y' /sys/kernel/debug/dri/0/clients` 应该只有 seatd 和 cage。
- `dumbprobe` 会一直申请到失败为止，这个失败也会让 `e5-dumbres` 以为屏幕要用而全部让出（60 秒后自己补回），量完不用管。
- 同一个服务启动时把 `vm.min_free_kbytes` 从 16 MB 调到 64 MB：内核更早回收缓存，整块空闲内存保留得更久。
- `dumbprobe`：量「现在还能给屏幕几块缓冲区」（申请到失败为止再全部释放），0 表示此刻重启屏幕就会黑。

**安装**（静态 musl，不依赖系统库）：
```sh
python3 -m ziglang cc -target aarch64-linux-musl -O2 -s -static -o e5-dumbres e5-dumbres.c
scp e5-dumbres root@192.168.9.1:/usr/sbin/ && scp dumbprobe root@192.168.9.1:/usr/sbin/e5-dumbprobe && scp e5-dumbres.init root@192.168.9.1:/etc/init.d/e5-dumbres
ssh root@192.168.9.1 'chmod 755 /usr/sbin/e5-dumbres /etc/init.d/e5-dumbres; /etc/init.d/e5-dumbres enable; /etc/init.d/e5-dumbres start'
```
**已经黑屏时的急救**：停掉屏幕和录音服务、清缓存，再启动屏幕（`/etc/init.d/e5-infoscreen stop; /etc/init.d/e5-recorder stop; sync; echo 3 > /proc/sys/vm/drop_caches; /etc/init.d/e5-infoscreen start; /etc/init.d/e5-recorder start`），不行就重启 E5。
