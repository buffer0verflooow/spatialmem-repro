package com.example.blindassist.link

/**
 * 眼镜↔手机链路的线格式与消息模型。
 *
 * 本包**不依赖任何 Android API**，因此可以完整跑 JVM 单元测试。传输、编解码、
 * 相机等设备相关部分在 `capture/` 下另行实现，只调用这里的纯逻辑。
 *
 * 线格式（大端，头部固定 20 字节）：
 * ```
 * 偏移  长度  字段
 * 0     2    magic 'B''A' (0x42 0x41)
 * 2     1    version
 * 3     1    channel
 * 4     1    flags
 * 5     3    payload 长度（最大 16777215，实际再受 maxPayloadBytes 限制）
 * 8     4    sequence（每通道独立，uint32，允许回绕）
 * 12    8    发送方单调时钟纳秒
 * 20    …    payload
 * ```
 *
 * 时间戳一律是**发送方自己的单调时钟**，不做任何跨设备换算 —— 换算由
 * [ClockSyncEstimator] 在接收侧完成，这样线格式不依赖于对齐是否已收敛。
 */
object LinkWire {
    const val MAGIC_0: Byte = 0x42 // 'B'
    const val MAGIC_1: Byte = 0x41 // 'A'
    const val VERSION: Int = 1
    const val HEADER_SIZE: Int = 20

    /** 线格式本身的上限（3 字节长度字段）。实际上限由读取器的 maxPayloadBytes 决定。 */
    const val ABSOLUTE_MAX_PAYLOAD: Int = 0xFF_FF_FF

    /**
     * 默认负载上限 2 MiB。
     *
     * 640×360 的 H.264 关键帧远小于此；1080p 高质量 I 帧约 300KB–1MB。留 2 MiB 的余量，
     * 同时确保**一个损坏或恶意的长度字段不会导致巨额分配** —— 这是 R0-1 里
     * 「包长有上限」那条要求的落点。
     */
    const val DEFAULT_MAX_PAYLOAD: Int = 2 * 1024 * 1024
}

/**
 * 通道。单条 TCP 连接上按通道复用。
 *
 * 选单连接而不是多连接的理由见 `docs/M1-眼镜手机链路设计.md` 第 3 节：
 * 多连接会带来「控制通道活着但媒体通道死了」这类半死状态，判活逻辑显著复杂化。
 */
enum class LinkChannel(val code: Int) {
    /** 上行：H.264 访问单元，一包一帧。 */
    VIDEO(0x01),

    /** 上行：音频（PCM16 或 Opus，由握手协商）。 */
    AUDIO(0x02),

    /** 上行：IMU 采样批。 */
    IMU(0x03),

    /** 上行：姿态采样批。 */
    POSE(0x04),

    /** 上行：镜腿触控、佩戴状态等输入事件。 */
    INPUT(0x05),

    /** 双向：握手、心跳、时钟对齐。 */
    CONTROL(0x10),

    /** 下行：播报指令（文本或 promptId）。 */
    SPEAK(0x11),

    /** 上行：播报生命周期回执。 */
    SPEAK_STATUS(0x12);

    val isUplink: Boolean
        get() = this != SPEAK

    /**
     * 控制类通道在发送队列里插队。
     *
     * 视频包会占住 socket 写缓冲，控制包排在后面会被队头阻塞。让控制类插队，
     * 阻塞就只剩「当前正在写的那一个包」，最坏情况可算：
     * 50KB 关键帧 / 10Mbps 有效带宽 ≈ 40ms，在 700ms 端到端预算内可接受。
     */
    val isHighPriority: Boolean
        get() = this == CONTROL || this == SPEAK || this == SPEAK_STATUS

    companion object {
        private val byCode = entries.associateBy { it.code }
        fun fromCode(code: Int): LinkChannel? = byCode[code]
    }
}

object LinkFlags {
    const val NONE: Int = 0x00

    /** VIDEO：本包是关键帧。接收侧解码器重连后必须等到关键帧才能出画。 */
    const val KEYFRAME: Int = 0x01

    /** VIDEO：本包是编码器配置（SPS/PPS），不是可显示帧。 */
    const val CODEC_CONFIG: Int = 0x02

    /** 发送方即将正常关闭本通道。 */
    const val END_OF_STREAM: Int = 0x04

    fun has(flags: Int, flag: Int): Boolean = (flags and flag) != 0
}

/**
 * 一个完整的链路包。
 *
 * [senderTimestampNs] 是**发送方时钟域**的值。接收侧要换算到本机时钟域，
 * 必须经过 [ClockSyncEstimator.toReceiverNs]；在对齐收敛前不得直接把它
 * 当本机时间用（否则 `captureToEventLatencyMs` 会等于两机时钟偏移，是个无意义的大数）。
 */
data class LinkPacket(
    val channel: LinkChannel,
    val flags: Int,
    val sequence: Long,
    val senderTimestampNs: Long,
    val payload: ByteArray
) {
    init {
        require(sequence in 0..0xFFFF_FFFFL) { "sequence 超出 uint32 范围: $sequence" }
    }

    val isKeyframe: Boolean get() = LinkFlags.has(flags, LinkFlags.KEYFRAME)
    val isCodecConfig: Boolean get() = LinkFlags.has(flags, LinkFlags.CODEC_CONFIG)

    // data class 对 ByteArray 用引用相等，这里改成内容相等，否则测试里的断言会误判。
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is LinkPacket) return false
        return channel == other.channel &&
            flags == other.flags &&
            sequence == other.sequence &&
            senderTimestampNs == other.senderTimestampNs &&
            payload.contentEquals(other.payload)
    }

    override fun hashCode(): Int {
        var result = channel.hashCode()
        result = 31 * result + flags
        result = 31 * result + sequence.hashCode()
        result = 31 * result + senderTimestampNs.hashCode()
        result = 31 * result + payload.contentHashCode()
        return result
    }
}

/** 线格式层面的错误。全部不可恢复：TCP 流一旦错位，重同步不现实，应直接断开重连。 */
class LinkProtocolException(message: String) : Exception(message)

/**
 * 相机时间戳属于哪个时钟域。决定接收侧能否正确换算，选错会让时钟对齐"自信地错"。
 *
 * [REALTIME] 表示相机时间戳与 `elapsedRealtimeNanos()` 同域（对应 Camera2 的
 * `SENSOR_INFO_TIMESTAMP_SOURCE == REALTIME`），[UNKNOWN] 表示与
 * `System.nanoTime()` 同域。线格式里用 1 字节：0=UNKNOWN，1=REALTIME。
 */
enum class TimestampSource {
    UNKNOWN,
    REALTIME
}

/**
 * 眼镜端在 HELLO 里声明的实测能力。
 *
 * **这是 M1 不必等 M0 的关键**：分辨率、帧率、编码器、TTS 可用性这些
 * M0 要测的东西，在这里变成**运行时协商**而不是编译期常量。M0 的结论
 * 只改变协商的默认值，不改变代码结构。
 */
data class GlassCapabilities(
    val protocolVersion: Int,
    val deviceModel: String,
    /** 相机实际能交付的 (宽, 高, 最大帧率) 组合。 */
    val videoModes: List<VideoMode>,
    val hasHardwareAvcEncoder: Boolean,
    /** 眼镜端本地中文 TTS 是否可用 —— 决定 PRD 决策 D2 走哪条路。 */
    val hasLocalChineseTts: Boolean,
    val hasRotationVector: Boolean,
    val hasSixDof: Boolean,
    val hasTempleTouch: Boolean,
    val hasWearDetection: Boolean,
    /**
     * 眼镜端麦克风（AudioRecord 16kHz mono PCM16）能否真实初始化 —— HELLO 里
     * 运行时真读，不写死（工单 V-01 约束 3）。
     *
     * 注意：这是能力字段里**最后一个上线字段**（[ControlCodec] 把它写在
     * sensorOrientationDegrees 之后），旧解析器读到 sensorOrientationDegrees
     * 即停、忽略尾部字节，保证前向兼容。
     */
    val hasAudioCapture: Boolean = false,

    /**
     * 取自 Camera2 的 `SENSOR_INFO_TIMESTAMP_SOURCE`。接收侧据此决定能否把
     * 相机时间戳当成 `elapsedRealtimeNanos()` 域处理。
     */
    val sensorTimestampSource: TimestampSource = TimestampSource.UNKNOWN,

    /** 取自 `SENSOR_ORIENTATION`，接收侧用它填 `VideoFrame.rotationDegrees`。 */
    val sensorOrientationDegrees: Int = 0
)

data class VideoMode(val width: Int, val height: Int, val maxFps: Int)

/** 手机端在 HELLO_ACK 里下发的会话配置。 */
data class SessionConfig(
    val videoWidth: Int,
    val videoHeight: Int,
    val videoFps: Int,
    val videoBitrateBps: Int,
    val enabledChannels: Set<LinkChannel>,
    val speakPath: SpeakPath,
    val heartbeatIntervalMs: Long,
    val clockSyncIntervalMs: Long
)

/**
 * 播报走哪条路。对应 PRD 决策 D2 及其兜底。
 */
enum class SpeakPath {
    /** D2 首选：手机下发文本，眼镜端本地 TTS 合成播放。 */
    GLASSES_LOCAL_TTS,

    /** D2 兜底一：手机下发 promptId，眼镜播放预置音频。高风险提示走这条以省掉合成耗时。 */
    GLASSES_PRESET_AUDIO,

    /** D2 兜底二：手机端合成并经蓝牙播放。要接受 Wi-Fi/BT 2.4GHz 共存的延迟抖动。 */
    PHONE_TTS_BLUETOOTH
}

/** 链路状态。见 [LinkStateMachine]。 */
enum class LinkState {
    /** 未连接，且没有在尝试。 */
    IDLE,

    /** 正在建立传输连接。 */
    CONNECTING,

    /** 传输已通，正在交换 HELLO / HELLO_ACK。 */
    HANDSHAKING,

    /** 握手完成，正在做时钟对齐；此时媒体可以流动，但时间戳还不可信。 */
    SYNCING,

    /** 全部就绪。 */
    STREAMING,

    /** 掉线，正在退避重连。 */
    RECONNECTING
}

/** 驱动 [LinkStateMachine] 的事件。 */
sealed interface LinkEvent {
    data object StartRequested : LinkEvent
    data object StopRequested : LinkEvent
    data object TransportConnected : LinkEvent
    data class TransportFailed(val reason: String) : LinkEvent
    data class HandshakeCompleted(val capabilities: GlassCapabilities) : LinkEvent
    data class HandshakeRejected(val reason: String) : LinkEvent
    data object ClockConverged : LinkEvent
    data object HeartbeatTimeout : LinkEvent
    data class PeerDisconnected(val reason: String) : LinkEvent
}

/** 状态机要求调用方执行的副作用。状态机自身不做 IO。 */
sealed interface LinkAction {
    /** 在 [delayMs] 之后发起一次传输连接。 */
    data class ScheduleConnect(val delayMs: Long, val attempt: Int) : LinkAction

    data object SendHello : LinkAction
    data object StartClockSync : LinkAction
    data object CloseTransport : LinkAction

    /**
     * 需要主动播报给用户的状态变化（PRD F5-3）。
     *
     * 对盲人用户来说，链路断开必须**可听**，不能只体现在屏幕上或日志里。
     */
    data class AnnounceToUser(val message: String, val critical: Boolean) : LinkAction
}
