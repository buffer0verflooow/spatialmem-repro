package com.example.blindassist.glasses

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.WindowManager
import android.widget.TextView
import com.example.blindassist.p2p.P2pPermissions

/**
 * 眼镜端唯一 Activity：极简状态界面，**不 finish**，保持前台存活。
 *
 * docs/archive/工单-M1-03-打回4-Activity不能finish.md 第 3 节：Activity 一旦 finish，进程掉后台、相机会被系统按
 * 进程优先级仲裁收走（真机证据：`score 50 state 4` 打不过任何前台 Activity）。
 * 因此这里保留一个纯色背景 + 一行状态文字的极简界面（盲人不看，但这是换取相机
 * 优先级的必要代价），并加 `FLAG_KEEP_SCREEN_ON` 避免锁屏把进程清掉。
 *
 * 职责：按 Intent extra `host`（可选，USB 隧道/录制调试直连用）确定固定目标 IP，
 * 申请 Wi-Fi Direct 权限后启动前台服务，并保持在前台。相机开关跟随
 * [GlassLinkService] 生命周期，不跟随本 Activity 的 onPause/onResume
 * （否则系统弹个通知就断流）。
 */
class GlassLinkActivity : Activity() {

    private var lastStatusText: String? = null
    private val statusHandler = Handler(Looper.getMainLooper())
    private val statusRunnable = object : Runnable {
        override fun run() {
            val text = GlassLinkService.currentStatus
            if (text != null && text != lastStatusText) {
                lastStatusText = text
                findViewById<TextView>(R.id.status_text)?.text = text
            }
            statusHandler.postDelayed(this, STATUS_POLL_INTERVAL_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 保持屏幕常亮：实测未佩戴时约 30 秒会切到 LockScreenActivity，进程随后被清。
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_glass_link)

        findViewById<TextView>(R.id.status_text).text =
            getString(R.string.status_collecting)

        if (requiredPermissions().all {
                checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
            }
        ) {
            startGlassLinkService()
        } else {
            requestPermissions(requiredPermissions(), REQUEST_START_PERMISSIONS)
        }
    }

    override fun onStart() {
        super.onStart()
        statusHandler.post(statusRunnable)
    }

    override fun onStop() {
        statusHandler.removeCallbacks(statusRunnable)
        super.onStop()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_START_PERMISSIONS) {
            if (checkSelfPermission(Manifest.permission.CAMERA) !=
                PackageManager.PERMISSION_GRANTED
            ) {
                Log.w(TAG, "缺少相机权限，无法启动采集服务")
                findViewById<TextView>(R.id.status_text)?.text =
                    getString(R.string.status_camera_permission_missing)
                return
            }
            if (!P2pPermissions.hasAll(this)) {
                Log.w(TAG, "缺少 Wi-Fi Direct 权限，无法建立链路")
                findViewById<TextView>(R.id.status_text)?.text =
                    getString(R.string.status_wifi_direct_permission_missing)
                return
            }
            startGlassLinkService()
        }
    }

    private fun requiredPermissions(): Array<String> =
        arrayOf(Manifest.permission.CAMERA) + P2pPermissions.required()

    private fun startGlassLinkService() {
        val serviceIntent = Intent(this, GlassLinkService::class.java)
        // 透传 adb `--es host 127.0.0.1`：非空时眼镜直连该 host（m1 USB 隧道录制），
        // 不走 Wi-Fi Direct 搜索（见 GlassLinkService.startLinkTransport）。
        intent.getStringExtra(EXTRA_HOST)?.takeIf { it.isNotBlank() }?.let {
            serviceIntent.putExtra(EXTRA_HOST, it)
        }
        startForegroundService(serviceIntent)
        // 不 finish()：保持前台窗口，换取相机进程优先级（docs/archive/工单-M1-03-打回4-Activity不能finish.md 第 3 节）。
    }

    override fun onDestroy() {
        // 低优先级项：Activity 被系统回收（而服务仍在运行时）自动拉起，保住前台窗口。
        // isChangingConfigurations 排除旋转等配置变更引起的正常重建。
        if (!isChangingConfigurations && GlassLinkService.serviceRunning) {
            Log.w(TAG, "Activity 被销毁但服务仍在运行，重新拉起前台窗口")
            startActivity(
                Intent(this, GlassLinkActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }
        statusHandler.removeCallbacks(statusRunnable)
        super.onDestroy()
    }

    companion object {
        private const val TAG = "GlassLinkActivity"

        /** Intent extra 键，与 adb 的 `--es host 127.0.0.1` 对应。 */
        private const val EXTRA_HOST = "host"
        private const val REQUEST_START_PERMISSIONS = 100
        private const val STATUS_POLL_INTERVAL_MS = 1_000L
    }
}
