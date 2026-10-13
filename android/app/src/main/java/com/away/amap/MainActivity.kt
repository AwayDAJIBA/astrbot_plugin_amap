package com.away.amap

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * 主界面：配置 + 状态 + 日志。
 * 配置存本地，网络请求都在单线程池里跑，回到 UI 线程刷新。
 */
class MainActivity : AppCompatActivity(), LocateBus.Listener {

    private lateinit var prefs: Prefs
    private lateinit var api: ApiClient
    private val worker: ExecutorService = Executors.newSingleThreadExecutor()

    private lateinit var editHost: EditText
    private lateinit var editPort: EditText
    private lateinit var editToken: EditText
    private lateinit var editDeviceName: EditText
    private lateinit var editDeviceId: EditText
    private lateinit var editPoll: EditText

    private lateinit var textDeviceLine: TextView
    private lateinit var textService: TextView
    private lateinit var textLastReport: TextView
    private lateinit var textServer: TextView
    private lateinit var textLog: TextView
    private lateinit var scrollLog: ScrollView
    private lateinit var viewDot: View

    /** 拿到权限后要接着做的事（启动服务 / 立即上报）。 */
    private var pendingAction: (() -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = Prefs(this)
        api = ApiClient(prefs)

        editHost = findViewById(R.id.editHost)
        editPort = findViewById(R.id.editPort)
        editToken = findViewById(R.id.editToken)
        editDeviceName = findViewById(R.id.editDeviceName)
        editDeviceId = findViewById(R.id.editDeviceId)
        editPoll = findViewById(R.id.editPoll)

        textDeviceLine = findViewById(R.id.textDeviceLine)
        textService = findViewById(R.id.textService)
        textLastReport = findViewById(R.id.textLastReport)
        textServer = findViewById(R.id.textServer)
        textLog = findViewById(R.id.textLog)
        scrollLog = findViewById(R.id.scrollLog)
        viewDot = findViewById(R.id.viewDot)

        fillFromPrefs()

        findViewById<Button>(R.id.btnSave).setOnClickListener {
            saveFromUi()
            LocateBus.log("已保存配置：" + prefs.baseUrl + "  device_id=" + prefs.deviceId)
            updateStatusViews()
        }
        findViewById<Button>(R.id.btnPing).setOnClickListener {
            saveFromUi()
            doPing()
        }
        findViewById<Button>(R.id.btnReportNow).setOnClickListener {
            saveFromUi()
            ensurePermissions { reportNow() }
        }
        findViewById<Button>(R.id.btnStart).setOnClickListener {
            saveFromUi()
            ensurePermissions { startServiceNow() }
        }
        findViewById<Button>(R.id.btnStop).setOnClickListener {
            LocateService.stop(this)
            LocateBus.update(running = false, serviceStatus = "服务已停止")
        }
        findViewById<Button>(R.id.btnClearLog).setOnClickListener {
            LocateBus.clearLogs()
            textLog.text = ""
        }
    }

    override fun onResume() {
        super.onResume()
        fillFromPrefs()
        // register 会把缓存的状态和日志重放一遍，所以先清空再注册，避免重复
        textLog.text = ""
        LocateBus.register(this)
    }

    override fun onPause() {
        LocateBus.unregister(this)
        super.onPause()
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }

    // ---------------- LocateBus 回调 ----------------

    override fun onState(state: LocateBus.State) {
        runOnUiThread {
            textService.text = state.serviceStatus
            textLastReport.text = state.lastReport
            textServer.text = state.serverMessage
            setRunning(state.running)
        }
    }

    override fun onLog(line: String) {
        runOnUiThread {
            textLog.append(line + "\n")
            scrollLog.post { scrollLog.fullScroll(View.FOCUS_DOWN) }
        }
    }

    // ---------------- 网络动作 ----------------

    /** 测试连接：GET /amap/ping */
    private fun doPing() {
        val target = prefs.baseUrl
        LocateBus.log("[测试] GET " + target + "/amap/ping")
        api.ping { result ->
            runOnUiThread {
                val json = result.json()
                if (result.ok && json != null) {
                    setDot(true)
                    val text = "连接成功：" + json.optString("name", "?") +
                        " v" + json.optString("version", "?") +
                        "  poll_interval=" + json.optInt("poll_interval", 0) + "s" +
                        "  设备已登记=" + json.optBoolean("device_known", false) +
                        "  定位就绪=" + json.optBoolean("locate_ready", false)
                    textServer.text = text
                    LocateBus.log("[测试] " + text)
                } else {
                    setDot(false)
                    val text = "连接失败：" + (if (result.error.isNotEmpty()) result.error else "响应异常") +
                        (if (result.body.isEmpty()) "" else " / " + result.body.take(200))
                    textServer.text = text
                    LocateBus.log("[测试] " + text)
                }
            }
        }
    }

    /** 立即上报一次位置（不带 request_id）。 */
    private fun reportNow() {
        LocateBus.log("[手动] 取一次定位并上报…")
        worker.execute {
            try {
                Reporter.reportOnce(this, prefs, api, null)
            } catch (e: Exception) {
                runOnUiThread { LocateBus.log("[手动] 失败：" + (e.message ?: "")) }
            }
        }
    }

    private fun startServiceNow() {
        if (!hasLocationPermission()) {
            LocateBus.log("[服务] 没有定位权限，Android 14 不允许启动定位类型前台服务，已取消")
            return
        }
        LocateService.start(this)
        LocateBus.update(running = true, serviceStatus = "正在启动…")
        LocateBus.log("[服务] 已请求启动前台服务")
    }

    // ---------------- 配置读写 ----------------

    private fun fillFromPrefs() {
        editHost.setText(prefs.host)
        editPort.setText(prefs.port.toString())
        editToken.setText(prefs.token)
        editDeviceName.setText(prefs.deviceName)
        editDeviceId.setText(prefs.deviceId)
        editPoll.setText(prefs.pollSeconds.toString())
        textDeviceLine.text = prefs.deviceName + "  " + prefs.deviceId
        updateStatusViews()
    }

    private fun saveFromUi() {
        prefs.host = editHost.text.toString().trim()
        prefs.port = editPort.text.toString().trim().toIntOrNull() ?: 6310
        prefs.token = editToken.text.toString().trim()
        prefs.deviceName = editDeviceName.text.toString().trim().ifEmpty { Build.MODEL ?: "android" }
        val typedId = editDeviceId.text.toString().trim()
        if (typedId.isNotEmpty()) {
            prefs.deviceId = typedId
        }
        var poll = editPoll.text.toString().trim().toIntOrNull() ?: 15
        if (poll < 3) {
            poll = 3
        }
        prefs.pollSeconds = poll
        editPoll.setText(poll.toString())
        textDeviceLine.text = prefs.deviceName + "  " + prefs.deviceId
        updateStatusViews()
        if (prefs.isFullUrlMode) {
            LocateBus.log("已按「完整 URL」使用，端口框不生效 → " + prefs.baseUrl)
        }
    }

    private fun updateStatusViews() {
        val state = LocateBus.state()
        textService.text = if (state.running) state.serviceStatus else "服务未启动"
        textLastReport.text = state.lastReport
        textServer.text = state.serverMessage
        setRunning(state.running)
    }

    private fun setRunning(running: Boolean) {
        viewDot.background.setTint(
            getColor(if (running) R.color.am_ok else R.color.am_warn)
        )
    }

    private fun setDot(online: Boolean) {
        viewDot.background.setTint(getColor(if (online) R.color.am_ok else R.color.am_bad))
    }

    // ---------------- 权限 ----------------

    /** 需要时先申请定位 + 通知权限，拿到（或被拒）后继续执行 action。 */
    private fun ensurePermissions(action: () -> Unit) {
        val needed = ArrayList<String>()
        if (!hasLocationPermission()) {
            needed.add(Manifest.permission.ACCESS_FINE_LOCATION)
            needed.add(Manifest.permission.ACCESS_COARSE_LOCATION)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isEmpty()) {
            action()
            return
        }
        pendingAction = action
        requestPermissions(needed.toTypedArray(), REQ_PERMISSIONS)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_PERMISSIONS) {
            return
        }
        if (!hasLocationPermission()) {
            LocateBus.log("定位权限被拒绝：请到「设置 → 应用 → Dejiko定位 → 权限」里手动开启位置权限")
        }
        val action = pendingAction
        pendingAction = null
        action?.invoke()
    }

    private fun hasLocationPermission(): Boolean {
        val fine = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
        val coarse = checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
        return fine == PackageManager.PERMISSION_GRANTED || coarse == PackageManager.PERMISSION_GRANTED
    }

    companion object {
        private const val REQ_PERMISSIONS = 1001
    }
}
