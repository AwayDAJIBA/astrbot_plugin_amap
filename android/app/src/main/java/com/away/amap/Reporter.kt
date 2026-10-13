package com.away.amap

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.location.Location
import android.os.BatteryManager
import org.json.JSONObject

/**
 * 「取一次定位 → POST /amap/report」的共用实现：
 * 前台服务轮询到 request 时调用，界面上的「立即上报一次位置」也调用它。
 *
 * 约定：定位失败也照样上报，坐标填 0 并带上 error 字段，避免服务端一直等。
 */
object Reporter {

    /**
     * 阻塞式上报一次（必须在工作线程调用）。
     *
     * @param request 服务端下发的请求；手动上报时传 null（request_id 为空串）
     * @return 给界面看的一句话摘要
     */
    fun reportOnce(context: Context, prefs: Prefs, api: ApiClient, request: LocateRequest?): String {
        val timeout = request?.timeoutMs ?: prefs.fixTimeoutMs
        val maxAge = request?.maxAgeMs ?: 0
        val highAccuracy = request?.highAccuracy ?: true
        val requestId = request?.id ?: ""

        val fix = try {
            LocateHelper.acquire(context, timeout, maxAge, highAccuracy)
        } catch (e: Exception) {
            LocateHelper.Fix(false, null, "unknown", "取点异常：" + (e.message ?: ""))
        }

        val payload = buildPayload(context, prefs, requestId, fix)
        val result = api.reportSync(payload)

        val summary = describe(fix, payload, result)
        LocateBus.update(lastReport = summary)
        LocateBus.log("上报 " + (if (requestId.isEmpty()) "(手动)" else "request=" + requestId) + " → " + summary)

        // 服务端返回的原话也贴到界面上
        val serverText = if (result.ok) {
            val json = result.json()
            if (json != null) {
                "accepted=" + json.optBoolean("accepted", false) +
                    " matched=" + json.optBoolean("matched", false) +
                    " request_id=" + json.optString("request_id", "")
            } else {
                "HTTP 200，但响应不是 JSON：" + result.body.take(200)
            }
        } else {
            "上报失败：" + result.error + (if (result.body.isEmpty()) "" else " / " + result.body.take(200))
        }
        LocateBus.update(serverMessage = serverText)
        LocateBus.log("服务端：" + serverText)
        return summary
    }

    /** 按契约拼 body。provider 只会是 gps/network/passive/unknown。 */
    private fun buildPayload(
        context: Context,
        prefs: Prefs,
        requestId: String,
        fix: LocateHelper.Fix
    ): JSONObject {
        val location: Location? = fix.location
        val json = JSONObject()
        json.put("device_id", prefs.deviceId)
        json.put("request_id", requestId)
        // 系统原始 WGS-84 坐标，不做任何转换（转换交给服务端插件）
        json.put("lng", if (location != null) location.longitude else 0.0)
        json.put("lat", if (location != null) location.latitude else 0.0)
        json.put("accuracy", if (location != null && location.hasAccuracy()) location.accuracy.toDouble() else 0.0)
        json.put("provider", fix.provider)
        json.put("altitude", if (location != null && location.hasAltitude()) location.altitude else 0.0)
        json.put("speed", if (location != null && location.hasSpeed()) location.speed.toDouble() else 0.0)
        json.put("bearing", if (location != null && location.hasBearing()) location.bearing.toDouble() else 0.0)
        json.put("ts", System.currentTimeMillis() / 1000.0)
        json.put("battery", batteryPercent(context))
        json.put("error", fix.error)
        return json
    }

    /** 当前电量百分比，取不到就回 -1。 */
    private fun batteryPercent(context: Context): Int {
        return try {
            val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
            val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
            if (level < 0 || scale <= 0) -1 else level * 100 / scale
        } catch (e: Exception) {
            -1
        }
    }

    /** 摘要：坐标 / 精度 / 来源 / 时间。 */
    private fun describe(fix: LocateHelper.Fix, payload: JSONObject, result: ApiResult): String {
        val lng = payload.optDouble("lng", 0.0)
        val lat = payload.optDouble("lat", 0.0)
        val accuracy = payload.optDouble("accuracy", 0.0)
        val time = LocateBus.nowText()
        val head = if (fix.ok) {
            "lng=" + fmt(lng) + " lat=" + fmt(lat) +
                " 精度≈" + accuracy.toInt() + "m 来源=" + fix.provider
        } else {
            "定位失败（已上报 error）"
        }
        val tail = if (result.ok) "上报成功" else "上报失败(" + result.error + ")"
        return head + "  " + tail + "  " + time
    }

    private fun fmt(value: Double): String = String.format(java.util.Locale.US, "%.6f", value)
}
