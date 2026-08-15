package com.example.blindassist.link

import java.io.ByteArrayOutputStream

/**
 * CONTROL 通道的报文模型。
 *
 * 时间戳语义（与 [ClockSyncEstimator] 的口径一致，见工单 3.2 的图）：
 * 所有时间戳都是**发送方自己的单调时钟**，不做跨设备换算；换算由接收侧的
 * [ClockSyncEstimator] 完成。PING 的 [Ping.t1] 是发起方发送时刻；PONG 回显
 * [Pong.t1] 让发起方能把 PONG 和它对应的 PING 配上对，同时携带应答方
 * 收到时刻 [Pong.t2] 与发出时刻 [Pong.t3]。发起方收到 PONG 的本地时刻
 * `t4` 不在报文里，由接收方自己打点。
 */
sealed interface ControlMessage {
    /** 眼镜→手机：声明能力。体 = [GlassCapabilities]。 */
    data class Hello(val capabilities: GlassCapabilities) : ControlMessage

    /** 手机→眼镜：接受握手并下发会话配置。体 = [SessionConfig]。 */
    data class HelloAck(val config: SessionConfig) : ControlMessage

    /** 手机→眼镜：拒绝握手。体 = 原因字符串。 */
    data class HelloReject(val reason: String) : ControlMessage

    /** 手机→眼镜：时钟对齐请求。体 = int64 t1（发起方发送时刻）。 */
    data class Ping(val t1: Long) : ControlMessage

    /**
     * 眼镜→手机：时钟对齐应答。体 = int64 t1（回显）+ int64 t2（应答方收到）
     * + int64 t3（应答方发出）。
     */
    data class Pong(val t1: Long, val t2: Long, val t3: Long) : ControlMessage

    /** 双向：带原因的正常关闭。体 = 原因字符串。 */
    data class Bye(val reason: String) : ControlMessage
}

/** CONTROL 报文类型码。 */
enum class ControlMessageType(val code: Int) {
    HELLO(0x01),
    HELLO_ACK(0x02),
    HELLO_REJECT(0x03),
    PING(0x04),
    PONG(0x05),
    BYE(0x06)
}

/**
 * CONTROL 通道报文编解码。手写大端二进制，零第三方依赖，与 [LinkProtocol] 同风格。
 *
 * 线格式：CONTROL 通道的负载 = 1 字节报文类型 + 报文体。具体布局：
 * - 固定整数：uint32（4 字节，大端）；时间戳一律 int64（8 字节，大端）；
 * - 字符串：uint16 字节长度 + UTF-8 字节；
 * - [VideoMode] 列表：uint16 个数 + 每项 (uint32 width, uint32 height, uint32 maxFps)；
 * - [LinkChannel] 集合：uint16 个数 + 每项 1 字节通道码（[LinkChannel.code]）；
 * - 布尔：每项 1 字节，0x00=false、0x01=true（为可读性不用位域）；
 * - [TimestampSource]：1 字节，0=UNKNOWN、1=REALTIME；
 * - [SpeakPath]：1 字节，按枚举 ordinal（0..2）。
 *
 * 失败语义与线格式约定一致（见 [LinkProtocol]）：任何长度不足、非法类型码、
 * 非法枚举/布尔值一律抛 [LinkProtocolException]，调用方断开重连，不尝试在流里
 * 重新同步。**读任何字段之前先检查剩余长度**，绝不用 0 或默认值填补截断的字段
 * —— 否则残缺的 [GlassCapabilities] 会被当成真实能力去算 [SessionConfig]。
 *
 * 前向兼容：解码只消费已知字段，尾部多余字节一律忽略；以后往报文里加字段时，
 * 旧版本对端仍能解出已知部分。
 */
object ControlCodec {

    /** 把报文编码成 CONTROL 通道负载字节（不含 20 字节帧头）。 */
    fun encode(message: ControlMessage): ByteArray {
        val out = ByteArrayOutputStream()
        when (message) {
            is ControlMessage.Hello -> {
                out.write(ControlMessageType.HELLO.code)
                writeCapabilities(out, message.capabilities)
            }
            is ControlMessage.HelloAck -> {
                out.write(ControlMessageType.HELLO_ACK.code)
                writeSessionConfig(out, message.config)
            }
            is ControlMessage.HelloReject -> {
                out.write(ControlMessageType.HELLO_REJECT.code)
                writeString(out, message.reason, "reason")
            }
            is ControlMessage.Ping -> {
                out.write(ControlMessageType.PING.code)
                writeInt64(out, message.t1)
            }
            is ControlMessage.Pong -> {
                out.write(ControlMessageType.PONG.code)
                writeInt64(out, message.t1)
                writeInt64(out, message.t2)
                writeInt64(out, message.t3)
            }
            is ControlMessage.Bye -> {
                out.write(ControlMessageType.BYE.code)
                writeString(out, message.reason, "reason")
            }
        }
        return out.toByteArray()
    }

    /** 把 CONTROL 通道负载字节解码成报文。尾部多余字节忽略。 */
    fun decode(payload: ByteArray): ControlMessage {
        val reader = Reader(payload)
        return when (val typeCode = reader.readUInt8("报文类型")) {
            ControlMessageType.HELLO.code -> ControlMessage.Hello(readCapabilities(reader))
            ControlMessageType.HELLO_ACK.code -> ControlMessage.HelloAck(readSessionConfig(reader))
            ControlMessageType.HELLO_REJECT.code -> ControlMessage.HelloReject(reader.readString("reason"))
            ControlMessageType.PING.code -> ControlMessage.Ping(reader.readInt64("t1"))
            ControlMessageType.PONG.code -> ControlMessage.Pong(
                t1 = reader.readInt64("t1"),
                t2 = reader.readInt64("t2"),
                t3 = reader.readInt64("t3")
            )
            ControlMessageType.BYE.code -> ControlMessage.Bye(reader.readString("reason"))
            else -> throw LinkProtocolException("未知 CONTROL 报文类型 0x%02x".format(typeCode))
        }
    }

    // ---------- 编码 ----------

    private fun writeCapabilities(out: ByteArrayOutputStream, caps: GlassCapabilities) {
        writeUInt32(out, caps.protocolVersion.toLong(), "protocolVersion")
        writeString(out, caps.deviceModel, "deviceModel")
        if (caps.videoModes.size > 0xFFFF) {
            throw LinkProtocolException("videoModes 个数 ${caps.videoModes.size} 超过 uint16 上限")
        }
        writeUInt16(out, caps.videoModes.size, "videoModes 个数")
        for (mode in caps.videoModes) {
            writeUInt32(out, mode.width.toLong(), "videoMode.width")
            writeUInt32(out, mode.height.toLong(), "videoMode.height")
            writeUInt32(out, mode.maxFps.toLong(), "videoMode.maxFps")
        }
        writeBool(out, caps.hasHardwareAvcEncoder)
        writeBool(out, caps.hasLocalChineseTts)
        writeBool(out, caps.hasRotationVector)
        writeBool(out, caps.hasSixDof)
        writeBool(out, caps.hasTempleTouch)
        writeBool(out, caps.hasWearDetection)
        out.write(
            when (caps.sensorTimestampSource) {
                TimestampSource.UNKNOWN -> 0
                TimestampSource.REALTIME -> 1
            }
        )
        writeUInt32(out, caps.sensorOrientationDegrees.toLong(), "sensorOrientationDegrees")
        // 工单 V-01：hasAudioCapture 追加在能力字段最末尾。旧解析器（含
        // scripts/m1_mock_phone.py 的 decode_hello）读到这里即停、忽略尾部字节。
        writeBool(out, caps.hasAudioCapture)
    }

    private fun writeSessionConfig(out: ByteArrayOutputStream, config: SessionConfig) {
        writeUInt32(out, config.videoWidth.toLong(), "videoWidth")
        writeUInt32(out, config.videoHeight.toLong(), "videoHeight")
        writeUInt32(out, config.videoFps.toLong(), "videoFps")
        writeUInt32(out, config.videoBitrateBps.toLong(), "videoBitrateBps")
        if (config.enabledChannels.size > 0xFFFF) {
            throw LinkProtocolException("enabledChannels 个数 ${config.enabledChannels.size} 超过 uint16 上限")
        }
        writeUInt16(out, config.enabledChannels.size, "enabledChannels 个数")
        for (channel in config.enabledChannels) {
            out.write(channel.code)
        }
        out.write(config.speakPath.ordinal)
        writeUInt32(out, config.heartbeatIntervalMs, "heartbeatIntervalMs")
        writeUInt32(out, config.clockSyncIntervalMs, "clockSyncIntervalMs")
    }

    private fun writeString(out: ByteArrayOutputStream, value: String, what: String) {
        val bytes = value.toByteArray(Charsets.UTF_8)
        if (bytes.size > 0xFFFF) {
            throw LinkProtocolException("$what 的 UTF-8 字节数 ${bytes.size} 超过 uint16 上限")
        }
        writeUInt16(out, bytes.size, "$what 长度")
        out.write(bytes)
    }

    private fun writeUInt16(out: ByteArrayOutputStream, value: Int, what: String) {
        if (value < 0 || value > 0xFFFF) {
            throw LinkProtocolException("$what 超出 uint16 范围: $value")
        }
        out.write((value ushr 8) and 0xFF)
        out.write(value and 0xFF)
    }

    private fun writeUInt32(out: ByteArrayOutputStream, value: Long, what: String) {
        if (value < 0 || value > 0xFFFF_FFFFL) {
            throw LinkProtocolException("$what 超出 uint32 范围: $value")
        }
        out.write(((value ushr 24) and 0xFF).toInt())
        out.write(((value ushr 16) and 0xFF).toInt())
        out.write(((value ushr 8) and 0xFF).toInt())
        out.write((value and 0xFF).toInt())
    }

    private fun writeInt64(out: ByteArrayOutputStream, value: Long) {
        for (i in 7 downTo 0) {
            out.write(((value ushr (8 * i)) and 0xFF).toInt())
        }
    }

    private fun writeBool(out: ByteArrayOutputStream, value: Boolean) {
        out.write(if (value) 1 else 0)
    }

    // ---------- 解码 ----------

    /**
     * 游标式读取器。每个读取函数**先检查剩余长度**，不足即抛
     * [LinkProtocolException]，绝不读越界、绝不用默认值填补。
     */
    private class Reader(private val bytes: ByteArray) {
        private var pos = 0

        val remaining: Int get() = bytes.size - pos

        private fun require(n: Int, what: String) {
            if (remaining < n) {
                throw LinkProtocolException(
                    "CONTROL 报文被截断：字段 $what 需要 $n 字节，剩余 $remaining"
                )
            }
        }

        fun readUInt8(what: String): Int {
            require(1, what)
            return bytes[pos++].toInt() and 0xFF
        }

        fun readUInt16(what: String): Int {
            require(2, what)
            val value = ((bytes[pos].toInt() and 0xFF) shl 8) or
                (bytes[pos + 1].toInt() and 0xFF)
            pos += 2
            return value
        }

        fun readUInt32(what: String): Long {
            require(4, what)
            val value = ((bytes[pos].toLong() and 0xFF) shl 24) or
                ((bytes[pos + 1].toLong() and 0xFF) shl 16) or
                ((bytes[pos + 2].toLong() and 0xFF) shl 8) or
                (bytes[pos + 3].toLong() and 0xFF)
            pos += 4
            return value
        }

        fun readInt64(what: String): Long {
            require(8, what)
            var value = 0L
            for (i in 0 until 8) {
                value = (value shl 8) or (bytes[pos + i].toLong() and 0xFF)
            }
            pos += 8
            return value
        }

        fun readBool(what: String): Boolean {
            val value = readUInt8(what)
            if (value != 0 && value != 1) {
                throw LinkProtocolException("非法布尔值 $value（字段 $what）")
            }
            return value == 1
        }

        fun readString(what: String): String {
            val length = readUInt16("$what 长度")
            require(length, what)
            val value = String(bytes, pos, length, Charsets.UTF_8)
            pos += length
            return value
        }

        fun readChannels(what: String): Set<LinkChannel> {
            val count = readUInt16("$what 个数")
            val result = LinkedHashSet<LinkChannel>(count)
            repeat(count) {
                val code = readUInt8("$what 通道码")
                val channel = LinkChannel.fromCode(code)
                    ?: throw LinkProtocolException("未知通道码 0x%02x".format(code))
                result += channel
            }
            return result
        }
    }

    private fun readCapabilities(reader: Reader): GlassCapabilities {
        val protocolVersion = reader.readUInt32("protocolVersion").toInt()
        val deviceModel = reader.readString("deviceModel")
        val modeCount = reader.readUInt16("videoModes 个数")
        val modes = ArrayList<VideoMode>(modeCount)
        repeat(modeCount) {
            modes += VideoMode(
                width = reader.readUInt32("videoMode.width").toInt(),
                height = reader.readUInt32("videoMode.height").toInt(),
                maxFps = reader.readUInt32("videoMode.maxFps").toInt()
            )
        }
        val hasHardwareAvcEncoder = reader.readBool("hasHardwareAvcEncoder")
        val hasLocalChineseTts = reader.readBool("hasLocalChineseTts")
        val hasRotationVector = reader.readBool("hasRotationVector")
        val hasSixDof = reader.readBool("hasSixDof")
        val hasTempleTouch = reader.readBool("hasTempleTouch")
        val hasWearDetection = reader.readBool("hasWearDetection")
        val sensorTimestampSource = when (val value = reader.readUInt8("sensorTimestampSource")) {
            0 -> TimestampSource.UNKNOWN
            1 -> TimestampSource.REALTIME
            else -> throw LinkProtocolException("非法 sensorTimestampSource 值 $value")
        }
        val sensorOrientationDegrees = reader.readUInt32("sensorOrientationDegrees").toInt()
        val hasAudioCapture = reader.readBool("hasAudioCapture")
        return GlassCapabilities(
            protocolVersion = protocolVersion,
            deviceModel = deviceModel,
            videoModes = modes,
            hasHardwareAvcEncoder = hasHardwareAvcEncoder,
            hasLocalChineseTts = hasLocalChineseTts,
            hasRotationVector = hasRotationVector,
            hasSixDof = hasSixDof,
            hasTempleTouch = hasTempleTouch,
            hasWearDetection = hasWearDetection,
            sensorTimestampSource = sensorTimestampSource,
            sensorOrientationDegrees = sensorOrientationDegrees,
            hasAudioCapture = hasAudioCapture
        )
    }

    private fun readSessionConfig(reader: Reader): SessionConfig {
        val videoWidth = reader.readUInt32("videoWidth").toInt()
        val videoHeight = reader.readUInt32("videoHeight").toInt()
        val videoFps = reader.readUInt32("videoFps").toInt()
        val videoBitrateBps = reader.readUInt32("videoBitrateBps").toInt()
        val enabledChannels = reader.readChannels("enabledChannels")
        val speakPath = when (val value = reader.readUInt8("speakPath")) {
            SpeakPath.GLASSES_LOCAL_TTS.ordinal -> SpeakPath.GLASSES_LOCAL_TTS
            SpeakPath.GLASSES_PRESET_AUDIO.ordinal -> SpeakPath.GLASSES_PRESET_AUDIO
            SpeakPath.PHONE_TTS_BLUETOOTH.ordinal -> SpeakPath.PHONE_TTS_BLUETOOTH
            else -> throw LinkProtocolException("非法 speakPath 值 $value")
        }
        val heartbeatIntervalMs = reader.readUInt32("heartbeatIntervalMs")
        val clockSyncIntervalMs = reader.readUInt32("clockSyncIntervalMs")
        return SessionConfig(
            videoWidth = videoWidth,
            videoHeight = videoHeight,
            videoFps = videoFps,
            videoBitrateBps = videoBitrateBps,
            enabledChannels = enabledChannels,
            speakPath = speakPath,
            heartbeatIntervalMs = heartbeatIntervalMs,
            clockSyncIntervalMs = clockSyncIntervalMs
        )
    }
}
