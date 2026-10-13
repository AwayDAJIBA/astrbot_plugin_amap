package com.away.amap

import android.content.Context
import android.content.SharedPreferences
import android.os.Build

/** 设置存本地：主机地址 / 端口 / 令牌 / 设备名 / device_id / 轮询间隔。 */
class Prefs(context: Context) {

    private val sp: SharedPreferences =
        context.getSharedPreferences("amap_link", Context.MODE_PRIVATE)

    var host: String
        get() = sp.getString("host", "192.168.1.10") ?: "192.168.1.10"
        set(value) = sp.edit().putString("host", value).apply()

    /** AstrBot 上 astrbot_plugin_amap 的 HTTP 端口，默认 6310。 */
    var port: Int
        get() = sp.getInt("port", 6310)
        set(value) = sp.edit().putInt("port", value).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(value) = sp.edit().putString("token", value).apply()

    var deviceName: String
        get() = sp.getString("device_name", Build.MODEL ?: "android") ?: "android"
        set(value) = sp.edit().putString("device_name", value).apply()

    /** 轮询 /amap/poll 的间隔（秒），默认 15。 */
    var pollSeconds: Int
        get() = sp.getInt("poll_seconds", 15)
        set(value) = sp.edit().putInt("poll_seconds", value).apply()

    /** 手动「立即上报」时用的超时（毫秒）；服务端下发的 request 会覆盖它。 */
    var fixTimeoutMs: Int
        get() = sp.getInt("fix_timeout_ms", 20000)
        set(value) = sp.edit().putInt("fix_timeout_ms", value).apply()

    var deviceId: String
        get() {
            val saved = sp.getString("device_id", "") ?: ""
            if (saved.isNotEmpty()) {
                return saved
            }
            val made = makeDeviceId()
            sp.edit().putString("device_id", made).apply()
            return made
        }
        set(value) = sp.edit().putString("device_id", value).apply()

    /**
     * 手机端要访问的基地址，支持两种填法：
     *
     *  1) 「主机地址 + 端口」模式（默认，局域网和 Tailscale/ZeroTier 直接连都用这个）：
     *     host = 100.101.102.103，port = 6310  →  http://100.101.102.103:6310
     *
     *  2) 「完整 URL」模式：host 里直接写带协议的完整地址，端口以 URL 为准、上面的端口框会被忽略。
     *     适合走了 HTTPS 的场景，例如 Tailscale 的 `tailscale serve` 会自动给你一个
     *     https://<主机名>.<你的tailnet>.ts.net 域名（真证书，安卓系统直接信任）：
     *     host = https://myhost.tailxxxx.ts.net  →  原样使用
     */
    val baseUrl: String
        get() {
            val raw = host.trim().trimEnd('/')
            if (raw.startsWith("http://") || raw.startsWith("https://")) {
                return raw
            }
            return "http://" + raw + ":" + port
        }

    /** 是否处于「完整 URL」模式（界面用来提示端口框不生效）。 */
    val isFullUrlMode: Boolean
        get() {
            val raw = host.trim()
            return raw.startsWith("http://") || raw.startsWith("https://")
        }

    /** 首次运行时按机型 + 指纹哈希生成一个稳定的 device_id。 */
    private fun makeDeviceId(): String {
        val model = (Build.MODEL ?: "android").replace(" ", "-")
        val tail = (Build.FINGERPRINT ?: "").hashCode().toString().replace("-", "x")
        return (model + "-" + tail.take(6)).lowercase()
    }
}
