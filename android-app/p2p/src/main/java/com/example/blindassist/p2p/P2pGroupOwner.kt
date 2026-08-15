package com.example.blindassist.p2p

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.wifi.WifiManager
import android.net.wifi.p2p.WifiP2pGroup
import android.net.wifi.p2p.WifiP2pInfo
import android.net.wifi.p2p.WifiP2pManager
import android.net.wifi.p2p.nsd.WifiP2pDnsSdServiceInfo
import android.provider.Settings
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

/**
 * 手机端 Wi-Fi Direct Group Owner（服务端角色）。
 *
 * 职责：
 * - createGroup 成为 GO（残留旧组时先 removeGroup 再重建）；
 * - 注册 DNS-SD 本地服务 `_linksee._tcp`，供眼镜端服务发现定位本机；
 * - 组就绪后回调 [Listener.onGroupReady]，携带组网络名与 GO 地址
 *   （标准 P2P GO 地址为 192.168.49.1，即眼镜端 TCP 目标）；
 * - stop 或组被外部拆除时统一清理。
 *
 * 线程：WifiP2pManager 的调用与回调全部钉在主线程 Handler 上。
 */
class P2pGroupOwner(
    private val context: Context,
    private val listener: Listener
) {

    interface Listener {
        fun onState(message: String)
        fun onGroupReady(networkName: String?, ownerAddress: InetAddress?)
        fun onGroupRemoved(reason: String)
        fun onError(error: Throwable)
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val manager: WifiP2pManager? =
        context.getSystemService(Context.WIFI_P2P_SERVICE) as? WifiP2pManager
    private var channel: WifiP2pManager.Channel? = null
    private var started = false
    @Volatile private var groupActive = false
    @Volatile private var creating = false
    private var watchdogTask: Runnable? = null
    private var discoveryServerThread: Thread? = null
    @Volatile private var currentGroup: WifiP2pGroup? = null

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                WifiP2pManager.WIFI_P2P_STATE_CHANGED_ACTION -> {
                    val state = intent.getIntExtra(WifiP2pManager.EXTRA_WIFI_STATE, -1)
                    if (state != WifiP2pManager.WIFI_P2P_STATE_ENABLED && started) {
                        groupActive = false
                        listener.onState("Wi-Fi Direct 已被系统关闭，请开启 Wi-Fi")
                        listener.onGroupRemoved("Wi-Fi Direct 被系统关闭")
                    }
                }
                WifiP2pManager.WIFI_P2P_CONNECTION_CHANGED_ACTION -> {
                    val info = intent.p2pInfo()
                    if (info != null && !info.groupFormed && groupActive && started) {
                        groupActive = false
                        listener.onState("P2P 组已被外部拆除")
                        listener.onGroupRemoved("P2P 组被拆除")
                    }
                }
            }
        }
    }

    fun start() {
        val mgr = manager ?: run {
            listener.onError(IllegalStateException("设备不支持 Wi-Fi Direct"))
            return
        }
        if (started) return
        started = true
        val ch = mgr.initialize(context, mainHandler.looper) {
            Log.w(TAG, "channel 失效")
            listener.onError(IllegalStateException("Wi-Fi Direct channel 失效"))
        }
        if (ch == null) {
            started = false
            listener.onError(IllegalStateException("Wi-Fi Direct 初始化失败"))
            return
        }
        channel = ch
        val filter = IntentFilter().apply {
            addAction(WifiP2pManager.WIFI_P2P_STATE_CHANGED_ACTION)
            addAction(WifiP2pManager.WIFI_P2P_CONNECTION_CHANGED_ACTION)
        }
        runCatching { context.registerReceiver(receiver, filter) }
        listener.onState("Wi-Fi Direct 正在开启")
        trySetDeviceNameViaSettings()
        startDiscoveryServer()
        scheduleWatchdog(ch)
    }

    fun stop() {
        if (!started) return
        started = false
        groupActive = false
        creating = false
        val ch = channel
        channel = null
        runCatching { context.unregisterReceiver(receiver) }
        watchdogTask?.let { mainHandler.removeCallbacks(it) }
        watchdogTask = null
        discoveryServerThread?.interrupt()
        discoveryServerThread = null
        currentGroup = null
        val mgr = manager
        if (ch != null && mgr != null) {
            runCatching { mgr.clearLocalServices(ch, null) }
            runCatching { mgr.removeGroup(ch, null) }
        }
    }

    /**
     * 尽力把手机端 P2P 设备名写成 LinkSee-Phone（部分机型设置页会读取）。
     * 反射调用隐藏 setDeviceName 在三星上会被 NETWORK_SETTINGS 权限拦截并可能
     * 连带让 createGroup 被 Ignored，因此这里只写 Settings，不反射。
     */
    private fun trySetDeviceNameViaSettings() {
        runCatching {
            Settings.Global.putString(
                context.contentResolver,
                "wifi_p2p_device_name",
                P2pConstants.DEVICE_NAME
            )
        }
    }

    /**
     * 组自愈看门狗：三星等系统可能在无客户端时拆掉 GO 组，且连接变化广播
     * 被 NETWORK_SETTINGS 权限拦截收不到，只能轮询 requestGroupInfo 兜底。
     * 每 5 秒检查一次：有 GO 组则确保就绪，没有则先清残留再重建。
     */
    private fun scheduleWatchdog(ch: WifiP2pManager.Channel) {
        watchdogTask?.let { mainHandler.removeCallbacks(it) }
        val task = Runnable {
            watchdogTask = null
            if (!started) return@Runnable
            val mgr = manager ?: return@Runnable
            runCatching {
                mgr.requestGroupInfo(ch) { group ->
                    if (!started) return@requestGroupInfo
                    if (creating) {
                        scheduleWatchdog(ch)
                        return@requestGroupInfo
                    }
                    val alive = group != null && group.isGroupOwner
                    if (alive) {
                        if (!groupActive) {
                            Log.i(TAG, "检测到 GO 组：${group?.networkName}，接入")
                            onGroupInfo(ch, group)
                        }
                        scheduleWatchdog(ch)
                    } else {
                        Log.w(TAG, "无有效 GO 组，清残留并重建")
                        listener.onState("Wi-Fi Direct 组缺失，正在重建")
                        createGroupCleaned(ch)
                    }
                }
            }.onFailure {
                Log.w(TAG, "requestGroupInfo 失败：${it.message}")
                if (started) {
                    createGroupCleaned(ch)
                }
            }
        }
        watchdogTask = task
        mainHandler.postDelayed(task, OWNER_WATCHDOG_MS)
    }

    /** 先 removeGroup 清掉残留组/接口，再创建新组（避免 createGroup 被 Ignored）。 */
    private fun createGroupCleaned(ch: WifiP2pManager.Channel) {
        if (!started) return
        creating = true
        // 三星等实现可能让 createGroup 回调永远不触发（被 Ignored），
        // 用超时兜底：到时重置状态，由看门狗继续重试。
        mainHandler.postDelayed({
            if (creating && started) {
                Log.w(TAG, "createGroup 超时未回调，重置创建状态")
                creating = false
                scheduleWatchdog(channel ?: return@postDelayed)
            }
        }, CREATE_TIMEOUT_MS)
        val mgr = manager ?: return
        runCatching {
            mgr.removeGroup(ch, object : WifiP2pManager.ActionListener {
                override fun onSuccess() = createGroupWithRetry(ch, retried = false)
                override fun onFailure(reason: Int) = createGroupWithRetry(ch, retried = false)
            })
        }.onFailure {
            createGroupWithRetry(ch, retried = false)
        }
    }

    private fun createGroupWithRetry(ch: WifiP2pManager.Channel, retried: Boolean) {
        if (!started) return
        val mgr = manager ?: return
        runCatching {
            mgr.createGroup(ch, object : WifiP2pManager.ActionListener {
                override fun onSuccess() {
                    creating = false
                    Log.i(TAG, "createGroup 成功")
                    listener.onState("Wi-Fi Direct 组已建立")
                    requestGroupInfo(ch)
                }

                override fun onFailure(reason: Int) {
                    creating = false
                    Log.w(TAG, "createGroup 失败（reason=$reason），由看门狗重试")
                    if (!retried && reason == WifiP2pManager.BUSY) {
                        createGroupCleaned(ch)
                    } else if (reason == WifiP2pManager.P2P_UNSUPPORTED) {
                        listener.onError(IllegalStateException("设备不支持 Wi-Fi Direct"))
                    }
                }
            })
        }.onFailure {
            creating = false
            listener.onError(it)
        }
    }

    private fun requestGroupInfo(ch: WifiP2pManager.Channel) {
        val mgr = manager ?: return
        runCatching {
            mgr.requestGroupInfo(ch) { group -> onGroupInfo(ch, group) }
        }.onFailure { listener.onError(it) }
    }

    private fun onGroupInfo(ch: WifiP2pManager.Channel, group: WifiP2pGroup?) {
        if (group == null || !group.isGroupOwner) {
            listener.onError(IllegalStateException("P2P 组未建立或本机不是 GO"))
            return
        }
        val networkName = group.networkName
        currentGroup = group
        listener.onState("Wi-Fi Direct 组就绪：$networkName")
        registerLocalService(ch)
        val mgr = manager ?: return
        runCatching {
            mgr.requestConnectionInfo(ch) { info -> onConnectionInfo(networkName, info) }
        }.onFailure { listener.onError(it) }
    }

    private fun onConnectionInfo(networkName: String?, info: WifiP2pInfo) {
        if (!info.groupFormed || !info.isGroupOwner) {
            listener.onError(IllegalStateException("本机不是 P2P group owner"))
            return
        }
        groupActive = true
        listener.onState("P2P GO 地址：${info.groupOwnerAddress?.hostAddress}")
        listener.onGroupReady(networkName, info.groupOwnerAddress)
        scheduleWatchdog(channel ?: return)
    }

    private fun registerLocalService(ch: WifiP2pManager.Channel) {
        val mgr = manager ?: return
        val serviceInfo = WifiP2pDnsSdServiceInfo.newInstance(
            P2pConstants.SERVICE_INSTANCE,
            P2pConstants.SERVICE_TYPE,
            mapOf(
                "port" to P2pConstants.TCP_PORT.toString(),
                "model" to (Build.MODEL ?: "phone")
            )
        )
        runCatching {
            mgr.addLocalService(ch, serviceInfo, object : WifiP2pManager.ActionListener {
                override fun onSuccess() {
                    listener.onState("已发布 LinkSee 服务，等待眼镜加入")
                }

                override fun onFailure(reason: Int) {
                    Log.w(TAG, "addLocalService 失败：$reason")
                }
            })
        }.onFailure { Log.w(TAG, "addLocalService 异常：${it.message}") }
    }

    /**
     * UDP 发现服务：监听眼镜端的扫网 WHO 请求，组就绪后单播回复 P2P 凭据。
     * （UDP 广播在两端都被系统策略拦截，单播是实测可用的通道。）
     */
    private fun startDiscoveryServer() {
        val thread = Thread({
            runCatching {
                val socket = DatagramSocket(P2pConstants.CREDENTIAL_PORT)
                socket.soTimeout = 1_000
                val buffer = ByteArray(256)
                while (started && !Thread.currentThread().isInterrupted) {
                    try {
                        val packet = DatagramPacket(buffer, buffer.size)
                        socket.receive(packet)
                        handleWhoRequest(socket, packet)
                    } catch (e: java.net.SocketTimeoutException) {
                        // 继续等待
                    } catch (e: Exception) {
                        if (started) Log.w(TAG, "发现服务异常：${e.message}")
                        break
                    }
                }
                socket.close()
            }
        }, "p2p-credential-server").apply {
            isDaemon = true
        }
        discoveryServerThread = thread
        thread.start()
    }

    private fun handleWhoRequest(socket: DatagramSocket, packet: DatagramPacket) {
        val message = String(packet.data, 0, packet.length, Charsets.UTF_8)
        if (!message.startsWith(P2pConstants.WHO_PREFIX)) return
        if (!started) return
        // 附带手机局域网 IP：眼镜端 addNetwork 免弹窗加入失败时可直接走 LAN TCP。
        val lanIp = runCatching {
            val wifiManager =
                context.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val ip = wifiManager.connectionInfo?.ipAddress ?: 0
            if (ip == 0) "" else {
                "${ip and 0xFF}.${(ip shr 8) and 0xFF}." +
                    "${(ip shr 16) and 0xFF}.${(ip shr 24) and 0xFF}"
            }
        }.getOrDefault("")
        // P2P 组可能在重建（三星 GO 不稳定），组不在时 SSID/密码留空，
        // 眼镜端仍可用局域网 IP 走 LAN 兜底，不依赖 P2P 组状态。
        val group = currentGroup
        val ssid = group?.networkName.orEmpty()
        val passphrase = if (group != null && groupActive) group.passphrase else ""
        val reply = P2pConstants.CREDENTIAL_PREFIX +
            "$ssid|$passphrase|${P2pConstants.TCP_PORT}|$lanIp"
        val data = reply.toByteArray(Charsets.UTF_8)
        runCatching {
            socket.send(DatagramPacket(data, data.size, packet.address, packet.port))
        }.onFailure { Log.w(TAG, "凭据回复失败：${it.message}") }
    }

    private fun Intent.p2pInfo(): WifiP2pInfo? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getParcelableExtra(WifiP2pManager.EXTRA_WIFI_P2P_INFO, WifiP2pInfo::class.java)
        } else {
            @Suppress("DEPRECATION")
            getParcelableExtra(WifiP2pManager.EXTRA_WIFI_P2P_INFO)
        }

    companion object {
        private const val TAG = "P2pGroupOwner"
        private const val OWNER_WATCHDOG_MS = 5_000L
        private const val CREATE_TIMEOUT_MS = 10_000L
    }
}
