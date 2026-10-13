package com.away.amap

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CopyOnWriteArrayList

/**
 * 进程内的状态总线：前台服务把「服务状态 / 最近一次上报 / 服务端消息 / 日志」
 * 广播给正在显示的界面。界面重建时可以先 setState() 拉一次当前值。
 */
object LocateBus {

    /** 界面实现它来接收更新；回调可能来自工作线程，UI 侧记得 runOnUiThread。 */
    interface Listener {
        fun onState(state: State) {}
        fun onLog(line: String) {}
    }

    /** 界面要展示的全部状态。 */
    data class State(
        val running: Boolean = false,
        val serviceStatus: String = "服务未启动",
        val lastReport: String = "还没有上报过",
        val serverMessage: String = "无",
        val deviceId: String = "",
        val baseUrl: String = ""
    )

    private val listeners = CopyOnWriteArrayList<Listener>()
    private val logs = ArrayList<String>()

    @Volatile
    private var state = State()

    fun register(listener: Listener) {
        listeners.addIfAbsent(listener)
        listener.onState(state)
        synchronized(logs) {
            logs.forEach { listener.onLog(it) }
        }
    }

    fun unregister(listener: Listener) {
        listeners.remove(listener)
    }

    fun state(): State = state

    /** 更新状态（只传变化的字段，用 copy 保持其余不变）。 */
    @Synchronized
    fun update(
        running: Boolean = state.running,
        serviceStatus: String = state.serviceStatus,
        lastReport: String = state.lastReport,
        serverMessage: String = state.serverMessage,
        deviceId: String = state.deviceId,
        baseUrl: String = state.baseUrl
    ) {
        state = State(running, serviceStatus, lastReport, serverMessage, deviceId, baseUrl)
        listeners.forEach { it.onState(state) }
    }

    /** 追加一行日志（最多留 200 行）。 */
    fun log(line: String) {
        val text = nowText() + "  " + line
        synchronized(logs) {
            logs.add(text)
            while (logs.size > 200) {
                logs.removeAt(0)
            }
        }
        listeners.forEach { it.onLog(text) }
    }

    fun clearLogs() {
        synchronized(logs) { logs.clear() }
    }

    fun nowText(): String = SimpleDateFormat("MM-dd HH:mm:ss", Locale.US).format(Date())
}
