package com.away.amap

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager

/**
 * 前台服务：常驻通知 + 定时 GET /amap/poll。
 * 拿到 request 就用 LocationManager 取一次定位，再 POST /amap/report（失败也报）。
 * 前台服务的作用是防后台被杀；国产 ROM 还需要手动给「自启动 + 无限制后台」。
 */
class LocateService : Service() {

    private lateinit var prefs: Prefs
    private lateinit var api: ApiClient

    @Volatile
    private var running = false

    private var worker: Thread? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        api = ApiClient(prefs)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        try {
            startForeground(NOTIFY_ID, buildNotification("正在连接 " + prefs.baseUrl))
        } catch (e: Exception) {
            // Android 12+ 在极端情况下（系统后台重启带 START_STICKY 的服务）会拒绝启动前台服务，
            // 这里兜一下，别让进程直接崩掉。
            LocateBus.log("启动前台服务失败：" + (e.message ?: e.toString()))
            LocateBus.update(running = false, serviceStatus = "启动前台服务被系统拒绝，请手动再点一次「启动服务」")
            stopSelf()
            return START_NOT_STICKY
        }
        if (!running) {
            running = true
            LocateBus.update(
                running = true,
                serviceStatus = "轮询中 " + prefs.baseUrl + " 每 " + prefs.pollSeconds + "s",
                deviceId = prefs.deviceId,
                baseUrl = prefs.baseUrl
            )
            LocateBus.log("服务已启动 device_id=" + prefs.deviceId + " → " + prefs.baseUrl)
            val thread = Thread({ loop() }, "amap-poll")
            thread.isDaemon = true
            worker = thread
            thread.start()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        worker = null
        LocateBus.update(running = false, serviceStatus = "服务已停止")
        LocateBus.log("服务已停止")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ---------------- 轮询主循环 ----------------

    /** 单线程循环：一轮结束后再等 poll_seconds，避免请求堆积。 */
    private fun loop() {
        while (running) {
            val startedAt = System.currentTimeMillis()
            try {
                tick()
            } catch (e: Exception) {
                LocateBus.log("轮询异常：" + (e.message ?: e.toString()))
            }
            val period = prefs.pollSeconds.coerceAtLeast(3) * 1000L
            val waitMs = period - (System.currentTimeMillis() - startedAt)
            if (waitMs > 0 && running) {
                try {
                    Thread.sleep(waitMs)
                } catch (e: InterruptedException) {
                    break
                }
            }
        }
    }

    /** 一次轮询：没有 request 就空转，有 request 就取点并上报。 */
    private fun tick() {
        val result = api.pollSync()
        if (!result.ok) {
            val why = if (result.error.isNotEmpty()) result.error else "HTTP 错误"
            val text = "轮询失败：" + why + (if (result.body.isEmpty()) "" else " / " + result.body.take(120))
            LocateBus.update(serviceStatus = text)
            notify(text)
            return
        }
        val json = result.json()
        if (json == null) {
            val text = "轮询返回不是合法 JSON：" + result.body.take(120)
            LocateBus.update(serviceStatus = text)
            return
        }
        val node = json.optJSONObject("request")
        if (node == null) {
            val text = "轮询正常 " + LocateBus.nowText() + "（无待办）"
            LocateBus.update(serviceStatus = text)
            notify("待命中，每 " + prefs.pollSeconds + "s 轮询一次")
            return
        }

        val request = LocateRequest.from(node)
        LocateBus.update(serviceStatus = "收到定位请求 " + request.id + "：" + request.reason)
        LocateBus.log(
            "收到定位请求 id=" + request.id + " reason=" + request.reason +
                " timeout=" + request.timeoutMs + "ms max_age=" + request.maxAgeMs + "ms" +
                " high_accuracy=" + request.highAccuracy
        )
        notify("正在定位：" + request.reason)

        // 取点 + 上报期间持有唤醒锁（超时保护：就算卡住也会自动释放）
        val holdMs = request.timeoutMs + 30000L
        withWakeLock(holdMs) {
            Reporter.reportOnce(this, prefs, api, request)
        }
    }

    // ---------------- 工具 ----------------

    private fun withWakeLock(timeoutMs: Long, block: () -> Unit) {
        val manager = getSystemService(Context.POWER_SERVICE) as? PowerManager
        val lock = manager?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "amap:locate")
        try {
            lock?.acquire(timeoutMs)
        } catch (e: Exception) {
            // 拿不到锁也继续，不影响功能
        }
        try {
            block()
        } finally {
            try {
                if (lock != null && lock.isHeld) {
                    lock.release()
                }
            } catch (e: Exception) {
                // 忽略
            }
        }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val channel = NotificationChannel(CHANNEL_ID, "定位通道", NotificationManager.IMPORTANCE_LOW)
            channel.description = "Dejiko定位 后台轮询定位请求"
            manager.createNotificationChannel(channel)
        }
    }

    private fun pendingIntent(): PendingIntent {
        val intent = Intent(this, MainActivity::class.java)
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }
        return PendingIntent.getActivity(this, 0, intent, flags)
    }

    private fun buildNotification(text: String): Notification {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("Dejiko定位运行中")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_locate)
            .setContentIntent(pendingIntent())
            .setOngoing(true)
            .build()
    }

    /** 刷新常驻通知的状态文字。 */
    private fun notify(text: String) {
        try {
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.notify(NOTIFY_ID, buildNotification(text))
        } catch (e: Exception) {
            // 通知权限被拒时忽略
        }
    }

    companion object {
        const val CHANNEL_ID = "amap_locate"
        const val NOTIFY_ID = 6310

        fun start(context: Context) {
            val intent = Intent(context, LocateService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, LocateService::class.java))
        }

        /** 服务是否在跑（界面用来显示状态、也用于判断要不要拉起权限）。 */
        fun isRunning(): Boolean = LocateBus.state().running
    }
}
