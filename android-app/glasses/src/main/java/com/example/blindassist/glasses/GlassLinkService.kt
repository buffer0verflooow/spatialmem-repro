package com.example.blindassist.glasses

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.SurfaceTexture
import android.net.Network
import android.net.wifi.WifiManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.hardware.camera2.params.StreamConfigurationMap
import android.graphics.ImageFormat
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.ImageReader
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Surface
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlin.math.abs
import com.example.blindassist.link.AudioPayloadCodec
import com.example.blindassist.link.ControlCodec
import com.example.blindassist.link.ControlMessage
import com.example.blindassist.link.GlassCapabilities
import com.example.blindassist.link.LinkChannel
import com.example.blindassist.link.LinkEvent
import com.example.blindassist.link.LinkFlags
import com.example.blindassist.link.LinkFrameReader
import com.example.blindassist.link.LinkPacket
import com.example.blindassist.link.LinkProtocol
import com.example.blindassist.link.LinkProtocolException
import com.example.blindassist.link.LinkSendQueue
import com.example.blindassist.link.LinkState
import com.example.blindassist.link.LinkStateMachine
import com.example.blindassist.link.LinkWire
import com.example.blindassist.link.PosePayloadCodec
import com.example.blindassist.link.SessionConfig
import com.example.blindassist.link.TimestampSource
import com.example.blindassist.link.VideoMode
import com.example.blindassist.p2p.P2pConstants
import com.example.blindassist.p2p.P2pGroupClient
import com.example.blindassist.p2p.P2pPermissions
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * 眼镜端采集前台服务：Camera2 (id=0, 640×360@15) → MediaCodec H.264 硬件编码
 * → LinkProtocol 打包 → TCP → 手机。
 *
 * 线程模型（三个线程，职责单一）：
 * - camera HandlerThread：Camera2 会话 + MediaCodec 编码回调（输出 buffer 的拷贝
 *   与 release 都在这一条线程上串行发生，见约束 6）；
 * - link Thread：TCP 连接循环，驱动 [LinkStateMachine] 的退避重连，握手完成后
 *   兼任发送线程（从 [LinkSendQueue] 取包写 socket）；
 * - reader Thread：每会话一个，读 socket 解析下行 CONTROL（HELLO_ACK / PING 等）。
 *
 * 本模块唯一取时间入口是 [nowNs]，视频包时间戳则取自编码器
 * `presentationTimeUs × 1000`（相机 surface 时间戳，与 elapsedRealtimeNanos 同域）。
 */
class GlassLinkService : Service() {

    /** 主线程 Handler，用于启动阶段的退避重试（相机线程此时尚未创建）。 */
    private val mainHandler = Handler(Looper.getMainLooper())

    private var cameraThread: HandlerThread? = null
    private var cameraHandler: Handler? = null
    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var encoder: MediaCodec? = null
    private var encoderInputSurface: Surface? = null
    private var wakeLock: PowerManager.WakeLock? = null

    /** 实际使用的采集/编码尺寸；由 opaque（MediaCodec）尺寸列表核对后选定。 */
    private var videoWidth = VIDEO_WIDTH
    private var videoHeight = VIDEO_HEIGHT
    /** 当前生效的采集帧率/码率；HELLO_ACK 协商后可覆盖（见 [applyNegotiatedConfig]）。 */
    @Volatile private var videoFps = VIDEO_FPS
    @Volatile private var videoBitRate = VIDEO_BIT_RATE
    /** 协商目标配置；与 [videoWidth] 等生效值分离，避免重建竞态。 */
    @Volatile private var targetWidth = VIDEO_WIDTH
    @Volatile private var targetHeight = VIDEO_HEIGHT
    @Volatile private var targetFps = VIDEO_FPS
    @Volatile private var targetBitRate = VIDEO_BIT_RATE
    /** 相机支持的 AE 目标帧率范围；buildCapabilities 时读取，用于协商帧率的合法性收敛。 */
    private var aeFpsRanges: Array<Range<Int>>? = null

    /** 第二路 dummy 输出流的资源：只为满足 HAL 的流组合要求，不消费其内容。 */
    private var dummyTexture: SurfaceTexture? = null
    private var dummySurface: Surface? = null
    private var dummyReader: ImageReader? = null
    private var dummyWidth = 0
    private var dummyHeight = 0

    /** opaque（MediaCodec input Surface）输出尺寸列表；dummy 流尺寸的候选来源。 */
    private var opaqueSizes: Array<Size>? = null

    /** SurfaceTexture / YUV_420_888 输出尺寸列表；dummy 流按目标类型核对合法性。 */
    private var surfaceTextureSizes: Array<Size>? = null
    private var yuvSizes: Array<Size>? = null

    /** 相机/编码器连续失败次数；相机会话配置成功后清零（退避表与链路一致）。 */
    private var cameraFailureCount = 0

    /** 本次相机故障是否已通过链路上报过一次「眼镜相机异常」（camera 线程读写）。 */
    private var cameraAbnormalReported = false

    /** 启动阶段（能力枚举）连续失败次数；成功后清零。 */
    private var startupFailureCount = 0

    /** 已排队的退避重建任务（只在 camera 线程上读写）。 */
    private var cameraRebuildTask: Runnable? = null

    private var linkClient: LinkClient? = null
    private var linkThread: Thread? = null
    private var linkCapabilities: GlassCapabilities? = null
    private var p2pClient: P2pGroupClient? = null
    /** 非空时跳过 Wi-Fi Direct，直连该 host（m1 USB 隧道 / 录制调试，见 [startLinkTransport]）。 */
    @Volatile private var fixedHost: String? = null

    /** 服务生命周期开关；置 false 后所有线程自行退出。 */
    @Volatile
    private var running = false

    /** 握手是否完成（跨线程：link 线程写，camera 线程读）。 */
    @Volatile
    private var handshakeSucceeded = false

    /** AE 是否已收敛（camera 线程写、读）。 */
    @Volatile
    private var aeConverged = false

    /**
     * 编码器 PTS（相机 surface 时间戳）到本服务统一时钟域（[nowNs]，
     * elapsedRealtimeNanos）的运行时锚点偏移。ARGF20 实测 SENSOR_TIMESTAMP_SOURCE
     * 报 REALTIME，但 PTS 绝对值是一个厂商私有时钟基（约 1.93e14 ns，非墙上时钟），
     * 无法用 wall↔elapsed 换算；只能在首帧输出时刻把 PTS 锚定到 elapsed 域
     * （误差 ≈ 编码管线延迟，几十毫秒级），帧间间隔仍由 PTS 精确给出。
     * 手机端 ClockSyncEstimator 按 elapsed 域换算后即可得到正确年龄。
     */
    @Volatile
    private var videoPtsBaseOffsetNs: Long? = null

    private var captureStartNs = 0L

    /** 缓存的 SPS/PPS（编码器首次输出的 CODEC_CONFIG），重连后重发（约束 4）。 */
    @Volatile
    private var codecConfigPayload: ByteArray? = null

    /** 每次握手成功后置 true，由编码输出线程在下一次输出时先重发配置再发帧。 */
    @Volatile
    private var codecConfigPending = false

    /** 每通道独立递增的 uint32 序号。 */
    private var videoSequence = 0L
    private var controlSequence = 0L
    private var speakStatusSequence = 0L

    /** POSE (0x04) 通道（工单 M1-05）：rotation vector 采样批，100ms 一包。 */
    private var sensorThread: HandlerThread? = null
    private var sensorHandler: Handler? = null
    private var poseListener: SensorEventListener? = null
    private val poseSamples = mutableListOf<PosePayloadCodec.PoseSample>()
    private var poseFlushTask: Runnable? = null
    private var poseSequence = 0L

    /** AUDIO (0x02) 通道（工单 V-01）：AudioRecord 16kHz mono PCM16，20ms/包。 */
    private var audioThread: Thread? = null
    private var audioSequence = 0L

    /** 音频路径累计失败次数与「已上报」标志（只在 audio 线程读写）。 */
    private var audioFailureCount = 0
    private var audioIssueReported = false

    /** 链路是否处于"已握手且未断线"的媒体发送态；传感器批/音频块只在此时入队。 */
    @Volatile
    private var linkMediaActive = false

    // ------------------------------------------------------------------
    // 生命周期
    // ------------------------------------------------------------------

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!running) {
            running = true
            // 供 GlassLinkActivity 判断「服务仍在运行」以自动拉起被系统回收的窗口。
            serviceRunning = true
            startAsForeground()
            acquireWakeLock()
            startCaptureAndLink(intent)
        } else {
            // 服务已在运行：新 host 需要重启服务才生效（am stopservice + am start）。
            Log.d(TAG, "服务已在运行，忽略重复启动（新配置需重启服务生效）")
        }
        return START_STICKY
    }

    override fun onDestroy() {
        Log.i(TAG, "服务销毁，开始释放资源")
        running = false
        serviceRunning = false
        stopLink()
        p2pClient?.stop()
        p2pClient = null

        cameraHandler?.post { teardownCameraAndEncoder() }
        cameraThread?.quitSafely()
        stopPoseCapture()
        stopAudioCapture()

        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null
        Log.i(TAG, "服务资源已释放")
    }

    // ------------------------------------------------------------------
    // 前台服务与唤醒锁
    // ------------------------------------------------------------------

    private fun startAsForeground() {
        // 约束（工单 3.3）：API 32 上 startForeground 之前必须先建通知渠道。
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_name),
            NotificationManager.IMPORTANCE_LOW
        )
        channel.description = getString(R.string.notification_channel_description)
        manager.createNotificationChannel(channel)

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_text))
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        )
    }

    private fun acquireWakeLock() {
        val powerManager = getSystemService(PowerManager::class.java)
        val lock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "blindassist:glasslink")
        lock.setReferenceCounted(false)
        lock.acquire()
        wakeLock = lock
    }

    // ------------------------------------------------------------------
    // 启动：能力枚举 → 相机线程 → 链路线程
    // ------------------------------------------------------------------

    private fun startCaptureAndLink(intent: Intent?) {
        if (!running) return
        fixedHost = intent?.getStringExtra(EXTRA_HOST)?.takeIf { it.isNotBlank() }
        if (!P2pPermissions.hasAll(this)) {
            // 只走 Wi-Fi Direct：没有权限就无法建立链路。
            currentStatus = "缺少 Wi-Fi Direct 权限"
            Log.w(TAG, "缺少 Wi-Fi Direct 权限，服务停止")
            stopSelf()
            return
        }

        val caps = try {
            buildCapabilities(CAMERA_ID)
        } catch (e: SecurityException) {
            // 缺 CAMERA 权限：重试多少次都没用，唯一允许 stopSelf 的情况。
            Log.e(TAG, "缺少 CAMERA 权限，停止服务（重试无意义）", e)
            stopSelf()
            return
        } catch (e: Exception) {
            // 其余能力枚举失败也按「失败→退避重试」处理，不让服务自杀。
            val delayMs =
                CAMERA_BACKOFF_MS[startupFailureCount.coerceAtMost(CAMERA_BACKOFF_MS.size - 1)]
            startupFailureCount++
            Log.w(TAG, "读取相机能力失败：${e.message}，${delayMs}ms 后退避重试（连续失败 $startupFailureCount 次）", e)
            mainHandler.postDelayed({ if (running) startCaptureAndLink(intent) }, delayMs)
            return
        }
        startupFailureCount = 0
        Log.i(
            TAG,
            "能力：device=${caps.deviceModel} timestampSource=${caps.sensorTimestampSource} " +
                "orientation=${caps.sensorOrientationDegrees} modes=${caps.videoModes.size}"
        )

        cameraThread = HandlerThread("glasses-camera").apply { start() }
        cameraHandler = Handler(cameraThread!!.looper)
        cameraHandler!!.post { startCameraPipeline() }
        startPoseCapture()
        startAudioCapture()

        linkCapabilities = caps
        startLinkTransport()
    }

    // ------------------------------------------------------------------
    // 传输层：Wi-Fi Direct（唯一生产路径；局域网扫网仅作 P2P 失败后的兜底）
    // ------------------------------------------------------------------

    private fun startLinkTransport() {
        val host = fixedHost
        if (host != null) {
            // m1 录制路径：USB 隧道（adb reverse）或局域网直连，跳过 P2P 发现。
            Log.i(TAG, "直连固定 host（录制/调试）：$host")
            currentStatus = "直连：$host"
            startLink(host, network = null)
            return
        }
        // 官方用法："Wi-Fi 开关打开即可，无需连接网络"；雷鸟会在熄屏/未佩戴时
        // 自动关 Wi-Fi，启动前先确保打开，否则 P2P 发现和局域网兜底都起不来。
        ensureWifiEnabled()
        startP2pLink()
    }

    private fun ensureWifiEnabled() {
        runCatching {
            val wifiManager = getSystemService(Context.WIFI_SERVICE) as WifiManager
            if (!wifiManager.isWifiEnabled) {
                Log.i(TAG, "Wi-Fi 未开启，尝试自动开启")
                // Android 12 上第三方 App 仍可调用；13+ 受限制，眼镜端是 API 32。
                @Suppress("DEPRECATION")
                wifiManager.isWifiEnabled = true
            }
        }.onFailure {
            Log.w(TAG, "自动开启 Wi-Fi 失败：${it.message}")
        }
    }

    /**
     * P2P 客户端主流程：发现手机 → 加入 P2P 组 → 拿到 GO 地址与 P2P 网络后
     * 启动 [LinkClient]。组断开时自动重连；P2P 失败后由客户端的局域网
     * 扫网兜底（手机回复局域网 IP，仍走本链路的 TCP）。
     */
    private fun startP2pLink() {
        if (!P2pPermissions.locationServicesEnabled(this)) {
            Log.w(TAG, "系统定位开关未开启，P2P 发现可能失败（Android 12 需要）")
        }
        val client = P2pGroupClient(applicationContext, object : P2pGroupClient.Listener {
            override fun onState(message: String) {
                Log.i(TAG, "P2P: $message")
                // 已有活跃链路时不覆盖"连接中/已连接"：否则 P2P 客户端每 3 秒
                // 收到一次凭据回复，会把状态刷回"正在加入"，造成"显示加入中但
                // 实际已连接"的假象。
                if (linkClient == null) currentStatus = message
            }

            override fun onGroupReady(ownerAddress: InetAddress?, network: Network?) {
                val host = ownerAddress?.hostAddress ?: P2pConstants.DEFAULT_GROUP_OWNER_IP
                Log.i(TAG, "P2P 组就绪：owner=$host, network=${network != null}")
                currentStatus = "P2P 已加入：$host"
                startLink(host, network)
            }

            override fun onPeerFound() = Unit

            override fun onPeerConnectFailed() {
                Log.w(TAG, "P2P connect 失败/超时，交给局域网扫网兜底")
                currentStatus = "P2P 加入失败，尝试局域网"
            }

            override fun onFallbackReady(host: String) {
                Log.i(TAG, "P2P 免弹窗加入失败，走手机局域网 IP：$host")
                currentStatus = "回退局域网：$host"
                startLink(host, network = null)
            }

            override fun onGroupLost(reason: String) {
                Log.w(TAG, "P2P 组断开：$reason")
                currentStatus = "P2P 断开，等待重新加入"
                stopLink()
            }

            override fun onError(error: Throwable) {
                Log.w(TAG, "P2P 错误：${error.message}")
                currentStatus = "P2P 不可用：${error.message}"
            }
        })
        p2pClient = client
        client.start()
    }

    /**
     * 启动 TCP 链路（host 来自 P2P GO 或手机局域网 IP）。P2P 模式下 socket
     * 绑定到 P2P [Network]，避免路由走到 USB/其它接口（实测 10.0.0.2 空转问题）。
     */
    private fun startLink(host: String, network: Network?) {
        if (!running) return
        stopLink()
        val caps = linkCapabilities ?: return
        Log.i(TAG, "启动链路：host=$host（${if (network != null) "P2P 网络" else "默认网络"}）")
        currentStatus = "连接中：$host"
        val client = LinkClient(host, TCP_PORT, caps, network)
        linkClient = client
        linkThread = Thread({ client.run() }, "glass-link").apply {
            isDaemon = true
            start()
        }
    }

    private fun stopLink() {
        linkClient?.shutdown()
        linkThread?.interrupt()
        linkThread = null
        linkClient = null
    }

    /** 链路握手成功（进入媒体发送）时更新状态文案。 */
    private fun onLinkEstablished(host: String) {
        currentStatus = "已连接：$host"
    }

    /** 链路中断并进入重连时更新状态文案。 */
    private fun onLinkLost(reason: String) {
        currentStatus = "链路中断，正在重连：$reason"
    }

    // ------------------------------------------------------------------
    // 能力枚举（运行时真读，不写死）
    // ------------------------------------------------------------------

    private fun buildCapabilities(cameraId: String): GlassCapabilities {
        val manager = getSystemService(CameraManager::class.java)
        val characteristics = manager.getCameraCharacteristics(cameraId)
        val map = characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val sensorManager = getSystemService(SensorManager::class.java)

        // docs/archive/工单-M1-03-打回2-相机会话失败.md 第 3 节：编码器 input Surface 走 opaque（IMPLEMENTATION_DEFINED）
        // 格式，其可用尺寸集合与 YUV/SurfaceTexture 不一定相同，必须在 startEncoder 之前
        // 查询并核对 640×360，避免相机 endConfigure 因尺寸不合法而 Broken pipe。
        val opaqueSizes = map?.getOutputSizes(MediaCodec::class.java)
        this.opaqueSizes = opaqueSizes
        this.surfaceTextureSizes = map?.getOutputSizes(SurfaceTexture::class.java)
        this.yuvSizes = map?.getOutputSizes(ImageFormat.YUV_420_888)
        Log.i(TAG, "MediaCodec 可用尺寸：${opaqueSizes?.joinToString { "${it.width}x${it.height}" }}")
        selectVideoSize(opaqueSizes)

        // 约束 1：sensorTimestampSource 运行时真读，不写死 REALTIME。
        val timestampSource = when (
            characteristics.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE)
        ) {
            CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME -> TimestampSource.REALTIME
            else -> TimestampSource.UNKNOWN
        }
        val orientation = characteristics.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0
        aeFpsRanges = characteristics.get(
            CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES
        )

        return GlassCapabilities(
            protocolVersion = LinkWire.VERSION,
            deviceModel = Build.MODEL,
            videoModes = enumVideoModes(map, opaqueSizes),
            hasHardwareAvcEncoder = hasHardwareAvcEncoder(),
            // M0 已证实眼镜上没有任何 TTS 引擎：固定 false，不做运行时探测。
            hasLocalChineseTts = false,
            hasRotationVector = sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR) != null,
            hasSixDof = false,
            hasTempleTouch = false,
            hasWearDetection = false,
            hasAudioCapture = probeAudioCapture(),
            sensorTimestampSource = timestampSource,
            sensorOrientationDegrees = orientation
        )
    }

    /**
     * 运行时枚举 StreamConfigurationMap 的实际输出尺寸与最大帧率。
     * 优先用 opaque（MediaCodec input Surface）尺寸集合——这才是本管线真正能用的集合，
     * 保证 HELLO 的 videoModes 里包含实际使用的尺寸；查询不到时回退 SurfaceTexture 集合。
     */
    private fun enumVideoModes(
        map: StreamConfigurationMap?,
        opaqueSizes: Array<Size>?
    ): List<VideoMode> {
        val theMap = map ?: return emptyList()
        val useOpaque = !opaqueSizes.isNullOrEmpty()
        val sizes = if (useOpaque) {
            opaqueSizes!!
        } else {
            theMap.getOutputSizes(SurfaceTexture::class.java) ?: return emptyList()
        }
        val modes = mutableListOf<VideoMode>()
        for (size in sizes) {
            val minDurationNs = if (useOpaque) {
                theMap.getOutputMinFrameDuration(MediaCodec::class.java, size)
            } else {
                theMap.getOutputMinFrameDuration(SurfaceTexture::class.java, size)
            }
            val maxFps = if (minDurationNs > 0L) {
                (1_000_000_000L / minDurationNs).toInt().coerceAtLeast(1)
            } else {
                30
            }
            modes.add(VideoMode(width = size.width, height = size.height, maxFps = maxFps))
        }
        modes.sortWith(
            compareByDescending<VideoMode> { it.width * it.height }.thenByDescending { it.maxFps }
        )
        return modes
    }

    /**
     * 核对 opaque（IMPLEMENTATION_DEFINED）尺寸列表，确定实际使用的编码尺寸：
     * 640×360 在列表中则直接用；不在则选最接近 640×360 的 16:9 尺寸，
     * 绝不硬塞 HAL 不支持的尺寸（docs/archive/工单-M1-03-打回2-相机会话失败.md 第 3 节）。
     */
    private fun selectVideoSize(opaqueSizes: Array<Size>?) {
        if (opaqueSizes.isNullOrEmpty()) {
            Log.w(TAG, "opaque 尺寸列表为空，回退目标 ${targetWidth}x$targetHeight")
            videoWidth = targetWidth
            videoHeight = targetHeight
            return
        }
        val exact = opaqueSizes.firstOrNull { it.width == targetWidth && it.height == targetHeight }
        if (exact != null) {
            videoWidth = exact.width
            videoHeight = exact.height
            Log.i(TAG, "${targetWidth}x$targetHeight 在 MediaCodec opaque 列表中，直接使用")
            return
        }
        val byDistance = compareBy<Size> { size ->
            val dw = size.width - targetWidth
            val dh = size.height - targetHeight
            dw * dw + dh * dh
        }
        // 先找 16:9（等距时取较大者，保留下游识别所需分辨率）；整个列表都没有
        // 16:9 时退而求其次取最接近的任意尺寸，也不回退到列表外的 640x360。
        val nearest = opaqueSizes
            .filter { it.width * 9L == it.height * 16L }
            .minWithOrNull(byDistance.thenByDescending { it.width })
            ?: opaqueSizes.minWithOrNull(byDistance)
        if (nearest != null) {
            videoWidth = nearest.width
            videoHeight = nearest.height
            Log.w(TAG, "${targetWidth}x$targetHeight 不在 opaque 列表，"
                + "选择最接近的 16:9 尺寸：${videoWidth}x$videoHeight")
        }
    }

    private fun hasHardwareAvcEncoder(): Boolean {
        return try {
            val format = MediaFormat.createVideoFormat(MIME_TYPE_AVC, VIDEO_WIDTH, VIDEO_HEIGHT)
            val codecList = MediaCodecList(MediaCodecList.REGULAR_CODECS)
            val name = codecList.findEncoderForFormat(format) ?: return false
            codecList.codecInfos.firstOrNull { it.name == name }?.isHardwareAccelerated ?: false
        } catch (e: Exception) {
            Log.w(TAG, "查询 AVC 编码器失败：${e.message}")
            false
        }
    }

    /**
     * 运行时真读麦克风能力（工单 V-01 约束 3）：按 16kHz mono PCM16 建一个
     * [AudioRecord] 并立即释放，初始化成功才算 hasAudioCapture=true。
     * 缺 RECORD_AUDIO 权限（SecurityException）或设备无麦克风都返回 false，
     * 不写死 —— HELLO 里必须诚实，手机端才敢开 AUDIO 通道。
     */
    private fun probeAudioCapture(): Boolean {
        return try {
            val record = createAudioRecord()
            if (record == null) {
                Log.w(TAG, "麦克风能力探测失败：AudioRecord 未初始化（hasAudioCapture=false）")
                false
            } else {
                record.release()
                Log.i(TAG, "麦克风能力探测成功：16kHz mono PCM16（hasAudioCapture=true）")
                true
            }
        } catch (e: SecurityException) {
            Log.w(TAG, "麦克风能力探测失败：缺少 RECORD_AUDIO 权限（hasAudioCapture=false）", e)
            false
        } catch (e: Exception) {
            Log.w(TAG, "麦克风能力探测失败：${e.message}（hasAudioCapture=false）", e)
            false
        }
    }

    /** 按 16kHz mono PCM16 创建并校验 [AudioRecord]；初始化失败返回 null（不抛）。 */
    private fun createAudioRecord(): AudioRecord? {
        val record = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            AudioPayloadCodec.SAMPLE_RATE_HZ,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            audioBufferSizeBytes()
        )
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "AudioRecord 初始化失败：state=${record.state}")
            record.release()
            return null
        }
        return record
    }

    /**
     * AudioRecord 缓冲区大小（工单 V-01）：取 max(minBufferSize, 4 个 20ms 包)，
     * 至少容纳 4 包（2560 字节），给系统调度抖动留余量，降低 read 被饿到的概率。
     */
    private fun audioBufferSizeBytes(): Int {
        val min = AudioRecord.getMinBufferSize(
            AudioPayloadCodec.SAMPLE_RATE_HZ,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (min <= 0) {
            Log.w(TAG, "AudioRecord.getMinBufferSize 返回 $min，按 4 包（2560B）兜底")
            return AudioPayloadCodec.PACKET_BYTES * 4
        }
        return maxOf(min, AudioPayloadCodec.PACKET_BYTES * 4)
    }

    // ------------------------------------------------------------------
    // 编码器（Surface 直通，像素不进 CPU）
    // ------------------------------------------------------------------

    private fun startEncoder() {
        // 生效配置：启动前从协商目标取值（HELLO_ACK 在握手后可能已改目标）。
        videoFps = targetFps
        videoBitRate = targetBitRate
        val format = MediaFormat.createVideoFormat(MIME_TYPE_AVC, videoWidth, videoHeight).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, videoBitRate)
            setInteger(MediaFormat.KEY_FRAME_RATE, videoFps)
            // 约束 3：I 帧间隔 1 秒，不是 M0 探针用的 2。
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
        }
        val codec = MediaCodec.createEncoderByType(MIME_TYPE_AVC)
        // 异步模式契约：setCallback 必须在 configure 之前调（docs/archive/工单-M1-03-打回2-相机会话失败.md 第 2 节）。
        // 顺序反了会让高通 c2 编码器进入不自洽状态、input Surface 生产端被废，
        // 相机端配流时拿到 Broken pipe (-32)。
        codec.setCallback(encoderCallback, cameraHandler!!)
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        val surface = codec.createInputSurface()
        codec.start()
        encoder = codec
        encoderInputSurface = surface
        Log.i(TAG, "编码器已启动：${videoWidth}x$videoHeight@$videoFps, ${videoBitRate}bps, I 帧间隔 1s")
    }

    private val encoderCallback = object : MediaCodec.Callback() {
        override fun onInputBufferAvailable(codec: MediaCodec, index: Int) = Unit

        override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
            handleEncodedOutput(codec, index, info)
        }

        override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
            Log.e(TAG, "编码器错误：${e.message}", e)
            scheduleCameraRebuild("编码器错误：${e.message}")
        }

        override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
            // 兜底：个别编码器不发 CODEC_CONFIG buffer 只给 csd-0/csd-1；qcom 编码器
            // 正常会走 buffer 路径，这里仅在没有缓存时合成一份。
            if (codecConfigPayload == null) {
                try {
                    val csd0 = format.getByteBuffer("csd-0")
                    val csd1 = format.getByteBuffer("csd-1")
                    if (csd0 != null && csd1 != null) {
                        val b0 = ByteArray(csd0.remaining()).also { csd0.get(it) }
                        val b1 = ByteArray(csd1.remaining()).also { csd1.get(it) }
                        cacheCodecConfig(b0 + b1)
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "从输出格式读取 SPS/PPS 失败：${e.message}")
                }
            }
        }
    }

    /**
     * 编码输出处理。约束 6：必须先完整拷贝再 [MediaCodec.releaseOutputBuffer]，
     * release 后缓冲区内容未定义。每帧只分配一个 payload ByteArray（LinkPacket
     * 需要持有它直到发送），不做额外暂存拷贝。
     */
    private fun handleEncodedOutput(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
        if (info.size <= 0) {
            codec.releaseOutputBuffer(index, false)
            return
        }
        val outputBuffer = codec.getOutputBuffer(index)
        val payload: ByteArray
        if (outputBuffer != null) {
            outputBuffer.position(info.offset)
            outputBuffer.limit(info.offset + info.size)
            payload = ByteArray(info.size)
            outputBuffer.get(payload)
        } else {
            payload = ByteArray(0)
        }
        // 拷贝完成之后才能 release（约束 6）。
        codec.releaseOutputBuffer(index, false)

        if (payload.isEmpty()) return

        val isCodecConfig = (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
        if (isCodecConfig) {
            cacheCodecConfig(payload)
            return
        }

        if (!handshakeSucceeded) return
        // AE 收敛前的帧先不发，收敛时打日志说明等了多久（见 captureCallback）。
        if (!aeConverged) return

        if (codecConfigPending) {
            val cached = codecConfigPayload
            if (cached != null) {
                codecConfigPending = false
                enqueuePacket(
                    LinkPacket(
                        channel = LinkChannel.VIDEO,
                        flags = LinkFlags.CODEC_CONFIG,
                        sequence = nextVideoSequence(),
                        // 配置包没有可显示帧，时间戳沿用该输出 buffer 的采集时刻。
                        senderTimestampNs = videoPtsInNowDomain(info.presentationTimeUs),
                        payload = cached
                    )
                )
            }
        }

        val flags = if ((info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0) {
            LinkFlags.KEYFRAME
        } else {
            LinkFlags.NONE
        }
        // 约束 2：时间戳是采集时刻（surface 时间戳，微秒），不是取到输出的当下时刻。
        enqueuePacket(
            LinkPacket(
                channel = LinkChannel.VIDEO,
                flags = flags,
                sequence = nextVideoSequence(),
                senderTimestampNs = videoPtsInNowDomain(info.presentationTimeUs),
                payload = payload
            )
        )
    }

    /**
     * 编码器输出 PTS（微秒）→ 本服务统一时钟域（elapsedRealtimeNanos）。
     * 相机 PTS 的绝对基是厂商私有时钟，且其走时速率与 elapsedRealtime 有微小
     * 差异（实测约 0.6%/分钟量级）。因此锚点不能只取首帧：每帧用
     * nowNs − PTS（≈ 编码管线延迟）做 EMA 持续校正，帧间间隔仍由 PTS 精确给出，
     * 绝对基准则被 EMA 吸收掉厂商时钟漂移，避免手机端帧龄随时间越算越老。
     */
    private fun videoPtsInNowDomain(presentationTimeUs: Long): Long {
        val ptsNs = presentationTimeUs * 1000L
        val nowNs = SystemClock.elapsedRealtimeNanos()
        val current = videoPtsBaseOffsetNs
        val base = if (current == null) {
            nowNs - ptsNs
        } else {
            current + (nowNs - ptsNs - current) / ANCHOR_EMA_DIVISOR
        }
        videoPtsBaseOffsetNs = base
        return ptsNs + base
    }

    /** 相机/编码器重建或重连后 PTS 基可能变化，重置锚点（camera 线程调用）。 */
    private fun resetVideoPtsAnchor() {
        videoPtsBaseOffsetNs = null
    }

    private fun cacheCodecConfig(payload: ByteArray) {
        codecConfigPayload = payload
        codecConfigPending = true
        Log.d(TAG, "已缓存 CODEC_CONFIG（${payload.size} 字节），握手完成后重发")
    }

    /** 每次握手成功后请求一个同步帧，加快重连后的出画（F6-7）。 */
    private fun requestSyncFrame() {
        try {
            encoder?.setParameters(
                Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) }
            )
        } catch (e: Exception) {
            Log.d(TAG, "请求同步帧失败（忽略）：${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // Camera2（id=0，输出直接给编码器 input Surface）
    // ------------------------------------------------------------------

    /**
     * 相机/编码器管线入口（初始启动与退避重建共用）。
     * 除缺 CAMERA 权限外，任何失败都走退避重建，不再让整个服务自杀。
     */
    private fun startCameraPipeline() {
        if (!running) return
        try {
            // 每次启动/重建都按当前 target（协商后可能已变）重新解析分辨率，
            // 否则 HELLO_ACK 改 720p 后重建只会换 fps/码率、尺寸停在 640×360。
            selectVideoSize(opaqueSizes)
            startEncoder()
            openCamera()
        } catch (e: SecurityException) {
            // 唯一允许 stopSelf 的情况之一：缺权限，重试多少次都没用。
            Log.e(TAG, "缺少 CAMERA 权限，停止服务（重试无意义）", e)
            stopSelf()
        } catch (e: Exception) {
            Log.e(TAG, "启动相机/编码器失败：${e.message}", e)
            scheduleCameraRebuild("启动相机/编码器失败：${e.message}")
        }
    }

    /**
     * 相机/编码器路径失败统一入口：拆掉相机与编码器 → 按链路同款退避表延迟重建，
     * 对齐约束 5 的「失败→退避重连」模型（docs/archive/工单-M1-03-打回2-相机会话失败.md 第 4 节）。
     * 相机被抢、Wi-Fi 断、会话配置失败都是常态，常态不该让服务自杀。
     *
     * docs/archive/工单-M1-03-打回3-厂商HAL崩溃.md 第 4 节：本路径用相机专用退避表（首档 1s，不用链路的 250ms
     * 档）——厂商 HAL 崩溃后进程重启需要时间；重试次数不设上限，连续失败达到阈值
     * 时通过链路向手机上报一次「眼镜相机异常」。
     */
    private fun scheduleCameraRebuild(reason: String) {
        if (!running) return
        val delayMs =
            CAMERA_BACKOFF_MS[cameraFailureCount.coerceAtMost(CAMERA_BACKOFF_MS.size - 1)]
        cameraFailureCount++
        Log.w(TAG, "$reason：${delayMs}ms 后退避重建相机/编码器（连续失败 $cameraFailureCount 次）")
        maybeReportCameraAbnormal()
        cameraHandler?.post {
            teardownCameraAndEncoder()
            if (!running) return@post
            // 同一条 camera 线程串行执行；先清掉旧的重建任务，防止重复排队。
            cameraRebuildTask?.let { cameraHandler?.removeCallbacks(it) }
            val task = Runnable {
                cameraRebuildTask = null
                startCameraPipeline()
            }
            cameraRebuildTask = task
            cameraHandler?.postDelayed(task, delayMs)
        }
    }

    /**
     * 连续失败达到阈值后，通过链路向手机端上报一次「眼镜相机异常」，让手机端能播报。
     * 一次故障只报一次：会话配置成功（[CameraCaptureSession.StateCallback.onConfigured]）
     * 时复位；链路未握手成功时先跳过，等下一次失败再试，避免把报告积压在发送队列里
     * 变成迟到消息。
     *
     * 线协议里没有专门的相机状态报文（:link 冻结不动），复用 SPEAK_STATUS 上行通道
     * （HELLO_ACK 已启用该通道）发 UTF-8 文本，手机端解析该通道即可播报。
     */
    private fun maybeReportCameraAbnormal() {
        if (cameraFailureCount < CAMERA_ABNORMAL_REPORT_THRESHOLD || cameraAbnormalReported) return
        if (!handshakeSucceeded) {
            Log.d(TAG, "相机异常待上报，但链路尚未握手成功，等下一次失败再试")
            return
        }
        cameraAbnormalReported = true
        Log.w(TAG, "相机连续失败达 $CAMERA_ABNORMAL_REPORT_THRESHOLD 次，通过链路上报「眼镜相机异常」")
        enqueuePacket(
            LinkPacket(
                channel = LinkChannel.SPEAK_STATUS,
                flags = LinkFlags.NONE,
                sequence = nextSpeakStatusSequence(),
                senderTimestampNs = nowNs(),
                payload = CAMERA_ABNORMAL_REPORT_TEXT.toByteArray(Charsets.UTF_8)
            )
        )
    }

    private fun openCamera() {
        val manager = getSystemService(CameraManager::class.java)
        try {
            manager.openCamera(
                CAMERA_ID,
                object : CameraDevice.StateCallback() {
                    override fun onOpened(camera: CameraDevice) {
                        cameraDevice = camera
                        createCaptureSession(camera)
                    }

                    override fun onDisconnected(camera: CameraDevice) {
                        Log.e(TAG, "相机已断开（被抢或系统回收），退避重建")
                        scheduleCameraRebuild("相机已断开")
                    }

                    override fun onError(camera: CameraDevice, error: Int) {
                        Log.e(TAG, "相机错误：$error（${cameraErrorName(error)}），退避重建")
                        scheduleCameraRebuild("相机错误 $error")
                    }
                },
                cameraHandler!!
            )
        } catch (e: SecurityException) {
            // 缺 CAMERA 权限：重试无意义，允许 stopSelf。
            Log.e(TAG, "缺少 CAMERA 权限，停止服务（重试无意义）", e)
            stopSelf()
        } catch (e: Exception) {
            Log.e(TAG, "打开相机失败：${e.message}", e)
            scheduleCameraRebuild("打开相机失败：${e.message}")
        }
    }

    private fun createCaptureSession(camera: CameraDevice) {
        val surface = encoderInputSurface
        if (surface == null) {
            Log.e(TAG, "编码器输入 Surface 未就绪")
            scheduleCameraRebuild("编码器输入 Surface 未就绪")
            return
        }
        // docs/archive/工单-M1-03-打回3-厂商HAL崩溃.md 第 3 节：第二路 dummy 输出流，只为满足厂商 HAL 对流组合
        // 的要求（本机只有一路编码器流会触发 AdvancedCameraUsecase 崩溃），不消费。
        val dummy = createDummyOutput()
        val streams = if (dummy != null) listOf(surface, dummy) else listOf(surface)
        Log.i(
            TAG,
            "相机会话流配置：共 ${streams.size} 路 —— " +
                "1) 编码器 ${videoWidth}x$videoHeight（MediaCodec input Surface，消费）" +
                if (dummy != null) "；2) dummy ${dummyWidth}x$dummyHeight（仅满足 HAL 流组合，不消费）"
                else "；无 dummy 流"
        )
        try {
            camera.createCaptureSession(
                streams,
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(session: CameraCaptureSession) {
                        captureSession = session
                        cameraFailureCount = 0
                        // 故障已恢复，下一次故障可以重新上报。
                        cameraAbnormalReported = false
                        Log.i(TAG, "相机会话配置成功")
                        startRepeatingRequest(session)
                    }

                    override fun onConfigureFailed(session: CameraCaptureSession) {
                        Log.e(TAG, "相机会话配置失败（endConfigure），退避重建")
                        scheduleCameraRebuild("相机会话配置失败")
                    }
                },
                cameraHandler!!
            )
        } catch (e: SecurityException) {
            Log.e(TAG, "缺少 CAMERA 权限，停止服务（重试无意义）", e)
            stopSelf()
        } catch (e: Exception) {
            Log.e(TAG, "创建相机会话失败：${e.message}", e)
            scheduleCameraRebuild("创建相机会话失败：${e.message}")
        }
    }

    /**
     * 创建第二路 dummy 输出流（docs/archive/工单-M1-03-打回3-厂商HAL崩溃.md 第 3 节）。
     *
     * 只为满足 HAL 对流组合的要求，不消费其内容：
     * - 只参与流配置（[DUMMY_IN_REPEATING_REQUEST] = false，默认）时首选
     *   `SurfaceTexture(0)`：texName=0 不绑定任何 GL 纹理，无 GL 上下文也可用，
     *   且不进 repeating target 就不会有帧进来，无需消费；
     * - 进 target（[DUMMY_IN_REPEATING_REQUEST] = true）时用 ImageReader，
     *   `onImageAvailable` 里立即 `close()`，只为了让队列不堵；
     * - 构造期抛 GL/其它异常时回退 ImageReader（文档给的兜底）。
     *
     * 尺寸从 opaque 尺寸列表里选（优先较小的 320×180 / 640×360 以省功耗），
     * 再核对目标 surface 类型的合法输出尺寸；全部不可用时返回 null 保持单流。
     */
    private fun createDummyOutput(): Surface? {
        if (dummySurface != null) return dummySurface

        // 候选尺寸池：opaque 列表优先（工单第 2 节那份），缺失时回退 YUV/SurfaceTexture。
        val preferred = preferDummySize(opaqueSizes)
            ?: preferDummySize(yuvSizes)
            ?: preferDummySize(surfaceTextureSizes)
        if (preferred == null) {
            Log.w(TAG, "没有任何尺寸列表可用，无法添加 dummy 输出流，保持单流配置")
            return null
        }

        val needsConsumption = DUMMY_IN_REPEATING_REQUEST
        if (!needsConsumption && surfaceTextureSizes?.any { it == preferred } == true) {
            createSurfaceTextureDummy(preferred)?.let { return it }
        }
        if (yuvSizes?.any { it == preferred } == true) {
            createImageReaderDummy(preferred)?.let { return it }
        }
        // 候选尺寸不是该 surface 类型的合法输出尺寸：按各自列表重新选。
        if (!needsConsumption) {
            preferDummySize(surfaceTextureSizes)?.let { st ->
                createSurfaceTextureDummy(st)?.let { return it }
            }
        }
        preferDummySize(yuvSizes)?.let { yuv ->
            createImageReaderDummy(yuv)?.let { return it }
        }

        Log.w(TAG, "dummy 输出流创建失败，保持单流配置（HAL 流组合问题可能复发）")
        return null
    }

    /**
     * 按偏好顺序从尺寸集合里选 dummy 流尺寸：320×180 → 640×360 →
     * 面积最小的 16:9（不超过 640×360）→ 集合内面积最小。
     */
    private fun preferDummySize(sizes: Array<Size>?): Size? {
        if (sizes.isNullOrEmpty()) return null
        val preferred = listOf(Size(320, 180), Size(640, 360))
            .firstOrNull { c -> sizes.any { it == c } }
        if (preferred != null) return preferred
        return sizes
            .filter { it.width * 9L == it.height * 16L && it.width * it.height <= VIDEO_WIDTH * VIDEO_HEIGHT }
            .minByOrNull { it.width * it.height }
            ?: sizes.minByOrNull { it.width * it.height }
    }

    private fun createSurfaceTextureDummy(size: Size): Surface? {
        return try {
            val texture = SurfaceTexture(0).apply {
                setDefaultBufferSize(size.width, size.height)
                // texName=0 未绑定任何 GL 纹理，detachFromGLContext() 是 no-op；
                // 保留调用以防将来改用绑定式构造后出现 GL 相关错误。
                detachFromGLContext()
            }
            val surface = Surface(texture)
            dummyTexture = texture
            dummySurface = surface
            dummyWidth = size.width
            dummyHeight = size.height
            Log.i(TAG, "dummy 输出流（SurfaceTexture）：${size.width}x${size.height}")
            surface
        } catch (e: Exception) {
            Log.w(TAG, "SurfaceTexture dummy 创建失败（${e.message}），回退 ImageReader")
            releaseDummy()
            null
        }
    }

    private fun createImageReaderDummy(size: Size): Surface? {
        return try {
            val reader = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 2)
            // 不做任何图像处理，只为让队列不堵（只有 dummy 进 target 时相机才会产帧）。
            reader.setOnImageAvailableListener({ r ->
                var image = r.acquireNextImage()
                while (image != null) {
                    image.close()
                    image = r.acquireNextImage()
                }
            }, cameraHandler)
            dummyReader = reader
            dummySurface = reader.surface
            dummyWidth = size.width
            dummyHeight = size.height
            Log.i(TAG, "dummy 输出流（ImageReader）：${size.width}x${size.height}，onImageAvailable 立即 close")
            reader.surface
        } catch (e: Exception) {
            Log.e(TAG, "ImageReader dummy 创建失败：${e.message}", e)
            releaseDummy()
            null
        }
    }

    private fun releaseDummy() {
        try {
            dummyReader?.close()
        } catch (e: Exception) {
            Log.d(TAG, "关闭 dummy ImageReader 异常：${e.message}")
        }
        dummyReader = null
        try {
            dummySurface?.release()
        } catch (e: Exception) {
            Log.d(TAG, "释放 dummy Surface 异常：${e.message}")
        }
        dummySurface = null
        try {
            dummyTexture?.release()
        } catch (e: Exception) {
            Log.d(TAG, "释放 dummy SurfaceTexture 异常：${e.message}")
        }
        dummyTexture = null
        dummyWidth = 0
        dummyHeight = 0
    }

    private fun startRepeatingRequest(session: CameraCaptureSession) {
        try {
            val builder = cameraDevice!!.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW)
            builder.addTarget(encoderInputSurface!!)
            // docs/archive/工单-M1-03-打回3-厂商HAL崩溃.md 第 3 节：第一档只把编码器 Surface 作为 target，
            // dummy 只参与流配置；HAL 仍崩时把 DUMMY_IN_REPEATING_REQUEST 改为 true 再试。
            if (DUMMY_IN_REPEATING_REQUEST) {
                dummySurface?.let { builder.addTarget(it) }
            }
            builder.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)
            builder.set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_ON)
            // 帧率按当前生效配置；HAL 不支持精确值时取包含/最接近的可用 AE 范围。
            builder.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, targetFpsRange(videoFps))
            // 重建后 AE 需要重新收敛，帧门控与超时保底一并复位。
            aeConverged = false
            captureStartNs = nowNs()
            session.setRepeatingRequest(builder.build(), captureCallback, cameraHandler!!)
            Log.i(
                TAG,
                "相机采集已启动：${videoWidth}x$videoHeight@$videoFps" +
                    if (DUMMY_IN_REPEATING_REQUEST && dummySurface != null) "（dummy 也在 target 中）" else ""
            )
        } catch (e: Exception) {
            Log.e(TAG, "启动重复请求失败：${e.message}", e)
            scheduleCameraRebuild("启动重复请求失败：${e.message}")
        }
    }

    private fun cameraErrorName(error: Int): String = when (error) {
        CameraDevice.StateCallback.ERROR_CAMERA_IN_USE -> "ERROR_CAMERA_IN_USE"
        CameraDevice.StateCallback.ERROR_MAX_CAMERAS_IN_USE -> "ERROR_MAX_CAMERAS_IN_USE"
        // StateCallback 未暴露该常量名，3 即 CameraAccessException.CAMERA_DISCONNECTED。
        3 -> "ERROR_CAMERA_DISCONNECTED"
        CameraDevice.StateCallback.ERROR_CAMERA_DEVICE -> "ERROR_CAMERA_DEVICE"
        CameraDevice.StateCallback.ERROR_CAMERA_SERVICE -> "ERROR_CAMERA_SERVICE"
        CameraDevice.StateCallback.ERROR_CAMERA_DISABLED -> "ERROR_CAMERA_DISABLED"
        else -> "ERROR_UNKNOWN"
    }

    /**
     * 把请求帧率收敛到相机支持的 AE 目标帧率范围：优先取包含该帧率的范围，
     * 没有则取上下界总距离最小的范围；实在查不到才回退固定范围。
     */
    private fun targetFpsRange(requestedFps: Int): Range<Int> {
        val ranges = aeFpsRanges
        if (!ranges.isNullOrEmpty()) {
            ranges.firstOrNull { requestedFps in it.lower..it.upper }?.let { return it }
            ranges.minByOrNull {
                abs(it.lower - requestedFps) + abs(it.upper - requestedFps)
            }?.let { return it }
        }
        return Range(requestedFps, requestedFps)
    }

    private val captureCallback = object : CameraCaptureSession.CaptureCallback() {
        override fun onCaptureCompleted(
            session: CameraCaptureSession,
            request: CaptureRequest,
            result: TotalCaptureResult
        ) {
            // AE 状态从 TotalCaptureResult（onCaptureCompleted 的结果对象）里读。
            val aeState = result.get(CaptureResult.CONTROL_AE_STATE)
            if (!aeConverged) {
                val waitedMs = (nowNs() - captureStartNs) / 1_000_000.0
                if (aeState == CameraMetadata.CONTROL_AE_STATE_CONVERGED) {
                    aeConverged = true
                    Log.i(TAG, "AE 已收敛（CONVERGED），等待了 %.1f ms，开始发送帧".format(waitedMs))
                } else if (waitedMs >= AE_CONVERGE_TIMEOUT_MS) {
                    // docs/archive/工单-M1-03-打回4-Activity不能finish.md 第 4.1 节：设备可能一直停在 SEARCHING，AE 永远
                    // 不收敛。安全相关的采集链路不能因此永远不出帧——最多等 2 秒，
                    // 超时照发并打 warning，保证 VIDEO>0。
                    aeConverged = true
                    Log.w(
                        TAG,
                        "AE 等待 ${AE_CONVERGE_TIMEOUT_MS}ms 未收敛（最后状态 $aeState），" +
                            "超时保底放行，开始发送帧"
                    )
                }
            }
        }
    }

    private fun teardownCameraAndEncoder() {
        // 相机/编码器重建后 PTS 基可能变化，重置视频时间戳锚点。
        resetVideoPtsAnchor()
        try {
            captureSession?.close()
        } catch (e: Exception) {
            Log.d(TAG, "关闭相机会话异常：${e.message}")
        }
        captureSession = null
        try {
            cameraDevice?.close()
        } catch (e: Exception) {
            Log.d(TAG, "关闭相机异常：${e.message}")
        }
        cameraDevice = null
        try {
            encoder?.stop()
        } catch (e: Exception) {
            Log.d(TAG, "停止编码器异常：${e.message}")
        }
        try {
            encoder?.release()
        } catch (e: Exception) {
            Log.d(TAG, "释放编码器异常：${e.message}")
        }
        encoder = null
        encoderInputSurface = null
        releaseDummy()
    }

    // ------------------------------------------------------------------
    // 时钟与序号（约束 1）
    // ------------------------------------------------------------------

    /**
     * 全模块唯一取时间入口（PING/PONG 与 POSE 包时间戳）。注意相机 SENSOR_TIMESTAMP
     * 的绝对基是厂商私有时钟（非 elapsed 域），视频包时间戳须经
     * [videoPtsInNowDomain] 锚定回本域后再发送，不能直接取用。
     */
    private fun nowNs(): Long = SystemClock.elapsedRealtimeNanos()

    private fun nextVideoSequence(): Long {
        val current = videoSequence
        videoSequence = (videoSequence + 1) and 0xFFFF_FFFFL
        return current
    }

    private fun nextControlSequence(): Long {
        val current = controlSequence
        controlSequence = (controlSequence + 1) and 0xFFFF_FFFFL
        return current
    }

    @Synchronized
    private fun nextSpeakStatusSequence(): Long {
        val current = speakStatusSequence
        speakStatusSequence = (speakStatusSequence + 1) and 0xFFFF_FFFFL
        return current
    }

    private fun nextPoseSequence(): Long {
        val current = poseSequence
        poseSequence = (poseSequence + 1) and 0xFFFF_FFFFL
        return current
    }

    private fun nextAudioSequence(): Long {
        val current = audioSequence
        audioSequence = (audioSequence + 1) and 0xFFFF_FFFFL
        return current
    }

    private fun enqueuePacket(packet: LinkPacket) {
        linkClient?.enqueue(packet)
    }

    // ------------------------------------------------------------------
    // POSE 通道（工单 M1-05 §3.2）：rotation vector → 100ms 采样批
    // ------------------------------------------------------------------

    /**
     * 启动 rotation vector 采样。回调与批发送都钉在 [sensorThread] 上，
     * [poseSamples] 只在该线程读写，不需要额外加锁。
     *
     * 约束 1：`SensorEvent.timestamp` 与 `elapsedRealtimeNanos` 同域，直接透传
     * 为采样时间戳，包时间戳则取批发送时刻 [nowNs]。
     * 约束 2：批间隔 100ms（约 10 采样/批），不小于 50ms。
     */
    private fun startPoseCapture() {
        if (!running) return
        val sensorManager = getSystemService(SensorManager::class.java)
        val sensor = sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)
        if (sensor == null) {
            // 能力上报 hasRotationVector=false（运行时真读），本通道自动停用。
            Log.w(TAG, "无 rotation vector 传感器，跳过 POSE 通道")
            return
        }
        val thread = HandlerThread("glasses-pose").apply { start() }
        sensorThread = thread
        val handler = Handler(thread.looper)
        sensorHandler = handler

        val listener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                if (!running) return
                try {
                    val q = FloatArray(4)
                    // getQuaternionFromVector 输出为 [w,x,y,z]；线格式按
                    // 消费端 pose.csv 约定为 (x,y,z,w)，映射为 q[1],q[2],q[3],q[0]。
                    SensorManager.getQuaternionFromVector(q, event.values)
                    poseSamples.add(
                        PosePayloadCodec.PoseSample(
                            timestampNs = event.timestamp,
                            qx = q[1],
                            qy = q[2],
                            qz = q[3],
                            qw = q[0],
                            accuracy = event.accuracy
                        )
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "rotation vector 采样失败（丢弃该采样）：${e.message}")
                }
            }

            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
        }
        poseListener = listener
        val registered = sensorManager.registerListener(
            listener, sensor, SENSOR_PERIOD_US, 0, handler
        )
        if (!registered) {
            Log.w(TAG, "rotation vector 注册失败，跳过 POSE 通道")
            thread.quitSafely()
            sensorThread = null
            sensorHandler = null
            poseListener = null
            return
        }

        lateinit var flushTask: Runnable
        flushTask = Runnable {
            if (!running) return@Runnable
            flushPoseBatch()
            if (running) handler.postDelayed(flushTask, POSE_BATCH_INTERVAL_MS)
        }
        poseFlushTask = flushTask
        handler.postDelayed(flushTask, POSE_BATCH_INTERVAL_MS)
        Log.i(
            TAG,
            "POSE 通道已启动：rotation vector @ ${SENSOR_PERIOD_US}us，批间隔 ${POSE_BATCH_INTERVAL_MS}ms"
        )
    }

    /**
     * 把已积累的采样编码为 POSE 包入队。只在 [sensorThread] 上调用。
     * 链路未处于媒体发送态（未握手或已断线）时丢弃整批，避免队列堆积过期位姿。
     */
    private fun flushPoseBatch() {
        if (poseSamples.isEmpty()) return
        val batch = poseSamples.toList()
        poseSamples.clear()
        if (!linkMediaActive) {
            Log.d(TAG, "链路未就绪，丢弃 POSE 批（${batch.size} 采样）")
            return
        }
        try {
            enqueuePacket(
                LinkPacket(
                    channel = LinkChannel.POSE,
                    flags = LinkFlags.NONE,
                    sequence = nextPoseSequence(),
                    senderTimestampNs = nowNs(),
                    payload = PosePayloadCodec.encode(batch)
                )
            )
        } catch (e: Exception) {
            Log.w(TAG, "POSE 批编码失败（丢弃 ${batch.size} 采样）：${e.message}")
        }
    }

    /** 停止传感器采集：刷掉剩余采样、注销监听并退出线程。 */
    private fun stopPoseCapture() {
        val handler = sensorHandler
        val listener = poseListener
        handler?.post {
            flushPoseBatch()
            if (listener != null) {
                getSystemService(SensorManager::class.java).unregisterListener(listener)
            }
        }
        sensorThread?.quitSafely()
        sensorThread = null
        sensorHandler = null
        poseListener = null
        poseFlushTask = null
    }

    // ------------------------------------------------------------------
    // AUDIO 通道（工单 V-01 §3.2）：AudioRecord → 20ms PCM16 包
    // ------------------------------------------------------------------

    /**
     * 启动音频采集。专用线程（不占相机/传感器 HandlerThread），
     * 权限缺失或持续失败都如实上报并退避重试，**不 stopSelf**
     * （与相机路径「缺 CAMERA 权限才允许自杀」不同，工单 V-01 明确要求）。
     */
    private fun startAudioCapture() {
        if (!running) return
        if (audioThread?.isAlive == true) {
            Log.d(TAG, "音频采集线程已在运行，跳过重复启动")
            return
        }
        val thread = Thread({ audioCaptureLoop() }, "glass-audio")
        thread.isDaemon = true
        audioThread = thread
        thread.start()
    }

    /**
     * 停止音频采集。中断阻塞中的 [AudioRecord.read]；线程在 [running] 置 false
     * 后自行退出（READ_BLOCKING 每 20ms 返回一次，最长一个块即可感知退出）。
     */
    private fun stopAudioCapture() {
        audioThread?.interrupt()
        audioThread = null
    }

    private fun audioCaptureLoop() {
        var record: AudioRecord? = null
        var framesRead = 0L
        var consecutiveZeroReads = 0
        var lastDroppedLogNs = 0L
        try {
            while (running && !Thread.currentThread().isInterrupted) {
                // 换代守卫：stopAudioCapture() 把 audioThread 置 null（或指向新线程），
                // 旧线程即使被 interrupt 没打断 read，也会在这里退出，避免双采集。
                if (audioThread !== Thread.currentThread()) return
                if (record == null) {
                    record = try {
                        createAudioRecord()
                    } catch (e: SecurityException) {
                        // 缺 RECORD_AUDIO 权限：如实上报，退避重试，不 stopSelf。
                        onAudioIssue(AUDIO_ISSUE_NO_PERMISSION)
                        if (!sleepInterruptibly(AUDIO_RETRY_DELAY_MS)) return
                        null
                    } catch (e: Exception) {
                        onAudioIssue("眼镜麦克风不可用：${e.message}")
                        if (!sleepInterruptibly(AUDIO_RETRY_DELAY_MS)) return
                        null
                    }
                    if (record != null) {
                        // 修复（真机实测）：创建 AudioRecord 后必须显式 startRecording()，
                        // 否则 read 永远返回 0（V-01 真机回归发现）。
                        try {
                            record.startRecording()
                        } catch (e: Exception) {
                            onAudioIssue("AudioRecord.startRecording 失败：${e.message}")
                            closeAudioRecord(record)
                            record = null
                            if (!sleepInterruptibly(AUDIO_RETRY_DELAY_MS)) return
                            continue
                        }
                        // 新建采集器 = 新的帧时钟，帧位置计数从 0 重新开始。
                        framesRead = 0
                        audioIssueReported = false
                        audioFailureCount = 0
                        Log.i(
                            TAG,
                            "AUDIO 通道已启动：AudioRecord 16kHz mono PCM16，20ms/包（${AudioPayloadCodec.PACKET_BYTES}B）"
                        )
                    }
                    continue
                }

                val chunk = ShortArray(AudioPayloadCodec.FRAMES_PER_PACKET)
                val frames = try {
                    record.read(chunk, 0, chunk.size, AudioRecord.READ_BLOCKING)
                } catch (e: SecurityException) {
                    // 运行中权限被撤销：整块丢弃，重建采集器。
                    onAudioIssue(AUDIO_ISSUE_NO_PERMISSION)
                    closeAudioRecord(record)
                    record = null
                    if (!sleepInterruptibly(AUDIO_RETRY_DELAY_MS)) return
                    continue
                } catch (e: Exception) {
                    onAudioIssue("音频读取异常：${e.message}")
                    closeAudioRecord(record)
                    record = null
                    if (!sleepInterruptibly(AUDIO_RETRY_DELAY_MS)) return
                    continue
                }
                if (frames <= 0) {
                    // 真机预热：首次 read 可能返回 0（采集器刚启动），
                    // 同一采集器重试几次再判失败，避免一读 0 就重建的抖动循环。
                    consecutiveZeroReads++
                    if (consecutiveZeroReads < MAX_CONSECUTIVE_ZERO_READS) {
                        if (!sleepInterruptibly(READ_RETRY_INTERVAL_MS)) return
                        continue
                    }
                    onAudioIssue("AudioRecord.read 返回 $frames（连续 $consecutiveZeroReads 次）")
                    closeAudioRecord(record)
                    record = null
                    consecutiveZeroReads = 0
                    if (!sleepInterruptibly(AUDIO_RETRY_DELAY_MS)) return
                    continue
                }
                consecutiveZeroReads = 0

                // 换算首采样时刻：优先用 AudioRecord.getTimestamp（CLOCK_MONOTONIC 域，
                // 标记 framePosition 帧的采集时刻）；厂商 HAL 不支持时（雷鸟 X3 Pro
                // 真机实测返回失败）回退 elapsedRealtimeNanos 到达时刻 − 块时长，
                // 与手机端 S21MicrophoneSource 同域同法，避免拿不到时间戳就无限重建。
                val timestamp = AudioTimestamp()
                var hasTimestamp = false
                try {
                    hasTimestamp =
                        record.getTimestamp(timestamp, AudioTimestamp.TIMEBASE_MONOTONIC) != 0
                } catch (e: Exception) {
                    Log.w(TAG, "AudioRecord.getTimestamp 异常（回退到达时刻）：${e.message}")
                }
                val firstSampleNs = if (hasTimestamp) {
                    timestamp.nanoTime +
                        (framesRead - timestamp.framePosition) * AudioPayloadCodec.FRAME_DURATION_NS
                } else {
                    nowNs() - frames * AudioPayloadCodec.FRAME_DURATION_NS
                }
                framesRead += frames

                if (!linkMediaActive) {
                    // 链路未握手或已断线：整块丢弃，避免发送队列堆积过期音频。
                    val now = nowNs()
                    if (now - lastDroppedLogNs >= 1_000_000_000L) {
                        Log.d(TAG, "链路未就绪，丢弃音频块（$frames 采样）")
                        lastDroppedLogNs = now
                    }
                    continue
                }
                try {
                    enqueuePacket(
                        LinkPacket(
                            channel = LinkChannel.AUDIO,
                            flags = LinkFlags.NONE,
                            sequence = nextAudioSequence(),
                            senderTimestampNs = firstSampleNs,
                            payload = AudioPayloadCodec.encode(
                                shortToPcm16Le(chunk, frames),
                                frames
                            )
                        )
                    )
                    // 发送成功 = 故障期结束，下次失败再上报。
                    audioFailureCount = 0
                    audioIssueReported = false
                } catch (e: Exception) {
                    onAudioIssue("音频包编码失败：${e.message}")
                }
            }
        } finally {
            closeAudioRecord(record)
            Log.i(TAG, "音频采集线程退出")
        }
    }

    /** PCM16 短整型 → 线格式固定小端字节。AudioRecord 读入的是原生字节序，Android 恒为 LE。 */
    private fun shortToPcm16Le(samples: ShortArray, count: Int): ByteArray {
        val bytes = ByteArray(count * AudioPayloadCodec.BYTES_PER_SAMPLE)
        for (i in 0 until count) {
            val v = samples[i].toInt()
            bytes[i * 2] = (v and 0xFF).toByte()
            bytes[i * 2 + 1] = ((v ushr 8) and 0xFF).toByte()
        }
        return bytes
    }

    /**
     * 音频路径问题（audio 线程调用）：如实上报但一次故障期只报一次，
     * 发出音频包后复位；链路未握手时跳过上报，等下一次失败再试，
     * 避免把报告积压在发送队列里变成迟到消息（与相机异常上报同模式）。
     * 复用 SPEAK_STATUS 上行通道发 UTF-8 文本，**绝不 stopSelf**。
     */
    private fun onAudioIssue(message: String) {
        audioFailureCount++
        if (audioIssueReported) {
            Log.d(TAG, "音频问题持续：$message（累计 $audioFailureCount 次）")
            return
        }
        Log.w(TAG, "音频问题（累计 $audioFailureCount 次）：$message")
        if (!handshakeSucceeded) {
            Log.d(TAG, "音频问题待上报，但链路尚未握手成功，等下一次失败再试")
            return
        }
        audioIssueReported = true
        enqueuePacket(
            LinkPacket(
                channel = LinkChannel.SPEAK_STATUS,
                flags = LinkFlags.NONE,
                sequence = nextSpeakStatusSequence(),
                senderTimestampNs = nowNs(),
                payload = message.toByteArray(Charsets.UTF_8)
            )
        )
    }

    private fun closeAudioRecord(record: AudioRecord?) {
        if (record == null) return
        try {
            record.release()
        } catch (e: Exception) {
            Log.d(TAG, "释放 AudioRecord 异常：${e.message}")
        }
    }

    private fun sleepInterruptibly(ms: Long): Boolean {
        return try {
            Thread.sleep(ms)
            running
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }
    }

    // ------------------------------------------------------------------
    // 握手结果
    // ------------------------------------------------------------------

    private enum class HandshakeOutcome {
        ACKED,
        REJECTED,
        TIMEOUT,
        SOCKET_ERROR
    }

    // ------------------------------------------------------------------
    // TCP 链路客户端（重连退避走 LinkStateMachine）
    // ------------------------------------------------------------------

    private inner class LinkClient(
        private val host: String,
        private val port: Int,
        private val caps: GlassCapabilities,
        private val network: Network? = null
    ) {
        private val stateMachine = LinkStateMachine()
        private val queue = LinkSendQueue(QUEUE_CAPACITY)
        private val queueLock = java.lang.Object()
        private val handshakeLock = java.lang.Object()

        @Volatile
        private var socket: Socket? = null

        @Volatile
        private var linkFailure: String? = null

        @Volatile
        private var handshakeOutcome: HandshakeOutcome? = null

        private var rejectReason: String? = null
        private var readerThread: Thread? = null

        fun enqueue(packet: LinkPacket) {
            val before = queue.droppedVideoPacketCount
            synchronized(queueLock) {
                queue.offer(packet)
                queueLock.notifyAll()
            }
            val after = queue.droppedVideoPacketCount
            if (after > before) {
                Log.w(
                    TAG,
                    "发送队列背压丢包（仅 VIDEO）：累计丢 $after 包，其中关键帧 ${queue.droppedKeyframeCount}"
                )
            }
        }

        fun shutdown() {
            closeSocket()
            synchronized(queueLock) { queueLock.notifyAll() }
            synchronized(handshakeLock) { handshakeLock.notifyAll() }
            readerThread?.interrupt()
            Log.i(
                TAG,
                "发送队列统计：累计丢视频 ${queue.droppedVideoPacketCount} 包（关键帧 ${queue.droppedKeyframeCount}）"
            )
        }

        /** 链路主循环：连接 → 握手 → 发送，断线后按状态机退避重连（约束 5）。 */
        fun run() {
            Log.i(TAG, "链路线程启动，目标 $host:$port")
            stateMachine.onEvent(LinkEvent.StartRequested)
            while (running && !Thread.currentThread().isInterrupted) {
                if (stateMachine.state == LinkState.IDLE) return
                val delayMs = stateMachine.backoffDelayMs(stateMachine.consecutiveFailures)
                if (delayMs > 0L) {
                    Log.i(TAG, "重连退避 ${delayMs}ms（连续失败 ${stateMachine.consecutiveFailures} 次）")
                    if (!sleepInterruptibly(delayMs)) return
                }
                if (!running) return
                runOneSession()
            }
        }

        private fun runOneSession() {
            linkFailure = null
            handshakeOutcome = null
            rejectReason = null
            // 会话开始（含重连退避期间）传感器包不入队，握手完成后才恢复。
            linkMediaActive = false
            // 上一会话的握手结果作废：新会话必须重新握手并重发 CODEC_CONFIG
            // 后才允许发送 VIDEO（否则手机端会收到「配置之前的视频帧」被丢弃）。
            handshakeSucceeded = false
            // 清掉上一会话积压的视频帧，保证新会话的 CODEC_CONFIG 立即排到队首。
            queue.clear()

            if (!openSocket()) return
            stateMachine.onEvent(LinkEvent.TransportConnected)

            if (!sendHello()) {
                stateMachine.onEvent(LinkEvent.PeerDisconnected("发送 HELLO 失败"))
                closeSocket()
                return
            }

            startReader()
            when (awaitHandshake()) {
                HandshakeOutcome.ACKED -> {
                    handshakeSucceeded = true
                    linkMediaActive = true
                    // 约束 4：每次重连握手完成后重发缓存的 SPS/PPS（下一帧触发）。
                    codecConfigPending = true
                    requestSyncFrame()
                    stateMachine.onEvent(LinkEvent.HandshakeCompleted(caps))
                    Log.i(TAG, "握手完成，状态 ${stateMachine.state}，进入媒体发送")
                    onLinkEstablished(host)
                    val reason = runWriterLoop()
                    if (reason == null) return // 服务停止
                    Log.w(TAG, "链路中断：$reason")
                    onLinkLost(reason)
                    stateMachine.onEvent(LinkEvent.PeerDisconnected(reason))
                    closeSocket()
                }
                HandshakeOutcome.REJECTED -> {
                    stateMachine.onEvent(
                        LinkEvent.HandshakeRejected(rejectReason ?: "对端拒绝握手")
                    )
                    closeSocket()
                }
                HandshakeOutcome.TIMEOUT -> {
                    Log.w(TAG, "等待 HELLO_ACK 超时")
                    stateMachine.onEvent(LinkEvent.HeartbeatTimeout)
                    closeSocket()
                }
                HandshakeOutcome.SOCKET_ERROR -> {
                    stateMachine.onEvent(LinkEvent.PeerDisconnected("握手期间连接断开"))
                    closeSocket()
                }
            }
        }

        private fun openSocket(): Boolean {
            return try {
                // P2P 模式下用网络 socket factory 创建 socket，保证路由走 P2P 接口。
                val s = if (network != null) {
                    network.getSocketFactory().createSocket() as Socket
                } else {
                    Socket()
                }
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
                s.soTimeout = READ_TIMEOUT_MS
                socket = s
                Log.i(TAG, "TCP 已连接 $host:$port" + if (network != null) "（P2P）" else "")
                true
            } catch (e: Exception) {
                Log.w(TAG, "TCP 连接失败 $host:$port：${e.message}")
                stateMachine.onEvent(LinkEvent.TransportFailed(e.message ?: "连接失败"))
                false
            }
        }

        private fun sendHello(): Boolean {
            val payload = ControlCodec.encode(ControlMessage.Hello(caps))
            val packet = LinkPacket(
                channel = LinkChannel.CONTROL,
                flags = LinkFlags.NONE,
                sequence = nextControlSequence(),
                senderTimestampNs = nowNs(),
                payload = payload
            )
            return try {
                writePacket(packet)
                Log.i(TAG, "HELLO 已发送（protocolVersion=${LinkWire.VERSION}，videoModes=${caps.videoModes.size}）")
                true
            } catch (e: Exception) {
                Log.w(TAG, "发送 HELLO 失败：${e.message}")
                false
            }
        }

        private fun startReader() {
            val s = socket ?: return
            val thread = Thread {
                val frameReader = LinkFrameReader()
                val buffer = ByteArray(8192)
                try {
                    val input = s.getInputStream()
                    while (running && linkFailure == null && socket === s) {
                        val n = input.read(buffer)
                        if (n < 0) {
                            failLinkFrom(s, "对端关闭连接")
                            break
                        }
                        if (n > 0) {
                            frameReader.append(buffer, 0, n)
                            while (true) {
                                val packet = frameReader.next() ?: break
                                handleIncoming(packet)
                            }
                        }
                    }
                } catch (e: LinkProtocolException) {
                    // 流错位不可恢复，直接断线重连（不尝试重同步）。
                    failLinkFrom(s, "链路协议错误：${e.message}")
                } catch (e: SocketTimeoutException) {
                    failLinkFrom(s, "读取超时")
                } catch (e: Exception) {
                    failLinkFrom(s, "读取异常：${e.message}")
                }
            }
            thread.name = "glass-link-reader"
            thread.isDaemon = true
            readerThread = thread
            thread.start()
        }

        private fun handleIncoming(packet: LinkPacket) {
            if (packet.channel != LinkChannel.CONTROL) return
            when (val message = ControlCodec.decode(packet.payload)) {
                is ControlMessage.HelloAck -> {
                    handshakeOutcome = HandshakeOutcome.ACKED
                    synchronized(handshakeLock) { handshakeLock.notifyAll() }
                    applyNegotiatedConfig(message.config)
                }
                is ControlMessage.HelloReject -> {
                    rejectReason = message.reason
                    handshakeOutcome = HandshakeOutcome.REJECTED
                    synchronized(handshakeLock) { handshakeLock.notifyAll() }
                }
                is ControlMessage.Ping -> {
                    // 约束 1：t1 回显、t2=收到时刻、t3=发出时刻，全部走 nowNs()。
                    val t2 = nowNs()
                    val t3 = nowNs()
                    enqueue(
                        LinkPacket(
                            channel = LinkChannel.CONTROL,
                            flags = LinkFlags.NONE,
                            sequence = nextControlSequence(),
                            senderTimestampNs = t3,
                            payload = ControlCodec.encode(ControlMessage.Pong(message.t1, t2, t3))
                        )
                    )
                }
                is ControlMessage.Bye -> failLink("收到 BYE：${message.reason}")
                // 眼镜侧不应收到 PONG / HELLO，忽略并记日志。
                is ControlMessage.Pong -> Log.d(TAG, "收到意外的 PONG，忽略")
                is ControlMessage.Hello -> Log.d(TAG, "收到意外的 HELLO，忽略")
            }
        }

        private fun awaitHandshake(): HandshakeOutcome {
            val deadlineNs = nowNs() + HANDSHAKE_TIMEOUT_MS * 1_000_000L
            synchronized(handshakeLock) {
                while (handshakeOutcome == null && linkFailure == null) {
                    val remainingMs = (deadlineNs - nowNs()) / 1_000_000L
                    if (remainingMs <= 0L) return HandshakeOutcome.TIMEOUT
                    try {
                        handshakeLock.wait(remainingMs)
                    } catch (e: InterruptedException) {
                        Thread.currentThread().interrupt()
                        return HandshakeOutcome.SOCKET_ERROR
                    }
                }
            }
            return handshakeOutcome ?: HandshakeOutcome.SOCKET_ERROR
        }

        /** 发送循环：阻塞在队列上，队列空时等待被入队唤醒（非轮询）。 */
        private fun runWriterLoop(): String? {
            while (running && linkFailure == null) {
                val packet = nextPacketOrWait() ?: return linkFailure
                try {
                    writePacket(packet)
                } catch (e: Exception) {
                    failLink("发送失败：${e.message}")
                }
            }
            return linkFailure
        }

        private fun nextPacketOrWait(): LinkPacket? {
            synchronized(queueLock) {
                while (running && linkFailure == null) {
                    queue.poll()?.let { return it }
                    try {
                        queueLock.wait(2000L)
                    } catch (e: InterruptedException) {
                        Thread.currentThread().interrupt()
                        return null
                    }
                }
                return null
            }
        }

        /** 仅当 [sessionSocket] 仍是当前会话的 socket 时才报故障，防止旧会话收尾误伤新会话。 */
        private fun failLinkFrom(sessionSocket: Socket, reason: String) {
            if (socket !== sessionSocket) return
            failLink(reason)
        }

        private fun writePacket(packet: LinkPacket) {
            val bytes = LinkProtocol.encode(packet)
            val output = socket?.getOutputStream() ?: throw IllegalStateException("socket 已关闭")
            output.write(bytes)
            output.flush()
        }

        private fun failLink(reason: String) {
            if (linkFailure == null) linkFailure = reason
            closeSocket()
            synchronized(queueLock) { queueLock.notifyAll() }
            synchronized(handshakeLock) { handshakeLock.notifyAll() }
        }

        private fun closeSocket() {
            val s = socket
            socket = null
            // 任何断线/关闭都停止传感器包入队，避免队列堆积过期位姿。
            linkMediaActive = false
            // 同理：断线后未完成新握手前不发送视频帧（CODEC_CONFIG 必须先到）。
            handshakeSucceeded = false
            try {
                s?.close()
            } catch (e: Exception) {
                Log.d(TAG, "关闭 socket 异常：${e.message}")
            }
        }

        private fun sleepInterruptibly(ms: Long): Boolean {
            return try {
                Thread.sleep(ms)
                running
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                false
            }
        }
    }

    /**
     * 应用手机端在 HELLO_ACK 里下发的会话配置（分辨率/帧率/码率）。
     * 相机在握手前就已启动，配置变化时需要在相机线程上拆掉重建——
     * 链路不受影响，重建后编码器会重新输出 CODEC_CONFIG + IDR，接收侧按
     * 新配置续流（m1_record.py 在每次收到参数集后重新对齐）。
     */
    private fun applyNegotiatedConfig(config: SessionConfig) {
        if (config.videoWidth <= 0 || config.videoHeight <= 0) return
        val fps = config.videoFps.coerceIn(1, 30)
        val bitrate = config.videoBitrateBps.coerceIn(200_000, 8_000_000)
        val changed = config.videoWidth != targetWidth ||
            config.videoHeight != targetHeight ||
            fps != targetFps ||
            bitrate != targetBitRate
        if (!changed) return
        targetWidth = config.videoWidth
        targetHeight = config.videoHeight
        targetFps = fps
        targetBitRate = bitrate
        Log.i(
            TAG,
            "应用协商配置：${targetWidth}x$targetHeight@$targetFps ${targetBitRate}bps，重建相机/编码器"
        )
        val handler = cameraHandler
        if (handler == null) {
            // 相机线程尚未就绪（理论上握手晚于相机启动，此分支只是防御）。
            videoWidth = targetWidth
            videoHeight = targetHeight
            videoFps = targetFps
            videoBitRate = targetBitRate
            return
        }
        handler.post {
            if (!running) return@post
            teardownCameraAndEncoder()
            startCameraPipeline()
        }
    }

    companion object {
        private const val TAG = "GlassLinkService"
        /** 视频 PTS 锚点 EMA 除数：越大越平滑，越小跟踪漂移越快。 */
        private const val ANCHOR_EMA_DIVISOR = 32L
        private const val CAMERA_ID = "0"
        /** Intent extra 键，与 adb 的 `--es host 127.0.0.1` 对应（透传自 GlassLinkActivity）。 */
        private const val EXTRA_HOST = "host"
        private const val TCP_PORT = 47810
        private const val MIME_TYPE_AVC = "video/avc"
        private const val VIDEO_WIDTH = 640
        private const val VIDEO_HEIGHT = 360
        private const val VIDEO_FPS = 15
        /**
         * 640×360@15 的码率。实测眼镜 Wi-Fi 链路吞吐约 1.5–2 Mbps，1.2 Mbps 视频
         * 加协议开销会把链路打满，发送队列被迫积压十几秒的视频，手机端整批帧
         * 被新鲜度门限丢弃，表现为画面十几秒才刷新一次。降到 600 kbps 后链路
         * 有余量，队列不再积压，端到端延迟回到几百毫秒级。
         */
        private const val VIDEO_BIT_RATE = 600_000
        private const val CONNECT_TIMEOUT_MS = 3_000
        private const val READ_TIMEOUT_MS = 5_000
        private const val HANDSHAKE_TIMEOUT_MS = 10_000

        /** POSE 采样周期：5ms 目标（≥100Hz，实际由 HAL 决定）。 */
        private const val SENSOR_PERIOD_US = 5_000

        /** POSE 批间隔（工单 M1-05 约束 2：100ms 即可，不许小于 50ms）。 */
        private const val POSE_BATCH_INTERVAL_MS = 100L

        /** 音频采集失败重试间隔：持续失败只如实上报一次，不 stopSelf。 */
        private const val AUDIO_RETRY_DELAY_MS = 2_000L

        /** 首次 read=0 的预热重试次数与间隔（真机实测修复）。 */
        private const val MAX_CONSECUTIVE_ZERO_READS = 5
        private const val READ_RETRY_INTERVAL_MS = 20L

        private const val AUDIO_ISSUE_NO_PERMISSION = "眼镜麦克风权限缺失"

        /**
         * 相机/编码器路径的退避表（docs/archive/工单-M1-03-打回3-厂商HAL崩溃.md 第 4 节）：厂商 HAL 崩溃后
         * 进程重启需要时间，首档至少 1s，不用链路的 250ms 档；之后 2s / 4s 封顶。
         */
        private val CAMERA_BACKOFF_MS = longArrayOf(1_000, 2_000, 4_000)

        /** 相机连续失败达到该次数后，通过链路向手机通报一次「眼镜相机异常」。 */
        private const val CAMERA_ABNORMAL_REPORT_THRESHOLD = 3

        private const val CAMERA_ABNORMAL_REPORT_TEXT = "眼镜相机异常"

        /**
         * docs/archive/工单-M1-03-打回3-厂商HAL崩溃.md 第 3 节：dummy 流是否也加入 repeating request target。
         * 先试 false（dummy 只参与流配置，SurfaceTexture 无需消费、不会堵队列）。
         * 若加了第二路流 HAL 仍然崩，改为 true 再测——此时 dummy 走 ImageReader
         * 自动消费（onImageAvailable 立即 close）。
         */
        private const val DUMMY_IN_REPEATING_REQUEST = false

        /**
         * 30 包 ≈ 2 秒 @15fps。低延迟直播以新鲜度优先：队列只吸收瞬时抖动，
         * 不承载长时间积压（积压=旧帧，对实时画面毫无价值）。
         */
        private const val QUEUE_CAPACITY = 30

        /** AE 收敛等待上限：最多等 2 秒，超时保底放行（docs/archive/工单-M1-03-打回4-Activity不能finish.md 第 4.1 节）。 */
        private const val AE_CONVERGE_TIMEOUT_MS = 2_000L

        /** 服务是否仍在运行（主线程写，Activity 主线程读；用于被回收后自动拉起窗口）。 */
        @Volatile
        var serviceRunning = false

        /** 当前链路状态文案（Service 写，Activity 轮询显示）。 */
        @Volatile
        var currentStatus: String? = null

        private const val CHANNEL_ID = "glass_link"
        private const val NOTIFICATION_ID = 1
    }
}
