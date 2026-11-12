# 荣悦 E5 信息屏应用：代理（Nikki）、流量、在家

给荣悦 E5 OpenWrt 的信息屏（机身上那块 320×480 小屏）用的两个应用，都可以直接在设备上操作，不用打开电脑或手机浏览器：

- **「代理」**（`nikki/`）：控制 [Nikki](https://github.com/nikkinikki-org/OpenWrt-nikki)（mihomo 透明代理）。
- **「流量」**（`dataplan/`）：自动发短信向运营商查询套餐，显示剩余流量（锁屏上也有），快用完时提醒，用完自动断开移动数据。
- **「在家」**（`homewifi/`）：能连上家里 WiFi 时，E5 关掉热点、改用家里网络（不耗 5G 流量，配合热点省电休眠）；家里 WiFi 断开后自动重新打开热点。

三个应用互不依赖，按需安装即可。

## 「代理」

### 功能

- **开关代理**：开启或关闭 Nikki。按钮显示 已开启 / 已关闭 / 启动中 / 停止中，要等透明代理真正接管流量后才显示"已开启"。
- **模式**：规则 / 全局 / 直连，切换立即生效，重启后保持。
  - 切到全局时，如果 `GLOBAL` 组选的是 `DIRECT`，会先自动把它指向主代理组。否则"全局"实际会让所有流量都走本地网络。
- **当前节点**：显示主分组最终用的节点（会顺着 节点选择 → 自动选择 → 具体节点 一路找下去）和上次测速结果。
- **切换节点**：订阅里所有可手动选择（Selector）的分组都会列出来，进去就能选节点、测延迟。
- **订阅**：列出全部订阅，显示正在用哪个、剩余流量、到期时间和上次更新时间；可以切换订阅，也可以更新订阅。
- 小屏上没有 emoji 字体，所以节点名前面的 🚀🇺🇸 这类图标在显示时会去掉，切换节点时用的仍是原来的完整名字。

添加新订阅请用 LuCI（服务 → Nikki → 配置文件）。在小屏上用按键输入长链接实在太难了。

### 操作

| 按键 | 作用 |
|---|---|
| ↑ / ↓ | 移动选择 |
| 确定 | 打开分组 / 选用节点 / 切换订阅 |
| ← / → | 在分组页里给选中的节点测延迟 |
| 返回 | 回到上一层 |

触摸也能用。

### 需要什么

- 已经装好 Nikki，并配好至少一个订阅：

  ```sh
  wget -O - https://github.com/nikkinikki-org/OpenWrt-nikki/raw/refs/heads/main/install.sh | ash
  ```

- 国内网络下 mihomo 第一次启动时，可能因为从 GitHub 下载 GeoIP 数据库超时而失败。解决办法：把电脑上 Clash 客户端的 `Country.mmdb`（改名为 `geoip.metadb`）和 `geosite.dat` 拷到 `/etc/nikki/run/`。
- 有的机场按 User-Agent 拦截：用 `clash.meta` 拉取会返回 Cloudflare 验证页或"访问受限"假节点。在 Nikki 里把订阅的 User-Agent 设成 `clash-verge/v2.4.2` 就能正常拉取。

### 实现

- `backend.uc`：信息屏后端以 root 身份运行的 ucode 路由。它通过 mihomo 的外部控制接口（`127.0.0.1:9090`）读取和切换节点，用 uci 和 `/etc/init.d/nikki` 处理开关、订阅和模式。
  - 控制接口的密钥只在后端使用，通过 curl 的 `-K -`（标准输入）传进去，不会出现在进程列表里。页面上只能看到分组和节点名。
  - 分组名和节点名先跟 mihomo 当前的数据核对，确认存在后才会用到；传给 shell 的内容一律加了引号。
  - 开关和切换订阅时会记下重启前的 mihomo PID，等新进程起来、`inet nikki` 的 nft 表建好后，才算启动完成。启动或停止期间的重复操作会被拒绝。
- `index.html`：页面本身，使用信息屏 SDK（`/sdk/e5.js`）处理按键，并调用后端接口。

## 「流量」

### 功能

- **向运营商查询**：定时（默认每 6 小时）发短信查询套餐，查询是免费的：电信发「108」到 10001，移动发「CXLL」到 10086，联通发「CXLL」到 10010。
  - 从回复中解析出通用流量的「共有 / 剩余」，再用它校准系统自带的流量统计（`unisoc.traffic`）：设置额度，修正本周期已用量。这样在手机上或 Android 下用掉的流量也会算进去。
  - 解析前会先去掉括号里的说明（例如"含上月结转 15.42GB"）；只讲定向流量的回复会被忽略；如果短信里总量、已用、剩余三个数都有，会检查它们能不能对上。
- **显示**：在应用里看剩余、已用、总量、今日用量、统计周期，以及运营商的回复原文。
- **锁屏**：锁屏上的用量框顶部多一行「套餐剩余 X GB · N%」和一根细进度条，快用完时变黄，用完时变红。
- **提醒**：剩余低于设定的百分比（5/10/20%）时，在信息屏上弹出通知。
- **用完自动断网**：用完后约 20 秒内断开移动数据，并亮屏通知；下个周期开始时自动接回。
  - 在应用里点「恢复上网」，本周期内就不再自动断网，以后可以随时重新开启。
  - 只撤销自己做过的断网。热点省电应用在热点关闭时用 `auto=0` 关掉的数据，它不会去碰。
- **出错时的处理**：
  - 查询失败后按 15 分钟、30 分钟、1 小时……逐步拉长间隔重试，不会连续发短信。
  - 新周期开始后等 3 小时再查，因为运营商月初的数据有延迟。
  - 系统时间没同步时不校准。
  - 读不到流量统计时什么都不改。

运营商的 "GB" 按 10⁹ 字节计算，这样屏幕、LuCI 和短信里的数字完全一致。如果运营商实际按 1024 计算，那么显示的剩余量会比实际少约 7%，断网只会提前，不会超额。

**目前只用中国电信的真实回复测试过。** 移动和联通的回复格式如果不一样，查询会失败，应用会显示原因，但不会改动任何设置。

设置（查询开关、查询间隔、提醒阈值、自动断网）在信息屏的「高级」里，「流量」这一项下面。

### 实现

- `service.uc` + `dataplan.init`：procd 后台服务，每 20 秒检查一次是否超额，到时间就发短信查询。
  - 短信通过信息屏自己的 `/api/sms-send` 和 `/api/sms` 接口收发。
  - 断网用 `ifdown`，恢复用 `ubus network.interface up`，与状态看板上的「移动数据」开关是同一个接口。
  - 应用卸载后，服务会从 procd 注销、删除自己的 init 脚本，并把锁屏改动还原。
- `backend.uc`：屏幕页面和通知用的接口。它负责安装后台服务；服务代码有变化时会重启服务；锁屏那一行在系统升级后被覆盖掉，也会重新加回来。这些检查每分钟一次，挂在信息屏的通知轮询上执行。
- `lock-card.html`：锁屏那一行，插在信息屏页面 `index.html` 里 `app.js` 之后，和「热点省电」插 CPU 卡片的方式相同。
- `index.html`：应用页面。

## 「在家」

### 功能

- **在家**（E5 连着家里 WiFi）：热点关闭，E5 通过家里宽带上网。热点关闭后，「热点省电」会自动断开 5G 数据，熄屏后让设备休眠。
  - **判断离家**：家里 WiFi **连续断开 60 秒**才算离家，然后重新打开热点。
  - 只按醒着的时间计算：设备刚从休眠中醒来时，WiFi 会短暂断开，这次断开不计入。
  - "连上"指的是已关联、并且 WWAN 已经拿到地址。只关联上但拿不到地址的，不算连上。
- **在外**（热点开着）：E5 的无线芯片不能同时做热点和连 WiFi，开着热点时扫描不到家里 WiFi。
  - 所以只有看到**家附近的基站**时，才每 2.5 分钟暂停热点 3～4 秒去扫描一次。连续 3 次没找到，就改为每 10 分钟一次。
  - 家附近的基站是在家时自动记录的，不需要手动设置。
  - **离家较远时完全不扫描**，热点不会受到任何影响。
  - 扫到家里 WiFi（信号强于 −85 dBm）就切换过去。40 秒内没连上，就恢复热点，10 分钟内不再尝试。
- **防止来回切换**：
  - 连上家里 WiFi 后 10 分钟内又断开，会暂停自动回家 30 分钟。
  - 在家时手动打开热点，同样暂停 30 分钟。
  - 手动关闭热点（关闭无线）时，自动切换全部暂停。
- **保证有网**：
  - 每次切换后，都会确认热点确实已经在发射信号，否则重试。
  - 热点在配置上是开着的、实际却不在线时，后台会自动修复。
  - 扫描中途服务被停止或者应用被卸载，都会把热点恢复。
- **锁屏**：网络状态那一行显示「在家 · 已连 <WiFi 名>」或「· 在外」，不会增加锁屏的高度。
- **模式**：自动 / 一直在家 / 一直在外，在应用里或「高级」设置里切换。

### 首次配置（一次性，用 SSH 执行）

家里 WiFi 的名称和密码只保存在设备的无线配置里，应用代码中不包含这些信息。

```sh
uci set network.wwan=interface; uci set network.wwan.proto=dhcp; uci set network.wwan.metric=20; uci commit network
uci add_list firewall.@zone[1].network=wwan; uci commit firewall          # @zone[1] 是 wan 区
uci set wireless.homewifi_sta=wifi-iface
uci set wireless.homewifi_sta.device=radio0; uci set wireless.homewifi_sta.mode=sta
uci set wireless.homewifi_sta.network=wwan;  uci set wireless.homewifi_sta.ifname=wlan0
uci set wireless.homewifi_sta.ssid='家里WiFi名'; uci set wireless.homewifi_sta.encryption=psk2
uci set wireless.homewifi_sta.key='家里WiFi密码'; uci set wireless.homewifi_sta.disabled=1
uci commit wireless
# 在家时保留短信和电话（热点省电默认的"关闭蜂窝"模式会让短信电话也收不到）：
uci set e5-plugin-hotspotdata.settings.mode=data; uci commit e5-plugin-hotspotdata
/etc/init.d/network reload; /etc/init.d/firewall reload
```

热点必须是 `wireless.default_radio0`（mode ap，ifname wlan0）。装好应用后，在家里点一次「立即检查家里 WiFi」：E5 会切到家里 WiFi，并开始记录家附近的基站。

### 实现

- `service.uc` + `homewifi.init`：procd 后台服务，每 15 秒执行一次状态机。
  - 切换方式：修改 uci 里两个 wifi-iface 的 `disabled`，然后依次执行 `wifi reload` 和 `wifi up radio0`。单独执行 `reload` 时，不会重新启动之前被 `wifi down` 停掉的无线芯片。
  - 扫描时先执行 `wifi down radio0`，再用 `iw` 扫描。比较 SSID 时会按照 `iw` 的转义规则处理，所以中文名称的 WiFi 也能匹配。
- `backend.uc`：负责状态查询、立即检查和切换模式；同时安装并维护后台服务（扫描进行中不会重启服务）。
- `lock-mark.html`：锁屏标识，插入到 `index.html` 中 `app.js` 之后；后台服务不在运行时不显示。
- `index.html`：应用页面。

## 共同

### 需要什么

荣悦 E5 上运行 [e5-linux](https://github.com/sprd-linux-devs/e5-linux) 的 OpenWrt，信息屏插件 API 版本 2。社区的 [e5-sleep 刷机包](https://github.com/CharlesGool/e5-openwrt-flasher) 也可以。

### 安装

从 [Releases](../../releases) 下载安装包，拷到设备上安装（以「流量」为例）：

```sh
scp -O dataplan-infoscreen-1.2.tar.gz root@192.168.9.1:/tmp/
ssh root@192.168.9.1 /usr/libexec/e5-infoscreen/plugin install /tmp/dataplan-infoscreen-1.2.tar.gz
```

也可以自己打包：在仓库根目录运行 `tar czf dataplan-infoscreen.tar.gz dataplan`（「代理」把 `dataplan` 换成 `nikki`）。

装好后在信息屏的应用列表里打开。应用装在 `/etc/e5-infoscreen/plugins/<id>`，升级系统镜像也会保留。卸载用 `/usr/libexec/e5-infoscreen/plugin remove <id>`。

两个应用的后端都以 root 运行（信息屏插件的通用规则）。装之前请先看过代码。

## 许可

MIT
