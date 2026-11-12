# 荣悦 E5 信息屏「代理」应用（Nikki）

给荣悦 E5 OpenWrt 的信息屏（机身上那块 320×480 小屏）用的应用。用它可以直接在设备上操作 [Nikki](https://github.com/nikkinikki-org/OpenWrt-nikki)（mihomo 透明代理），不用再打开电脑或手机浏览器。

## 功能

- **开关代理**：开启或关闭 Nikki。按钮显示 已开启 / 已关闭 / 启动中 / 停止中，要等透明代理真正接管流量后才显示"已开启"。
- **模式**：规则 / 全局 / 直连，切换立即生效，重启后保持。
  - 切到全局时，如果 `GLOBAL` 组选的是 `DIRECT`，会先自动把它指向主代理组。否则"全局"实际会让所有流量都走本地网络。
- **当前节点**：显示主分组最终用的节点（会顺着 节点选择 → 自动选择 → 具体节点 一路找下去）和上次测速结果。
- **切换节点**：订阅里所有可手动选择（Selector）的分组都会列出来，进去就能选节点、测延迟。
- **订阅**：列出全部订阅，显示正在用哪个、剩余流量、到期时间和上次更新时间；可以切换订阅，也可以更新订阅。
- 小屏上没有 emoji 字体，所以节点名前面的 🚀🇺🇸 这类图标在显示时会去掉，切换节点时用的仍是原来的完整名字。

添加新订阅请用 LuCI（服务 → Nikki → 配置文件）。在小屏上用按键输入长链接实在太难了。

## 操作

| 按键 | 作用 |
|---|---|
| ↑ / ↓ | 移动选择 |
| 确定 | 打开分组 / 选用节点 / 切换订阅 |
| ← / → | 在分组页里给选中的节点测延迟 |
| 返回 | 回到上一层 |

触摸也能用。

## 需要什么

- 荣悦 E5 上运行 [e5-linux](https://github.com/sprd-linux-devs/e5-linux) 的 OpenWrt，信息屏插件 API 版本 2。社区的 [e5-sleep 刷机包](https://github.com/CharlesGool/e5-openwrt-flasher) 也可以。
- 已经装好 Nikki，并配好至少一个订阅：

  ```sh
  wget -O - https://github.com/nikkinikki-org/OpenWrt-nikki/raw/refs/heads/main/install.sh | ash
  ```

- 国内网络下 mihomo 第一次启动时，可能因为从 GitHub 下载 GeoIP 数据库超时而失败。解决办法：把电脑上 Clash 客户端的 `Country.mmdb`（改名为 `geoip.metadb`）和 `geosite.dat` 拷到 `/etc/nikki/run/`。

## 安装

从 [Releases](../../releases) 下载 `nikki-infoscreen-*.tar.gz`，拷到设备上：

```sh
scp -O nikki-infoscreen-1.2.tar.gz root@192.168.9.1:/tmp/
ssh root@192.168.9.1 /usr/libexec/e5-infoscreen/plugin install /tmp/nikki-infoscreen-1.2.tar.gz
```

也可以自己打包：在仓库根目录运行 `tar czf nikki-infoscreen.tar.gz nikki`。

装好后，在信息屏的应用列表里打开「代理」。应用装在 `/etc/e5-infoscreen/plugins/nikki`，以后升级系统镜像也会保留。卸载用 `/usr/libexec/e5-infoscreen/plugin remove nikki`。

## 实现

- `backend.uc`：信息屏后端以 root 身份运行的 ucode 路由。它通过 mihomo 的外部控制接口（`127.0.0.1:9090`）读取和切换节点，用 uci 和 `/etc/init.d/nikki` 处理开关、订阅和模式。
  - 控制接口的密钥只在后端使用，通过 curl 的 `-K -`（标准输入）传进去，不会出现在进程列表里。页面上只能看到分组和节点名。
  - 分组名和节点名先跟 mihomo 当前的数据核对，确认存在后才会用到；传给 shell 的内容一律加了引号。
  - 开关和切换订阅时会记下重启前的 mihomo PID，等新进程起来、`inet nikki` 的 nft 表建好后，才算启动完成。启动或停止期间的重复操作会被拒绝。
- `index.html`：页面本身，使用信息屏 SDK（`/sdk/e5.js`）处理按键，并调用后端接口。
- `manifest.json`：应用的描述文件（API 版本 2）。

## 许可

MIT
