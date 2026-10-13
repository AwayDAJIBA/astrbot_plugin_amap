# astrbot_plugin_amap —— 高德地图能力 + 手机按需定位

把**高德开放平台 Web 服务 API** 接进 AstrBot，配一个手机端 App，让 agent 能查地图、查天气、查手机在哪。

手机**不常驻定位、不后台耗电**：只有当 agent 真的调用「查手机位置」的工具时，插件才向手机下发一次定位请求，
手机现场取一次 GPS 回传，插件自动做坐标转换与逆地理编码，再交给模型总结。

```
   agent 调工具                 手机 App 轮询               手机取 GPS           插件回调高德
locate_phone()  ──►  生成定位请求  ──►  GET /amap/poll  ──►  LocationManager ──►  POST /amap/report
                                                                                      │
       ◄──────────── 逆地理编码（坐标→中文地址）+ 天气 ◄──── WGS-84 自动转 GCJ-02 ◄────┘
```

> 动手之前先扫一眼文末的「合规与免责声明」：本插件**不附带任何 API Key**，需要你自己去高德申请；
> 项目代码**主要由 AI 生成、自用为主，作者不承诺维护**。看完还愿意用，那就往下翻。

---

## 一、目录结构

```
astrbot_plugin_amap/
├── metadata.yaml            插件元信息
├── _conf_schema.json        WebUI 里能看到的配置项定义
├── requirements.txt         运行时依赖（其实只有 aiohttp，AstrBot 自带）
├── main.py                  Star 入口：14 个 agent 工具 + 5 个聊天命令
├── amap/
│   ├── config.py            配置解析与默认值
│   ├── coord.py             坐标转换（WGS-84 / GCJ-02 / BD-09，纯本地算法）
│   ├── client.py            高德 Web 服务异步客户端（含签名、重试、TTL 缓存、错误归一化）
│   ├── formatter.py         高德响应 → 喂给 LLM 的短文本（丢掉 polyline 这类 token 炸弹）
│   ├── store.py             设备表 + 位置历史（落盘，重启不丢）
│   ├── locator.py           按需定位：下发请求 → 等手机回传
│   └── server.py            局域网 HTTP 服务（手机 App 的对接面）
├── android/                 手机端 App（Kotlin，独立工程，附自己的 README）
│   └── app/build/outputs/apk/debug/app-debug.apk   已构建好的安装包
└── tests/
    ├── selftest.py          离线全链路自测（128 项，不需要联网、不需要 key）
    └── probe_real_amap.py   用假 key 打一次真高德，验证错误链路
```

---

## 二、快速开始

1. 确认整个 `astrbot_plugin_amap` 目录在 `AstrBot/data/plugins/` 下，**重启 AstrBot**（新插件必须重启才会被扫描到）。
2. 启动日志里会看到：`amap: HTTP 服务已启动 http://0.0.0.0:6310（手机请用主机内网 IP 访问）`。
3. 到 WebUI → 插件 → `astrbot_plugin_amap` 配置里填 `amap_key`，然后重载插件。
4. 手机装 `android/app/build/outputs/apk/debug/app-debug.apk`，填主机内网 IP（Windows 上 `ipconfig` 看 WiFi 网卡的 IPv4），点「测试连接」「启动服务」。
5. 在聊天里对它说「我现在在哪？」——它会调 `where_is_phone` 走完整条链路。

---

## 三、高德 key 怎么申请（这一步最容易踩坑）

高德开放平台 → 应用管理 → 创建新应用 → 添加 Key。

**服务平台必须选「Web服务」**——不是「Web端(JS API)」，也不是 Android/iOS。
选错了会返回 `10009 USERKEY_PLAT_NOMATCH`，而且**Key 的服务平台选定后不可修改，只能重建**。

拿到 key 填进 `amap_key` 即可，一分钟生效。填完可以用聊天命令 `/amap_key` 确认。

> 没填 key 也不影响手机定位那条链路：坐标照样能取到、能存、能看历史，只是没法解析成中文地址、没法查天气。

---

## 四、agent 能用的 14 个工具

### 设备与定位

| 工具 | 干什么 |
|---|---|
| `list_phone_devices()` | 有哪些手机连过主机、在不在线、最近一次位置是什么时候 |
| `locate_phone(device_id, timeout, force, with_address)` | **按需拉取**手机实时位置，并解析成中文地址 |
| `where_is_phone(device_id, with_weather, timeout)` | 一步到位：位置 + 地址 + 当地天气（"我在哪/我这边天气如何"用这个） |
| `phone_location_history(device_id, limit)` | 位置历史 |

### 高德地图

| 工具 | 对应高德接口 |
|---|---|
| `amap_geocode(address, city)` | `/v3/geocode/geo` 地址 → 坐标 |
| `amap_reverse_geocode(location)` | `/v3/geocode/regeo` 坐标 → 地址（含周边 POI） |
| `amap_weather(city, forecast)` | `/v3/weather/weatherInfo` 实况 + 4 天预报 |
| `amap_search_place(keywords, city, near, radius, page)` | `/v3/place/text`、`/v3/place/around` 找餐厅/加油站/医院… |
| `amap_route(origin, destination, mode, city)` | 驾车 / 步行 / 骑行 / 公交 路径规划 |
| `amap_measure_distance(origins, destination, measure_type)` | `/v3/distance` 直线或驾车距离 |
| `amap_ip_location(ip)` | `/v3/ip` IP 归属地 |
| `amap_district(keywords, subdistrict)` | `/v3/config/district` 行政区划与 adcode |
| `amap_static_map(center, zoom, markers)` | `/v3/staticmap` 静态地图，存成图片 |

外加一个自检工具 `amap_status()`：key 配没配、服务通不通、有哪些设备、调用统计，排查问题先调它。

---

## 五、聊天命令

| 命令 | 作用 |
|---|---|
| `/amap_status` | 插件状态：监听地址、key 状态、设备列表、待处理请求数 |
| `/amap_loc [device_id]` | 手动拉一次手机位置 |
| `/amap_where [device_id]` | 位置 + 当地天气 |
| `/amap_map <地址或坐标> [缩放]` | 生成静态地图并**直接把图片发出来** |
| `/amap_key` | 快速检查 key 配没配 |

---

## 六、手机 App

源码在 `android/`，构建好的安装包在 `android/app/build/outputs/apk/debug/app-debug.apk`（约 5.9 MB，debug 签名，`adb install -r` 可装）。

App 里要填三样：**主机内网 IP、端口（默认 6310）、可选令牌**（跟插件配置里的 `auth_token` 一致）。
然后点「启动服务」，会挂一个常驻通知——这是前台服务，用来防止系统把轮询杀掉。

App 的行为：每 15 秒（可调）`GET /amap/poll` 问一句「有没有活干」；没有就空转，有的就去取一次 GPS 再上报。
**全程不获取后台定位**，只在你或 agent 要位置的时候才动手。取到的坐标是系统原始 WGS-84，转换交给插件做。

> 国产 ROM 需要额外给「自启动 + 允许后台运行 + 无限制省电」，否则锁屏一段时间后轮询会停。
> 详细说明（权限、release 打包、9 条 FAQ）见 [android/README.md](android/README.md)。

---

## 七、HTTP 接口（想自己写客户端的话）

基址 `http://<主机内网IP>:6310`，明文 HTTP，仅局域网。配了 `auth_token` 时每个请求都要带 Header `X-Amap-Token`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/amap/ping?device_id=&name=` | 存活探测 + 设备心跳，返回建议轮询间隔 |
| GET | `/amap/poll?device_id=` | 取定位请求：`{"ok":true,"request":{...} 或 null}` |
| POST | `/amap/report` | 上报坐标（见下） |
| GET | `/amap/last?device_id=` | 最近一次已知位置 |
| GET | `/amap/history?device_id=&limit=` | 位置历史 |
| GET | `/amap/health` | 服务自检 |
| GET | `/` | 一行说明页，浏览器打开能确认连通 |

`/amap/poll` 返回的 request：

```json
{"id":"loc-1a2b3c4d","reason":"agent 请求定位","timeout_ms":45000,
 "max_age_ms":15000,"high_accuracy":true}
```

`POST /amap/report` 的 body：

```json
{"device_id":"phone-1","request_id":"loc-1a2b3c4d",
 "lng":116.397428,"lat":39.90923,"accuracy":6.5,"provider":"gps",
 "altitude":45.0,"speed":0.0,"bearing":0.0,"ts":1770000000.0,
 "battery":66,"error":""}
```

- `request_id` 为空串 = 主动上报（不回应任何请求），照样入库。
- **定位失败也要上报**：把 `error` 填上失败原因，坐标填 0 即可；插件会记录失败、唤醒等待者，不会让 agent 干等。
- 坐标为 `0,0` 且没有 `error` 时，插件也会当成失败拦下，不会把「几内亚湾」写进位置历史。

---

## 八、坐标系（重要）

- 手机 GPS 原生是 **WGS-84**；高德全系接口吃 **GCJ-02**（火星坐标）。
- 两者在国内差**几百米**，直接混用会出现「明明在小区里，地址却报到隔壁街道」的现象。
- 本插件：手机上报 WGS-84 → 落库时自动用本地算法转成 GCJ-02（`auto_convert`，默认开）→ 再调高德。
- 工具参数里凡是要坐标的（`location`、`center`、`near`、`origin`…）**一律按 GCJ-02 解释**。
- 转换用本地算法（`convert_host="local"`），零延迟、不消耗配额；想走高德官方接口可以改成 `api`。

---

## 九、配额（用之前务必看一眼）

高德现在是**按月**计算的配额。个人认证开发者的额度大致是：

| 服务 | 月度额度 |
|---|---|
| 基础 LBS（路径规划、地理编码、逆地理、坐标转换、行政区划、IP 定位、静态地图、距离测量） | 15 万 / 月 |
| **搜索类（关键字/周边 POI、输入提示）** | **5,000 / 月** |
| **天气** | **5,000 / 月** |

搜索和天气的额度比基础服务低 30 倍，所以插件默认开了 **TTL 缓存**（`cache_ttl=120` 秒）：
同一句话在两分钟内再问一次直接走缓存，不消耗配额。不建议关掉。

另外：**高德严禁压测**，被判定为压测会自动封停；单 IP 超限（`10010`）封停后**必须提工单才能恢复**。
所以别拿它跑循环测试——本插件的自测用的是本地假高德服务，一个有意义的请求都不打。

---

## 十、配置项一览

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 是否启动手机定位通道（关掉后高德工具照常可用） |
| `listen_host` / `listen_port` | 0.0.0.0 / 6310 | 局域网服务地址端口（避开 AstrBot WebUI 的 6185/6186/6199，也别和其它插件的端口撞车） |
| `auth_token` | 空 | 可选令牌，App 里要填一样的 |
| `amap_key` | 空 | 高德 **Web服务** key |
| `amap_sig_secret` | 空 | 只在控制台开了数字签名时才填 |
| `api_base` | restapi.amap.com | 一般不用改（改这个可以做本地假服务测试） |
| `request_timeout` / `retry_times` | 10 / 1 | 高德请求超时与重试（仅网络类错误重试） |
| `cache_ttl` | 120 | 查询缓存秒数，0=关闭 |
| `auto_convert` | true | WGS-84 → GCJ-02 自动转换 |
| `convert_host` | local | `local` 本地算法 / `api` 高德官方接口 |
| `locate_timeout` | 45 | 等手机回传坐标的最长秒数 |
| `locate_max_age` | 15 | 位置缓存可用时长；0 = 每次都要手机现场取 |
| `high_accuracy` | true | 让手机优先用 GPS 而不是基站定位 |
| `default_device` | 空 | agent 不指定设备时用它；留空则只有一台设备时自动选 |
| `poll_interval` | 15 | 建议给 App 的轮询间隔 |
| `device_offline_sec` | 90 | 超过这么久没轮询就算离线（此时定位会立刻返回提示而不是干等） |
| `history_limit` | 200 | 每台手机保留多少条位置记录 |
| `persist` | true | 状态写进 `data/plugin_data/astrbot_plugin_amap/state.json` |
| `max_text_chars` | 6000 | 给 agent 的工具结果截断长度 |
| `poi_page_size` | 8 | POI 一次返回几条 |
| `static_map_zoom` / `static_map_size` | 14 / 750*500 | 静态地图默认参数 |
| `static_map_dir` | 空 | 图片保存目录，留空=插件数据目录下 `static_maps/` |

---

## 十一、常见问题

**Q：插件装了但聊天里没反应？**
先 `/amap_status` 看 key 配没配、设备在不在线。新插件必须重启 AstrBot 才加载。

**Q：`/amap_loc` 一直转圈然后说超时？**
手机 App 的前台服务没在跑，或者被系统杀了后台。看 App 里「最近一次上报」的时间戳。国产 ROM 记得给自启动权限。

**Q：位置出来了，地址却是隔壁小区的？**
坐标系问题。检查 `auto_convert` 是不是被关了。

**Q：报 `10009 USERKEY_PLAT_NOMATCH`？**
key 的服务平台选错了，必须是「Web服务」。

**Q：报 `10007 INVALID_USER_SIGNATURE`？**
你在高德控制台开了数字签名，但没填 `amap_sig_secret`。

**Q：报 `10003 DAILY_QUERY_OVER_LIMIT` / `10019`？**
配额用完了（搜索和天气各只有 5000/月），等下个月或者升企业认证。

**Q：静态地图怎么发到聊天里？**
用命令 `/amap_map 天安门 15`，它会直接发图。agent 调 `amap_static_map` 只会把图存到主机并把路径告诉它。

**Q：和别的插件端口冲突怎么办？**
本插件默认监听 6310。如果同时启用了别的也要监听端口的插件，撞了就在配置里把 `listen_port` 换掉，
改完重载插件即可。注意 AstrBot 自己的 WebUI 占用了 6185/6186/6199，别往那边填。

---

## 十二、出门也能用（组网方案）

插件默认只监听局域网，手机一离开 WiFi 就连不上——这很正常，家里宽带大多数没有公网 IP，
就算有，把一个明文无加密的位置接口直接暴露到公网也是给自己找麻烦。

正确做法是**组网**：主机和手机各装一个组网客户端、登录同一个账号，
两台设备就"永远待在同一个局域网里"，手机走 4G/5G 也能直接访问主机的虚拟地址。
不需要公网 IP、不需要域名、不往公网暴露任何端口，而且组网本身是加密的。

### 选哪个客户端

按「一个人自用 + 一台 Windows 主机 + 一部安卓手机」这个场景，推荐顺序是：

| 顺序 | 方案 | 免费额度 | 死穴 |
|---|---|---|---|
| 1 | Tailscale | 设备不限，≤6 用户 | 控制面与 DERP 中继都在境外，国内可用性必须先实测 |
| 2 | 蒲公英（贝锐） | 1 个网络 / 3 个成员 | 闭源；P2P 打不通时转发只有 2Mbps |
| 3 | ZeroTier | 1 个网络 / 10 台设备 | 控制面 planet 同样在境外，自建 Moon 只能优化、不能替换 |

再往下（Cloudflare Tunnel、frp、ngrok、cpolar、花生壳、家宽公网直连）不是要域名就是要 VPS，
而且手机端十几秒一次的轮询会把按请求数计费的免费额度直接打爆——
比如 ngrok 免费版每月只有 2 万次请求，而 30 秒轮询一个月就是 8.6 万次，超 4 倍。
组网方案没有这个问题：请求跑在自己的虚拟局域网里，不经过任何计费网关。

Radmin VPN 只有 Windows 客户端，安卓端根本没有，出局。

### 先花 15 分钟验证 Tailscale

Tailscale 唯一的不确定性是**中国大陆能不能连上控制面**，这个只能实测。两台设备装好、登进同一账号后，在主机上跑：

```powershell
tailscale status
tailscale netcheck
tailscale ping --until-direct <手机在 Tailscale 里的设备名>
```

三条都通就放心用 Tailscale——它不往公网开任何入站端口，扫描器连门都找不到。
`tailscale up` 卡住、或者 netcheck 显示连不上 DERP，别硬扛，直接换蒲公英。

### 装完之后

主机上会多出一块虚拟网卡，并分到该方案专用网段的地址（Tailscale 是 `100.x.x.x`）。

### 关键一步：放行 Windows 防火墙

组网通了但手机还是连不上，九成是卡在这里——Windows 防火墙默认拦掉所有入站连接。
用**管理员** PowerShell 跑一次：

```powershell
New-NetFirewallRule -DisplayName "AstrBot amap 6310" -Direction Inbound -Protocol TCP -LocalPort 6310 -Profile Any -Action Allow
```

`-Profile Any` 是必须的：组网虚拟网卡经常被 Windows 归类成「公用网络」，
只放行「专用网络」照样连不上。

### 手机 App 里填什么

启动日志和 `/amap_status` 会直接把候选地址列出来并标注用途，照着填就行：

```
手机 App 里「主机地址」可填：
  · http://100.101.102.103:6310   组网虚拟地址（Tailscale，出门就填它）
  · http://192.168.1.100:6310     局域网地址（只在同一 WiFi 下可用）
```

主机地址那一栏也支持直接填带协议的完整 URL（例如 `https://myhost.tailxxxx.ts.net`），
这时端口框会被忽略，端口以 URL 为准。

> 装好组网客户端后，主机的实际内网地址可能会变，App 里填的地址也要跟着改。
> 改完记得点「保存配置」，再点「测试连接」确认。

### 轮询间隔别调太密

App 默认每 15 秒问一次「有没有活干」，一个月约 17 万次请求。在组网方案里这些请求不出虚拟局域网，
完全免费；但如果你哪天改成按请求数计费的穿透服务，记得把 `poll` 间隔调大，否则额度几天就烧光。

---

## 十三、跑自测

```powershell
cd <AstrBot 根目录>
.\venv\Scripts\python.exe data\plugins\astrbot_plugin_amap\tests\selftest.py
```

128 项检查，覆盖语法、配置 schema 对齐、坐标转换、formatter 容错、仓储落盘、
定位闭环（下发→轮询→上报→唤醒）、端到端 HTTP 链路、14 个工具逐个跑通、
以及「没配 key 时每个工具都要好好说话而不是抛异常」。

全程使用本地假高德服务，**不联网、不需要 key、不消耗任何高德配额**。

想验证真高德的错误链路（只发 1 次请求）：

```powershell
.\venv\Scripts\python.exe data\plugins\astrbot_plugin_amap\tests\probe_real_amap.py
```

---

## 十四、合规与免责声明

### 关于高德开放平台

本插件**不内置、不附带、不分发任何高德 API Key**。每位使用者都要自己到高德开放平台申请一个
「Web服务」类型的 Key 填进配置，所有调用配额、费用与合规责任都落在你自己的账号上。

请留意这几点：

- 用量算在你自己账号上，和你申请时选择的认证类型（个人 / 企业）对应；
- 高德给个人认证开发者的免费配额限定为**非商业用途**。要把这套东西用在商业场景，
  需要按高德的要求升级为企业认证并遵守对应条款——这是使用者的责任，不是插件作者的责任；
- 请遵守[《高德服务条款》](https://map.amap.com/doc/serviceitem.html)与
  [《自定义地图服务协议》](https://lbs.amap.com/home/customize-map-terms/)：
  不要用它做压测、不要试图绕过配额限制、不要把自己的 Key 转借或公开出去
  （协议原文明确写了账户仅限本人使用，不得转让、出售、出租、出借）；
- `amap_static_map` 工具会把地图图片落到本地。图片内容来自高德，
  如果要对外公开发布或用于对外展示，请自行确认高德的地图合规要求与国家测绘相关规定
  （审图号、地图要素完整性等）。

本插件只是高德 Web 服务 API 的一个客户端封装，不包含、也不再分发高德提供的任何软件或数据。

### 关于本项目

- **代码主要由 AI 生成**，作者负责提需求、实际使用和验收。项目经过 128 项离线自测
  （全程使用本地假高德服务，不消耗任何配额）以及真实环境下的端到端联调，
  但**不保证没有缺陷**，请自行审阅代码后再决定是否使用。
- 这是一个**自用为主**的项目，作者**不承诺持续维护，也不保证处理 issue 或 PR**。
  欢迎反馈，但不保证有回应，也请别指望哪天突然加新功能。
- 以 [MIT 协议](LICENSE) 发布，可自由使用、修改、分发，保留版权声明即可。
- 使用本项目所产生的一切后果由使用者自行承担。

