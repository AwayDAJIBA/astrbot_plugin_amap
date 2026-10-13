# Dejiko定位（astrbot_plugin_amap 配套 Android App）

配合 AstrBot 插件 **astrbot_plugin_amap** 使用的手机端定位 App：前台服务常驻，
定时向主机 `GET /amap/poll` 询问「有没有人要定位」，有请求就用**系统 `LocationManager`** 取一次点，
再 `POST /amap/report` 回传。

- 包名 / applicationId：`com.away.amap`
- 显示名：`Dejiko定位`
- **不依赖任何第三方定位或地图 SDK**（没有高德/百度/Google SDK，也不需要任何 Android Key），
  只有一个额外依赖 OkHttp 用来发 HTTP。
- minSdk 24（Android 7.0）/ targetSdk 34 / compileSdk 34，Kotlin 1.9.22，AGP 8.2.2。

---

## 1. 目录结构

```
android/
├─ settings.gradle.kts        rootProject.name = "AmapLink"
├─ build.gradle.kts           顶层插件版本（AGP 8.2.2 / Kotlin 1.9.22）
├─ gradle.properties
├─ gradlew / gradlew.bat / gradle/wrapper/    Gradle 8.2 wrapper
├─ local.properties           sdk.dir=...（本机 SDK 路径，不要提交到 git）
├─ local.properties.example   模板，复制成 local.properties 改成自己的路径
└─ app/
   ├─ build.gradle.kts
   └─ src/main/
      ├─ AndroidManifest.xml
      ├─ java/com/away/amap/
      │  ├─ MainActivity.kt    界面：配置 / 状态 / 日志
      │  ├─ LocateService.kt   前台服务：轮询 + 取点 + 上报
      │  ├─ LocateHelper.kt    只用系统 LocationManager 的一次性取点
      │  ├─ Reporter.kt        拼 body + POST /amap/report（服务与「立即上报」共用）
      │  ├─ ApiClient.kt       OkHttp + org.json，5 个契约接口
      │  ├─ Prefs.kt           SharedPreferences 配置存储
      │  └─ LocateBus.kt       服务 → 界面的状态/日志总线
      └─ res/                  布局、颜色（含 values-night 深色）、主题、图标
```

---

## 2. 编译打包

### 2.1 环境要求

| 项目 | 要求 |
| --- | --- |
| JDK | **17**（AGP 8.2 不支持 JDK 21+，用 JDK 17 最稳） |
| Android SDK | `platforms;android-34`、`build-tools;34.0.0`、`platform-tools` |
| Gradle | 用工程自带的 `gradlew`（8.2），不用自己装 |

先写 `android/local.properties`（Windows 路径用正斜杠），指向你自己的 SDK：

```properties
sdk.dir=C:/Android/Sdk
```

### 2.2 Debug 包

在 `android/` 目录下（把两个环境变量换成你自己的路径）：

```bat
set JAVA_HOME=C:\Program Files\Java\jdk-17
set ANDROID_HOME=C:\Android\Sdk
gradlew.bat assembleDebug
```

产物：`android\app\build\outputs\apk\debug\app-debug.apk`
（debug 包用系统默认调试签名，可以直接装到手机上试。）

安装：`adb install -r app\build\outputs\apk\debug\app-debug.apk`

### 2.3 Release 包

**方式 A：Android Studio**（最省事）
`Build → Generate Signed App Bundle / APK → APK → Create new…` 新建 keystore，
选 `release`，勾上 `V1 + V2` 签名即可。

**方式 B：命令行 + keystore**

1. 生成 keystore（只做一次，**务必保管好，丢了就不能覆盖安装升级**）：

```bat
keytool -genkeypair -v -keystore amaplink.jks -alias amaplink ^
        -keyalg RSA -keysize 2048 -validity 10000
```

2. 在 `android/keystore.properties`（**别提交到 git**）里写：

```properties
storeFile=../amaplink.jks
storePassword=你的密码
keyAlias=amaplink
keyPassword=你的密码
```

3. 在 `app/build.gradle.kts` 的 `android { }` 里加签名配置：

```kotlin
val keystoreProps = java.util.Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    signingConfigs {
        create("release") {
            if (keystoreProps.isNotEmpty()) {
                storeFile = file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
}
```

4. 打包：

```bat
gradlew.bat assembleRelease
```

产物：`android\app\build\outputs\apk\release\app-release.apk`

> 也可以直接 `gradlew.bat assembleRelease` 出一个**未签名**的 release 包，
> 再用 `apksigner sign --ks amaplink.jks app-release-unsigned.apk` 手动签名。
> 未签名的 APK 是装不上的，必须签名。

### 2.4 依赖拉不动时

`settings.gradle.kts` 里已经配了阿里云镜像（google / public / gradle-plugin），
正常情况下不需要梯子。要是公司网络拦了，可加 `--offline` 用本地缓存：

```bat
gradlew.bat assembleDebug --offline
```

---

## 3. 使用步骤

1. 手机和跑 AstrBot 的机器在**同一个局域网**。
2. AstrBot 里启用 `astrbot_plugin_amap`，记下它的 HTTP 端口（默认 **6310**）。
3. 打开 App：
   - **主机地址**：AstrBot 所在机器的内网 IP，例如 `192.168.1.10`（不要带 `http://`，不要带端口）
   - **端口**：`6310`
   - **令牌**：插件里配了令牌就填，没配就留空
   - **设备名 / device_id**：默认自动填（device_id 首次运行按机型自动生成），可以手改
   - **轮询秒**：默认 15（最小 3）
4. 点「保存配置」→「测试连接」，看到 `连接成功：astrbot_plugin_amap v1.0.0 …` 就通了。
5. 点「启动服务」。状态灯变绿、通知栏出现「Dejiko定位运行中」即成功。
6. 需要马上要一次位置，点「立即上报」；想看服务端记了什么，可以用
   `GET /amap/last?device_id=<你的id>`。

---

## 4. 权限说明

| 权限 | 为什么要 |
| --- | --- |
| `INTERNET` / `ACCESS_NETWORK_STATE` | 和 AstrBot 主机通信 |
| `ACCESS_FINE_LOCATION` | 拿 GPS 精确位置；**Android 6.0+ 必须动态申请**，App 里点「立即上报」/「启动服务」时会弹窗 |
| `ACCESS_COARSE_LOCATION` | 只给模糊定位时的兜底（部分 ROM 只给大概位置也能出点） |
| `FOREGROUND_SERVICE` | 前台服务常驻，防后台被杀 |
| `FOREGROUND_SERVICE_LOCATION` | Android 14（targetSdk 34）对 `foregroundServiceType="location"` 的强制要求 |
| `POST_NOTIFICATIONS` | Android 13+ 通知要动态申请，否则常驻通知不显示（服务仍能跑） |
| `WAKE_LOCK` | 取点 + 上报那几秒持有 `PARTIAL_WAKE_LOCK`，防止刚息屏就断 |

另外 manifest 里开了 `android:usesCleartextTraffic="true"` + `network_security_config`
放行明文 HTTP —— 局域网里 AstrBot 一般没有证书，必须开。**公网部署请务必改回 HTTPS。**

---

## 5. HTTP 契约（App 侧调用的 5 个接口）

基址 `http://<host>:<port>`；配了令牌时每个请求都带 `X-Amap-Token: <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/amap/ping?device_id=&name=` | 「测试连接」，返回版本、`poll_interval`、`device_known`、`locate_ready` |
| GET | `/amap/poll?device_id=` | 服务每 `poll_seconds` 秒调一次；`request` 非空就表示要定位 |
| POST | `/amap/report` | 上报一次定位结果（**失败也报**，坐标填 0 + `error` 字段） |
| GET | `/amap/last?device_id=` | 查服务端记的最后一次位置（App 内暂未做界面，接口已实现） |
| GET | `/amap/health` | 服务端自检（App 内暂未做界面，接口已实现） |

`POST /amap/report` 的 body：

```json
{
  "device_id": "xiaomi-xxxxxx",
  "request_id": "3f2c…",
  "lng": 116.397428,
  "lat": 39.90923,
  "accuracy": 12.5,
  "provider": "gps",
  "altitude": 43.0,
  "speed": 0.0,
  "bearing": 0.0,
  "ts": 1712345678.123,
  "battery": 87,
  "error": ""
}
```

> **坐标是系统原始 WGS-84，App 不做任何坐标转换**（不转 GCJ-02 / BD-09），
> 由服务端插件统一处理。

### 取点顺序

1. `GPS_PROVIDER`（`requestLocationUpdates` 等一次回调，按 `request.timeout_ms` 超时）
2. 失败 → `NETWORK_PROVIDER`（两个 provider 共用同一份超时预算）
3. 再失败 → `getLastKnownLocation`（gps / network / passive 里取最新的一个）
4. 全失败 → 照样上报，`lng=0, lat=0, provider="unknown"`，`error` 写清原因

特例：`request.high_accuracy == false` 且 `max_age_ms > 0`，而系统缓存的位置还在
`max_age_ms` 之内时，**直接用缓存**，不去等 GPS（省电、快速返回）。其余情况一律 GPS 优先。

回退到缓存且缓存比 `max_age_ms` 还旧时，坐标照报，但 `error` 会标注「回退到过期缓存」，
方便服务端自己决定要不要信。

---

## 6. 常见问题（FAQ）

**Q1：服务在后台被系统杀了 / 过一会儿就不上报了。**
前台服务只是「降低」被杀概率，国产 ROM（小米/华为/OPPO/vivo/荣耀等）还要手动放行：

- 设置 → 应用 → Dejiko定位 → **省电策略改成「无限制」/「允许后台运行」**
- 打开**自启动 / 关联启动**（部分 ROM 叫「允许后台弹出界面」「后台活动」）
- 最近任务列表里给 App **加锁**（下拉或长按卡片上锁），别一键清理
- 开发者选项里如果有「不保留活动」「后台进程限制」，别开
- Android 13+ 记得给**通知权限**，否则常驻通知不显示，前台服务更容易被清

**Q2：点了「启动服务」，App 提示没有定位权限 / 服务起不来。**
Android 14（API 34）起，`foregroundServiceType="location"` 的前台服务**必须先有定位权限**才能启动。
去 设置 → 应用 → Dejiko定位 → 权限 → 位置信息，选「使用应用期间允许」或「始终允许」。
App 在启动服务前也会先弹权限申请，拒绝的话会明确写日志并取消启动（不会闪退）。

**Q3：Android 13+ 通知栏不显示「Dejiko定位运行中」。**
Android 13 起通知是**动态权限**。App 会在启动服务时申请一次；如果之前点过「拒绝」，
去 设置 → 应用 → Dejiko定位 → 通知，手动打开。通知被关了服务仍会跑，只是看不到常驻通知。

**Q4：一直拿不到点（日志里是「定位失败」）。**
- 室内 GPS 没信号是正常的，去窗边或室外；第一次定位（冷启动）也可能要 30s 以上
- 确认系统**位置服务总开关**是开的（下拉快捷开关里的定位图标）
- 检查是不是禁用了「Google 位置服务 / 网络定位」，关了的话 NETWORK_PROVIDER 也用不了
- 服务端给的 `timeout_ms` 太小也会失败，可以让服务端调大一点
- 实在取不到，App 也会上报一条带 `error` 的记录，服务端不会一直等

**Q5：坐标和地图上对不上（偏了几百米）。**
这是**坐标系**问题，不是 App 的问题：App 只上报系统原始 **WGS-84**。
高德/腾讯地图用 **GCJ-02**，百度用 **BD-09**。
转换由服务端 `astrbot_plugin_amap` 插件负责（它也持有高德 Key）。

**Q6：测试连接失败 / 一直转圈。**
- 主机地址填的是**内网 IP** 吗？`127.0.0.1` 在手机上指的是手机自己
- 电脑防火墙是否放行了 6310 端口（Windows 第一次跑 AstrBot 会弹「允许访问网络」，要允许专用网络）
- 端口是否和插件配置一致
- 插件里配了令牌，App 里的令牌是不是没填 / 填错（会返回 401/403）
- 手机和电脑是不是连的同一个 Wi-Fi（注意别一个连 5G 一个连 2.4G 分属不同网段，也别开 VPN）

**Q7：改了端口/地址后服务没生效。**
点「保存配置」再「停止服务」→「启动服务」。轮询间隔是每一轮重新读取的，改完就生效；
但 baseUrl 是在服务启动时写进状态的，稳妥起见重启服务。

**Q8：耗电吗？**
默认 15 秒轮询一次，但**只有拿到 request 才会真正开 GPS**，空闲时几乎没有定位开销。
嫌频繁可以把「轮询秒」调大，或者不用时点「停止服务」。

**Q9：能同时装 `手机通道`（astrbot_plugin_phone_link）吗？**
可以，两个 App 包名不同（`com.away.amap` / `com.away.phonelink`），互不影响。
只有一个建议：别让两个 App 都申请「始终允许定位」，部分 ROM 会互相干扰后台策略。
