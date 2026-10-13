package com.away.amap

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** 一次 HTTP 调用的结果：ok=是否 2xx，body=响应体原文，error=失败原因（成功时为空）。 */
data class ApiResult(val ok: Boolean, val body: String, val error: String) {

    /** 尝试把响应体解析成 JSON，失败返回 null。 */
    fun json(): JSONObject? = try {
        JSONObject(body)
    } catch (e: Exception) {
        null
    }
}

/** 服务端 /amap/poll 下发的定位请求。 */
data class LocateRequest(
    val id: String,
    val reason: String,
    val timeoutMs: Int,
    val maxAgeMs: Int,
    val highAccuracy: Boolean
) {
    companion object {
        fun from(json: JSONObject): LocateRequest {
            var timeout = json.optInt("timeout_ms", 20000)
            if (timeout <= 0) {
                timeout = 20000
            }
            return LocateRequest(
                id = json.optString("id", ""),
                reason = json.optString("reason", ""),
                timeoutMs = timeout,
                maxAgeMs = json.optInt("max_age_ms", 0),
                highAccuracy = json.optBoolean("high_accuracy", false)
            )
        }
    }
}

/**
 * 与 AstrBot 主机 astrbot_plugin_amap 的全部交互：
 *   GET  /amap/ping   /amap/poll   /amap/last   /amap/health
 *   POST /amap/report
 * 配置了令牌时每个请求都带 X-Amap-Token。
 *
 * 同步方法（*Sync）跑在调用者的线程上，服务里由工作线程调用；
 * 不带 Sync 的是异步包装，回调在 OkHttp 自己的线程上，UI 侧需要 runOnUiThread。
 */
class ApiClient(private val prefs: Prefs) {

    private val jsonType = "application/json; charset=utf-8".toMediaType()

    private val client = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    /** 拼请求：相对路径 + 基地址，带令牌头。 */
    private fun builder(path: String): Request.Builder {
        val req = Request.Builder().url(prefs.baseUrl + path)
        val token = prefs.token.trim()
        if (token.isNotEmpty()) {
            req.addHeader("X-Amap-Token", token)
        }
        return req
    }

    /** 同步 GET。 */
    private fun getSync(path: String): ApiResult {
        return try {
            client.newCall(builder(path).get().build()).execute().use { resp ->
                val body = resp.body?.string() ?: ""
                if (resp.isSuccessful) {
                    ApiResult(true, body, "")
                } else {
                    ApiResult(false, body, "HTTP " + resp.code)
                }
            }
        } catch (e: Exception) {
            ApiResult(false, "", e.message ?: "network error")
        }
    }

    /** 同步 POST JSON。 */
    private fun postSync(path: String, json: JSONObject): ApiResult {
        return try {
            val req = builder(path).post(json.toString().toRequestBody(jsonType)).build()
            client.newCall(req).execute().use { resp ->
                val body = resp.body?.string() ?: ""
                if (resp.isSuccessful) {
                    ApiResult(true, body, "")
                } else {
                    ApiResult(false, body, "HTTP " + resp.code)
                }
            }
        } catch (e: Exception) {
            ApiResult(false, "", e.message ?: "network error")
        }
    }

    /** 异步回调用的单线程池：UI 侧的调用排好队，不会一次打出一堆请求。 */
    private val callbackExecutor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "amap-api").apply { isDaemon = true }
    }

    /** 异步包装：回调在 amap-api 线程上，UI 侧需要 runOnUiThread。 */
    private fun async(block: () -> ApiResult, onDone: (ApiResult) -> Unit) {
        callbackExecutor.execute {
            val result = try {
                block()
            } catch (e: Exception) {
                ApiResult(false, "", e.message ?: "error")
            }
            onDone(result)
        }
    }

    // ---------------- 契约接口（都是阻塞的，请在后台线程调用） ----------------

    /** GET /amap/ping?device_id=&name= —— 测试连通性。 */
    fun pingSync(): ApiResult =
        getSync("/amap/ping?device_id=" + enc(prefs.deviceId) + "&name=" + enc(prefs.deviceName))

    /** GET /amap/poll?device_id= —— 取待办定位请求。 */
    fun pollSync(): ApiResult = getSync("/amap/poll?device_id=" + enc(prefs.deviceId))

    /** POST /amap/report —— 上报一次定位（失败也要报，带 error 字段）。 */
    fun reportSync(payload: JSONObject): ApiResult = postSync("/amap/report", payload)

    /** GET /amap/last?device_id= —— 查服务端记着的最后一次位置。 */
    fun lastSync(): ApiResult = getSync("/amap/last?device_id=" + enc(prefs.deviceId))

    /** GET /amap/health —— 服务端自检。 */
    fun healthSync(): ApiResult = getSync("/amap/health")

    /** 异步版：在 OkHttp 线程池里跑，回调不在主线程，UI 侧记得 runOnUiThread。 */
    fun ping(onDone: (ApiResult) -> Unit) = async({ pingSync() }, onDone)

    fun poll(onDone: (ApiResult) -> Unit) = async({ pollSync() }, onDone)

    fun report(payload: JSONObject, onDone: (ApiResult) -> Unit) =
        async({ reportSync(payload) }, onDone)

    fun last(onDone: (ApiResult) -> Unit) = async({ lastSync() }, onDone)

    fun health(onDone: (ApiResult) -> Unit) = async({ healthSync() }, onDone)

    // ---------------- 内部 ----------------

    /** URL 编码：设备名可能是中文。 */
    private fun enc(text: String): String {
        return try {
            URLEncoder.encode(text, "UTF-8")
        } catch (e: Exception) {
            text
        }
    }
}
