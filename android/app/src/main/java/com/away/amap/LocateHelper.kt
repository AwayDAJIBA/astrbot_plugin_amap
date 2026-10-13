package com.away.amap

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.HandlerThread
import android.os.Looper
import androidx.core.content.ContextCompat
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 一次性取点，只用系统 LocationManager（无任何第三方定位 SDK）。
 *
 * 顺序：GPS_PROVIDER → NETWORK_PROVIDER → getLastKnownLocation。
 * 拿到的经纬度是系统原始 WGS-84 坐标，**不做任何坐标转换**（转换由服务端插件负责）。
 */
object LocateHelper {

    /** 取点结果：ok=false 时 location 为 null，error 是给服务端的原因。 */
    data class Fix(
        val ok: Boolean,
        val location: Location?,
        val provider: String,
        val error: String
    )

    /**
     * 阻塞式取一次定位（必须在工作线程调用）。
     *
     * @param timeoutMs   单次等待超时；两个 provider 合计最多花这么久
     * @param maxAgeMs    缓存定位的最大可接受年龄，<=0 表示不限制
     * @param highAccuracy 服务端是否要高精度。false 且缓存还新鲜时直接用缓存，不等 GPS
     */
    fun acquire(context: Context, timeoutMs: Int, maxAgeMs: Int, highAccuracy: Boolean = true): Fix {
        if (!hasPermission(context)) {
            return Fix(false, null, "unknown", "缺少定位权限（请在系统设置里给「AmapLink」开启位置权限）")
        }
        val manager = context.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            ?: return Fix(false, null, "unknown", "设备不支持定位服务")

        val budget = if (timeoutMs > 0) timeoutMs.toLong() else 20000L
        val deadline = System.currentTimeMillis() + budget
        var lastError = ""

        // 服务端明说不要高精度、且缓存还新鲜：直接用缓存，省电省时间
        if (!highAccuracy && maxAgeMs > 0) {
            val cached = lastKnown(manager)
            if (cached != null && System.currentTimeMillis() - cached.time <= maxAgeMs) {
                return Fix(true, cached, shortProvider(cached.provider ?: "passive"), "")
            }
        }

        // 1) GPS，2) 网络；两者都失败才退缓存。两个 provider 共用同一份超时预算。
        for (provider in arrayOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
            val remain = deadline - System.currentTimeMillis()
            if (remain <= 0) {
                lastError = "定位超时（" + (budget / 1000) + "s）"
                break
            }
            val enabled = try {
                manager.isProviderEnabled(provider)
            } catch (e: Exception) {
                false
            }
            if (!enabled) {
                lastError = providerName(provider) + " 未开启"
                continue
            }
            val location = try {
                requestOnce(manager, provider, remain)
            } catch (e: SecurityException) {
                return Fix(false, null, "unknown", "定位权限被拒绝：" + (e.message ?: ""))
            } catch (e: Exception) {
                null
            }
            if (location != null) {
                return Fix(true, location, shortProvider(location.provider ?: provider), "")
            }
            lastError = providerName(provider) + " 超时（" + (remain / 1000) + "s）"
        }

        // 3) 退到系统缓存的最后位置
        val last = lastKnown(manager)
        if (last != null) {
            val age = System.currentTimeMillis() - last.time
            val expired = maxAgeMs > 0 && age > maxAgeMs
            val note = if (expired) {
                "定位超时，回退到过期缓存（age=" + (age / 1000) + "s，max_age=" + (maxAgeMs / 1000) + "s）"
            } else {
                ""
            }
            return Fix(true, last, shortProvider(last.provider ?: "passive"), note)
        }

        val reason = if (lastError.isEmpty()) "没有可用的定位来源" else lastError
        return Fix(false, null, "unknown", "定位失败：" + reason)
    }

    /** 从各 provider 里挑最新的一个缓存位置。 */
    private fun lastKnown(manager: LocationManager): Location? {
        var best: Location? = null
        for (provider in arrayOf(
            LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER,
            LocationManager.PASSIVE_PROVIDER
        )) {
            val location = try {
                manager.getLastKnownLocation(provider)
            } catch (e: Exception) {
                null
            } ?: continue
            if (best == null || location.time > best.time) {
                best = location
            }
        }
        return best
    }

    /**
     * 用 requestLocationUpdates 等一次回调，超时就放弃。
     * 回调需要一个 Looper，这里临时起一个 HandlerThread（服务的工作线程没有 Looper）。
     */
    private fun requestOnce(manager: LocationManager, provider: String, timeoutMs: Long): Location? {
        val thread = HandlerThread("amap-locate-" + provider)
        thread.start()
        val looper: Looper = thread.looper
        val latch = CountDownLatch(1)
        // 回调线程写、当前线程在 latch 之后读，CountDownLatch 已经保证了可见性
        val holder = arrayOfNulls<Location>(1)

        val listener = object : LocationListener {
            override fun onLocationChanged(location: Location) {
                holder[0] = location
                latch.countDown()
            }

            @Deprecated("API 29 起废弃，低版本系统仍会回调")
            override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit

            override fun onProviderEnabled(provider: String) = Unit

            override fun onProviderDisabled(provider: String) {
                latch.countDown()
            }
        }

        try {
            manager.requestLocationUpdates(provider, 0L, 0f, listener, looper)
            val got = latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            if (!got) {
                return null
            }
            return holder[0]
        } finally {
            try {
                manager.removeUpdates(listener)
            } catch (e: Exception) {
                // 忽略
            }
            thread.quitSafely()
        }
    }

    private fun hasPermission(context: Context): Boolean {
        val fine = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION)
        val coarse = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION)
        return fine == PackageManager.PERMISSION_GRANTED || coarse == PackageManager.PERMISSION_GRANTED
    }

    /** 契约里的 provider 只能是 gps / network / passive / unknown。 */
    private fun shortProvider(provider: String): String {
        return when (provider) {
            LocationManager.GPS_PROVIDER -> "gps"
            LocationManager.NETWORK_PROVIDER -> "network"
            LocationManager.PASSIVE_PROVIDER -> "passive"
            else -> "unknown"
        }
    }

    private fun providerName(provider: String): String {
        return if (provider == LocationManager.GPS_PROVIDER) "GPS" else "网络定位"
    }
}
