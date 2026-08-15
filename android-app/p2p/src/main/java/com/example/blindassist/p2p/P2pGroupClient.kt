package com.example.blindassist.p2p

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiConfiguration
import android.net.wifi.WifiManager
import android.net.wifi.p2p.WifiP2pConfig
import android.net.wifi.p2p.WifiP2pDevice
import android.net.wifi.p2p.WifiP2pInfo
import android.net.wifi.p2p.WifiP2pManager
import android.os.SystemClock
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

/**
 * 眼镜端 Wi-Fi Direct 客户端。
 *
 * 职责：
 * - 首选 peer 发现（discoverPeers + requestPeers）定位手机；
 * - connect 加入手机所在的 P2P 组（groupOwnerIntent=0，不竞争 GO）；
 * - 组就绪后拿到 GO 地址（192.168.49.1）与 P2P [Network]，回调
 *   [Listener.onGroupReady] 交给上层建立 TCP 链路；
 * - peer 发现 12 秒内未找到手机时启用局域网单播扫网兜底：手机回复
 *   P2P 组凭据 + 手机局域网 IP，优先 addNetwork 免弹窗加入；仍失败则
 *   回调 [Listener.onFallbackReady] 用手机局域网 IP 直连 TCP（无弹窗）；
 * - 组断开后自动重新发现、重新加入。
 *
 * 线程：WifiP2pManager 调用与回调全部钉在主线程 Handler 上。
 */
class P2pGroupClient(
    private val context: Context,
    private val listener: Listener
) {

    interface Listener {
        fun onState(message: String)
        fun onGroupReady(ownerAddress: InetAddress?, network: Network?)
        /** 已定位到手机并开始 connect（上层应立即取消手动 IP 兜底定时器）。 */
        fun onPeerFound()
        /** connect 失败或超时（上层应恢复手动 IP 兜底）。 */
        fun onPeerConnectFailed()
        /** peer 发现与免弹窗加入都失败时，用手机局域网 IP 直连 TCP。 */
        fun onFallbackReady(host: String)
        fun onGroupLost(reason: String)
        fun onError(error: Throwable)
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val manager: WifiP2pManager? =
        context.getSystemService(Context.WIFI_P2P_SERVICE) as? WifiP2pManager
    private val connectivityManager =
        context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    private var channel: WifiP2pManager.Channel? = null
    private var started = false
    @Volatile private var joined = false
    @Volatile private var connecting = false
    @Volatile private var p2pNetwork: Network? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var rediscoverTask: Runnable? = null
    private var peerPollTask: Runnable? = null
    private var connectTimeoutTask: Runnable? = null
    @Volatile private var peerConnectCooldownUntilMs = 0L
    private var networkRetryCount = 0
    private var credentialListenerThread: Thread? = null
    @Volatile private var joiningDirect = false
    @Volatile private var sweepEnabled = false
    private var fallbackTask: Runnable? = null

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                WifiP2pManager.WIFI_P2P_STATE_CHANGED_ACTION -> {
                    val state = intent.getIntExtra(WifiP2pManager.EXTRA_WIFI_STATE, -1)
                    if (state != WifiP2pManager.WIFI_P2P_STATE_ENABLED && started) {
                        joined = false
                        connecting = false
                        listener.onState("Wi-Fi Direct 已关闭")
                        listener.onGroupLost("Wi-Fi Direct 被系统关闭")
                    }
                }
                WifiP2pManager.WIFI_P2P_CONNECTION_CHANGED_ACTION -> {
                    val info = intent.p2pInfo()
                    if (info != null) handleConnectionInfo(info)
                }
                WifiP2pManager.WIFI_P2P_PEERS_CHANGED_ACTION -> {
                    if (started && !joined && !connecting) requestPeers()
                }
                WifiP2pManager.WIFI_P2P_DISCOVERY_CHANGED_ACTION -> {
                    val state = intent.getIntExtra(WifiP2pManager.EXTRA_DISCOVERY_STATE, -1)
                    if (started && !joined &&
                        state == WifiP2pManager.WIFI_P2P_DISCOVERY_STOPPED
                    ) {
                        scheduleRediscover()
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
        // 清除上一会话残留的组成员身份：App 被强杀/重装时 stop() 不会执行，
        // p2p0 可能仍挂在旧组里，导致新的 P2P 邀请被 GO 以 status=1 拒绝。
        runCatching { mgr.removeGroup(ch, null) }.onFailure {
            Log.w(TAG, "清除残留 P2P 组失败：${it.message}")
        }
        val filter = IntentFilter().apply {
            addAction(WifiP2pManager.WIFI_P2P_STATE_CHANGED_ACTION)
            addAction(WifiP2pManager.WIFI_P2P_CONNECTION_CHANGED_ACTION)
            addAction(WifiP2pManager.WIFI_P2P_DISCOVERY_CHANGED_ACTION)
        }
        runCatching { context.registerReceiver(receiver, filter) }
        listener.onState("Wi-Fi Direct 正在开启")
        startCredentialListener()
        // peer 发现是主路径；12 秒没连上再启用局域网扫网兜底。
        armSweepFallback()
        startPeerDiscovery(ch)
    }

    fun stop() {
        if (!started) return
        started = false
        joined = false
        connecting = false
        rediscoverTask?.let { mainHandler.removeCallbacks(it) }
        rediscoverTask = null
        peerPollTask?.let { mainHandler.removeCallbacks(it) }
        peerPollTask = null
        connectTimeoutTask?.let { mainHandler.removeCallbacks(it) }
        connectTimeoutTask = null
        fallbackTask?.let { mainHandler.removeCallbacks(it) }
        fallbackTask = null
        sweepEnabled = false
        credentialListenerThread?.interrupt()
        credentialListenerThread = null
        joiningDirect = false
        val ch = channel
        channel = null
        runCatching { context.unregisterReceiver(receiver) }
        runCatching {
            networkCallback?.let { connectivityManager.unregisterNetworkCallback(it) }
        }
        networkCallback = null
        p2pNetwork = null
        val mgr = manager
        if (ch != null && mgr != null) {
            runCatching { mgr.stopPeerDiscovery(ch, null) }
            runCatching { mgr.removeGroup(ch, null) }
        }
    }

    private fun startPeerDiscovery(ch: WifiP2pManager.Channel) {
        if (!started || joined || connecting) return
        val mgr = manager ?: return
        listener.onState("正在搜索手机（Wi-Fi Direct）…")
        runCatching {
            mgr.discoverPeers(ch, object : WifiP2pManager.ActionListener {
                override fun onSuccess() {
                    // 框架只在 peer 列表变化时广播 PEERS_CHANGED；若手机在我们
                    // 开始发现前已被登记，广播不会再来，必须主动轮询列表。
                    schedulePeerPoll()
                }
                override fun onFailure(reason: Int) {
                    Log.w(TAG, "discoverPeers 失败：$reason")
                    scheduleRediscover()
                }
            })
        }.onFailure { listener.onError(it) }
    }

    /** 发现期间每 2 秒轮询一次 peer 列表，直到连上或停止。 */
    private fun schedulePeerPoll() {
        peerPollTask?.let { mainHandler.removeCallbacks(it) }
        val task = Runnable {
            peerPollTask = null
            if (!started || joined || connecting) return@Runnable
            requestPeers()
            schedulePeerPoll()
        }
        peerPollTask = task
        mainHandler.postDelayed(task, PEER_POLL_INTERVAL_MS)
    }

    /**
     * 拉取 peer 列表，按设备名包含 LinkSee 过滤手机（P2P 设备名由手机端
     * P2pGroupOwner 设置为 LinkSee-Phone；RayNeo 眼镜不支持 DNS-SD 服务发现）。
     */
    private fun requestPeers() {
        val ch = channel ?: return
        val mgr = manager ?: return
        runCatching {
            mgr.requestPeers(ch) { deviceList ->
                if (!started || joined || connecting) return@requestPeers
                if (SystemClock.elapsedRealtime() < peerConnectCooldownUntilMs) {
                    // P2P connect 刚失败过：冷却期内交给局域网兜底，避免反复重试。
                    return@requestPeers
                }
                val peers = deviceList.deviceList
                val byName = peers.firstOrNull { peer ->
                    peer.deviceName.contains(P2pConstants.PEER_NAME_MARKER, ignoreCase = true)
                }
                // 名字过滤不到时（部分机型无法改 P2P 设备名），回退到 group owner：
                // 家庭场景下附近通常只有一个活跃 GO（即手机）。
                val target = byName ?: peers.firstOrNull { it.isGroupOwner }
                if (target != null) {
                    Log.i(TAG, "发现手机 peer：${target.deviceName}（${target.deviceAddress}）")
                    listener.onState(
                        "发现手机：${target.deviceName}" +
                            if (byName == null) "（GO 兜底）" else ""
                    )
                    connectToPhone(ch, target)
                } else {
                    listener.onState(
                        "未发现 LinkSee 手机（${peers.size} 个设备），继续搜索"
                    )
                }
            }
        }.onFailure { listener.onError(it) }
    }

    private fun connectToPhone(ch: WifiP2pManager.Channel, device: WifiP2pDevice) {
        if (joined || connecting || device.deviceAddress.isEmpty()) return
        connecting = true
        // 已找到手机：取消扫网兜底，避免它中途抢建 LAN 链路。
        sweepEnabled = false
        fallbackTask?.let { mainHandler.removeCallbacks(it) }
        fallbackTask = null
        listener.onPeerFound()
        // 三星 GO 对 P2P 邀请可能直接拒绝（status=1）且不回调，超时兜底。
        connectTimeoutTask?.let { mainHandler.removeCallbacks(it) }
        val timeout = Runnable {
            connectTimeoutTask = null
            if (connecting && !joined && started) {
                Log.w(TAG, "P2P connect 超时，改用局域网兜底")
                connecting = false
                peerConnectCooldownUntilMs =
                    SystemClock.elapsedRealtime() + PEER_CONNECT_COOLDOWN_MS
                listener.onPeerConnectFailed()
                sweepEnabled = true
                scheduleRediscover()
            }
        }
        connectTimeoutTask = timeout
        mainHandler.postDelayed(timeout, CONNECT_TIMEOUT_MS)
        val mgr = manager ?: return
        listener.onState("正在加入手机 P2P 组…")
        val config = WifiP2pConfig().apply {
            deviceAddress = device.deviceAddress
            // 0 = 最不愿意当 GO，保证手机保持 group owner 角色。
            groupOwnerIntent = 0
        }
        runCatching {
            mgr.connect(ch, config, object : WifiP2pManager.ActionListener {
                override fun onSuccess() = Unit
                override fun onFailure(reason: Int) {
                    connecting = false
                    connectTimeoutTask?.let { mainHandler.removeCallbacks(it) }
                    connectTimeoutTask = null
                    Log.w(TAG, "connect 失败：$reason")
                    listener.onState("加入 P2P 组失败（$reason），改用局域网兜底")
                    peerConnectCooldownUntilMs =
                        SystemClock.elapsedRealtime() + PEER_CONNECT_COOLDOWN_MS
                    listener.onPeerConnectFailed()
                    sweepEnabled = true
                    scheduleRediscover()
                }
            })
        }.onFailure {
            connecting = false
            connectTimeoutTask?.let { mainHandler.removeCallbacks(it) }
            connectTimeoutTask = null
            listener.onError(it)
        }
    }

    private fun handleConnectionInfo(info: WifiP2pInfo) {
        if (info.groupFormed) {
            if (joined) return
            joined = true
            connecting = false
            peerConnectCooldownUntilMs = 0L
            joiningDirect = false
            connectTimeoutTask?.let { mainHandler.removeCallbacks(it) }
            connectTimeoutTask = null
            peerPollTask?.let { mainHandler.removeCallbacks(it) }
            peerPollTask = null
            sweepEnabled = false
            fallbackTask?.let { mainHandler.removeCallbacks(it) }
            fallbackTask = null
            val owner = info.groupOwnerAddress
            listener.onState("已加入 P2P 组：${owner?.hostAddress}")
            val ch = channel ?: return
            val mgr = manager ?: return
            runCatching { mgr.stopPeerDiscovery(ch, null) }
            // 组已形成但网络对象/SSID 可能稍后才就绪，轮询等待（官方示例同款重试）。
            networkRetryCount = 0
            waitForP2pNetworkWithRetry(owner)
        } else if (joined || connecting) {
            joined = false
            connecting = false
            joiningDirect = false
            connectTimeoutTask?.let { mainHandler.removeCallbacks(it) }
            connectTimeoutTask = null
            p2pNetwork = null
            listener.onGroupLost("P2P 组连接断开")
            sweepEnabled = true
            scheduleRediscover()
        }
    }

    /**
     * 监听手机端单播回复的 P2P 组凭据（peer 发现超时后的兜底通道）。
     */
    private fun startCredentialListener() {
        val thread = Thread({
            runCatching {
                val socket = DatagramSocket(P2pConstants.CREDENTIAL_PORT)
                socket.soTimeout = 1_000
                val buffer = ByteArray(512)
                var lastSweepMs = 0L
                while (started && !Thread.currentThread().isInterrupted) {
                    // 只在兜底阶段周期扫网：向局域网每个地址单播 WHO。
                    if (sweepEnabled && !joined && !connecting) {
                        val now = SystemClock.elapsedRealtime()
                        if (now - lastSweepMs >= SWEEP_INTERVAL_MS) {
                            lastSweepMs = now
                            sweepForPhone(socket)
                        }
                    }
                    try {
                        val packet = DatagramPacket(buffer, buffer.size)
                        socket.receive(packet)
                        val message = String(
                            packet.data, 0, packet.length, Charsets.UTF_8
                        )
                        handleCredentialMessage(message)
                    } catch (e: SocketTimeoutException) {
                        // 继续等待
                    } catch (e: Exception) {
                        if (started) Log.w(TAG, "凭据监听异常：${e.message}")
                        break
                    }
                }
                socket.close()
            }
        }, "p2p-credential-listener").apply {
            isDaemon = true
        }
        credentialListenerThread = thread
        thread.start()
    }

    /**
     * 向 /24 内除本机外的所有地址单播 LINKSEE_WHO，等手机回复凭据。
     * 一次 253 个小包，3 秒一轮，对家用网络无压力。
     */
    private fun sweepForPhone(socket: DatagramSocket) {
        val wifiManager =
            context.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val ip = wifiManager.connectionInfo?.ipAddress ?: return
        val raw = byteArrayOf(
            (ip and 0xFF).toByte(),
            ((ip shr 8) and 0xFF).toByte(),
            ((ip shr 16) and 0xFF).toByte(),
            ((ip shr 24) and 0xFF).toByte()
        )
        val ownLast = raw[3].toInt() and 0xFF
        val prefix = "${raw[0].toInt() and 0xFF}." +
            "${raw[1].toInt() and 0xFF}.${raw[2].toInt() and 0xFF}"
        val data = P2pConstants.WHO_PREFIX.toByteArray(Charsets.UTF_8)
        for (i in 1..254) {
            if (i == ownLast) continue
            runCatching {
                socket.send(
                    DatagramPacket(
                        data, data.size,
                        InetAddress.getByName("$prefix.$i"),
                        P2pConstants.CREDENTIAL_PORT
                    )
                )
            }
        }
        listener.onState("正在局域网内寻找手机…")
    }

    private fun handleCredentialMessage(message: String) {
        if (!message.startsWith(P2pConstants.CREDENTIAL_PREFIX)) return
        val parts = message.removePrefix(P2pConstants.CREDENTIAL_PREFIX).split("|")
        if (parts.size < 3) return
        val ssid = parts[0]
        val lanIp = parts.getOrNull(3)?.takeIf { it.isNotBlank() }
        if (ssid.isBlank()) {
            // 手机 P2P 组未激活（可能正在重建），直接用局域网 IP 兜底。
            if (lanIp != null) {
                Log.i(TAG, "手机 P2P 组未激活，直接走局域网 IP：$lanIp")
                sweepEnabled = false
                listener.onFallbackReady(lanIp)
            }
            return
        }
        Log.i(TAG, "收到手机 P2P 组凭据：$ssid")
        listener.onState("收到手机 P2P 组，正在加入…")
        joinDirectGroup(ssid, parts[1], lanIp)
    }

    /** 以标准 Wi-Fi 方式加入手机创建的 DIRECT 组（P2P 发现不可用时的替代路径）。 */
    private fun joinDirectGroup(ssid: String, passphrase: String, lanIp: String?) {
        if (joined || joiningDirect) return
        joiningDirect = true
        val wifiManager =
            context.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val config = WifiConfiguration().apply {
            SSID = "\"$ssid\""
            preSharedKey = "\"$passphrase\""
            status = WifiConfiguration.Status.ENABLED
            allowedKeyManagement.set(WifiConfiguration.KeyMgmt.WPA_PSK)
            allowedProtocols.set(WifiConfiguration.Protocol.RSN)
            allowedProtocols.set(WifiConfiguration.Protocol.WPA)
            allowedGroupCiphers.set(WifiConfiguration.GroupCipher.CCMP)
            allowedGroupCiphers.set(WifiConfiguration.GroupCipher.TKIP)
            allowedPairwiseCiphers.set(WifiConfiguration.PairwiseCipher.CCMP)
            allowedPairwiseCiphers.set(WifiConfiguration.PairwiseCipher.TKIP)
        }
        val added = runCatching {
            val netId = wifiManager.addNetwork(config)
            netId >= 0 && wifiManager.enableNetwork(netId, true)
        }.getOrDefault(false)
        if (added) {
            listener.onState("已发起加入 $ssid，等待连接…")
            waitForDirectNetwork(ssid)
        } else {
            Log.w(TAG, "addNetwork 加入失败，lanIp=$lanIp")
            joiningDirect = false
            if (lanIp != null) {
                // 免弹窗兜底：直接用手机局域网 IP 建立 TCP 链路（居家场景）。
                Log.i(TAG, "回退到手机局域网 IP：$lanIp")
                sweepEnabled = false
                fallbackTask?.let { mainHandler.removeCallbacks(it) }
                fallbackTask = null
                listener.onFallbackReady(lanIp)
            } else {
                scheduleRediscover()
            }
        }
    }

    /** 等 DIRECT 组网络可用后回调上层（GO 地址固定 192.168.49.1）。 */
    private fun waitForDirectNetwork(ssid: String) {
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (!started) return
                val wifiManager =
                    context.getSystemService(Context.WIFI_SERVICE) as WifiManager
                val currentSsid = wifiManager.connectionInfo?.ssid
                val onDirect = currentSsid != null &&
                    (currentSsid.trim('"') == ssid || currentSsid.contains("DIRECT-"))
                if (onDirect) {
                    p2pNetwork = network
                    joiningDirect = false
                    joined = true
                    listener.onState("已加入 P2P 组：$ssid")
                    listener.onGroupReady(null, network)
                    runCatching { connectivityManager.unregisterNetworkCallback(this) }
                }
            }
        }
        networkCallback = callback
        runCatching { connectivityManager.registerNetworkCallback(request, callback) }
        // 兜底：10 秒内连不上就重置状态，让凭据/发现流程继续。
        mainHandler.postDelayed({
            if (joiningDirect && !joined) {
                joiningDirect = false
                listener.onState("加入 P2P 组超时，重新等待凭据")
            }
        }, DIRECT_JOIN_TIMEOUT_MS)
    }

    /** peer 发现 12 秒未连上时，启用局域网扫网兜底（主路径无弹窗）。 */
    private fun armSweepFallback() {
        fallbackTask?.let { mainHandler.removeCallbacks(it) }
        val task = Runnable {
            fallbackTask = null
            if (started && !joined) {
                Log.i(TAG, "peer 发现超时，启用局域网扫网兜底")
                sweepEnabled = true
            }
        }
        fallbackTask = task
        mainHandler.postDelayed(task, PEER_DISCOVERY_FALLBACK_DELAY_MS)
    }

    private fun findP2pNetwork(): Network? =
        connectivityManager.allNetworks.firstOrNull { network ->
            isP2pNetwork(network)
        } ?: run {
            // 能力匹配不到时（个别机型的 P2P 网络不暴露 specifier/capability），
            // 用当前连接 SSID 判断。
            val wifiManager =
                context.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val ssid = wifiManager.connectionInfo?.ssid
            if (ssid != null && ssid.contains("DIRECT-")) {
                connectivityManager.allNetworks.firstOrNull { network ->
                    connectivityManager.getNetworkCapabilities(network)
                        ?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
                }
            } else {
                null
            }
        }

    private fun isP2pNetwork(network: Network): Boolean {
        val caps = connectivityManager.getNetworkCapabilities(network) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
            (caps.hasCapability(NET_CAPABILITY_P2P) ||
                caps.networkSpecifier?.toString()?.startsWith("DIRECT") == true)
    }

    private fun waitForP2pNetworkWithRetry(owner: InetAddress?) {
        if (!started) return
        val network = findP2pNetwork()
        if (network != null) {
            networkRetryCount = 0
            p2pNetwork = network
            listener.onState("P2P 网络已就绪：${owner?.hostAddress}")
            listener.onGroupReady(owner, network)
            return
        }
        if (networkRetryCount >= P2P_NETWORK_MAX_RETRIES) {
            networkRetryCount = 0
            Log.w(TAG, "P2P 网络对象未就绪，直接用 GO 地址建立 TCP")
            // 加入 P2P 组后眼镜的默认路由就在 P2P 上，普通 socket 同样可达
            // 192.168.49.1，不必强求 Network 对象。
            listener.onGroupReady(owner, null)
            return
        }
        networkRetryCount++
        mainHandler.postDelayed(
            { waitForP2pNetworkWithRetry(owner) },
            P2P_NETWORK_RETRY_DELAY_MS
        )
    }

    private fun scheduleRediscover() {
        if (!started) return
        rediscoverTask?.let { mainHandler.removeCallbacks(it) }
        val task = Runnable {
            rediscoverTask = null
            if (!started || joined || connecting) return@Runnable
            val ch = channel ?: return@Runnable
            listener.onState("重新搜索手机…")
            startPeerDiscovery(ch)
        }
        rediscoverTask = task
        mainHandler.postDelayed(task, REDISCOVER_DELAY_MS)
    }

    private fun Intent.p2pInfo(): WifiP2pInfo? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getParcelableExtra(WifiP2pManager.EXTRA_WIFI_P2P_INFO, WifiP2pInfo::class.java)
        } else {
            @Suppress("DEPRECATION")
            getParcelableExtra(WifiP2pManager.EXTRA_WIFI_P2P_INFO)
        }

    companion object {
        private const val TAG = "P2pGroupClient"
        private const val REDISCOVER_DELAY_MS = 3_000L
        private const val PEER_POLL_INTERVAL_MS = 2_000L
        private const val SWEEP_INTERVAL_MS = 3_000L
        private const val PEER_DISCOVERY_FALLBACK_DELAY_MS = 12_000L
        private const val P2P_NETWORK_RETRY_DELAY_MS = 500L
        private const val P2P_NETWORK_MAX_RETRIES = 3
        private const val CONNECT_TIMEOUT_MS = 4_000L
        private const val PEER_CONNECT_COOLDOWN_MS = 30_000L
        private const val DIRECT_JOIN_TIMEOUT_MS = 10_000L

        /** NetworkCapabilities.NET_CAPABILITY_P2P 是隐藏常量（值 34），此处显式声明。 */
        private const val NET_CAPABILITY_P2P = 34
    }
}
