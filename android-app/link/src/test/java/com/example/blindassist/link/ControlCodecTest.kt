package com.example.blindassist.link

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class ControlCodecTest {

    private fun capabilities() = GlassCapabilities(
        protocolVersion = LinkWire.VERSION,
        deviceModel = "X3Pro",
        videoModes = listOf(VideoMode(640, 360, 30), VideoMode(1280, 720, 30)),
        hasHardwareAvcEncoder = true,
        hasLocalChineseTts = true,
        hasRotationVector = true,
        hasSixDof = false,
        hasTempleTouch = true,
        hasWearDetection = false,
        sensorTimestampSource = TimestampSource.REALTIME,
        sensorOrientationDegrees = 270,
        hasAudioCapture = true
    )

    private fun sessionConfig() = SessionConfig(
        videoWidth = 640,
        videoHeight = 360,
        videoFps = 30,
        videoBitrateBps = 1_000_000,
        enabledChannels = setOf(LinkChannel.VIDEO, LinkChannel.CONTROL, LinkChannel.SPEAK),
        speakPath = SpeakPath.GLASSES_LOCAL_TTS,
        heartbeatIntervalMs = 5_000,
        clockSyncIntervalMs = 30_000
    )

    private fun allSixMessages(): List<ControlMessage> = listOf(
        ControlMessage.Hello(capabilities()),
        ControlMessage.HelloAck(sessionConfig()),
        ControlMessage.HelloReject("协议版本不兼容"),
        ControlMessage.Ping(123_456_789L),
        ControlMessage.Pong(123_456_789L, 234_567_891L, 345_678_912L),
        ControlMessage.Bye("主动关闭")
    )

    private fun roundTrip(message: ControlMessage): ControlMessage =
        ControlCodec.decode(ControlCodec.encode(message))

    // ---------- 1. 六种报文各自 round-trip 等值 ----------

    @Test
    fun helloRoundTripsToEqualValue() {
        val message = ControlMessage.Hello(capabilities())
        assertEquals(message, roundTrip(message))
    }

    @Test
    fun helloHasAudioCaptureRoundTripsBothValues() {
        for (hasAudioCapture in booleanArrayOf(true, false)) {
            val message = ControlMessage.Hello(capabilities().copy(hasAudioCapture = hasAudioCapture))
            val decoded = roundTrip(message) as ControlMessage.Hello
            assertEquals("hasAudioCapture=$hasAudioCapture 必须往返一致", hasAudioCapture, decoded.capabilities.hasAudioCapture)
        }
    }

    /**
     * 工单 V-01 兼容回归门：hasAudioCapture 是能力字段最末尾的 1 字节，
     * 旧解析器（如 scripts/m1_mock_phone.py 的 decode_hello）读到
     * sensorOrientationDegrees 即停，尾部新字段必须被忽略、不影响旧字段解析。
     * 这里用 ByteBuffer 逐字段复刻旧解析器行为，而不是用新 [ControlCodec] 自证。
     */
    @Test
    fun oldStyleParserIgnoresTrailingHasAudioCaptureByte() {
        val caps = capabilities()
        val encoded = ControlCodec.encode(ControlMessage.Hello(caps))
        val body = ByteBuffer.wrap(encoded).order(ByteOrder.BIG_ENDIAN)

        // 以下解析顺序与 m1_mock_phone.py decode_hello 完全一致。
        assertEquals(ControlMessageType.HELLO.code.toByte(), body.get())
        assertEquals(caps.protocolVersion, body.int)
        val modelBytes = ByteArray(body.short.toInt() and 0xFFFF)
        body.get(modelBytes)
        assertEquals(caps.deviceModel, String(modelBytes, Charsets.UTF_8))
        val modeCount = body.short.toInt() and 0xFFFF
        assertEquals(caps.videoModes.size, modeCount)
        for (mode in caps.videoModes) {
            assertEquals(mode.width, body.int)
            assertEquals(mode.height, body.int)
            assertEquals(mode.maxFps, body.int)
        }
        val booleans = booleanArrayOf(
            caps.hasHardwareAvcEncoder,
            caps.hasLocalChineseTts,
            caps.hasRotationVector,
            caps.hasSixDof,
            caps.hasTempleTouch,
            caps.hasWearDetection
        )
        for (expected in booleans) {
            assertEquals(if (expected) 1 else 0, body.get().toInt())
        }
        assertEquals(1, body.get().toInt()) // REALTIME
        assertEquals(caps.sensorOrientationDegrees, body.int)

        // 旧解析器到这里停止；hasAudioCapture 必须是唯一剩余的尾部字节。
        assertEquals("hasAudioCapture 必须位于能力字段最末尾", 1, body.remaining())
    }

    @Test
    fun helloAckRoundTripsToEqualValue() {
        val message = ControlMessage.HelloAck(sessionConfig())
        assertEquals(message, roundTrip(message))
    }

    @Test
    fun helloRejectRoundTripsToEqualValue() {
        val message = ControlMessage.HelloReject("能力不足")
        assertEquals(message, roundTrip(message))
    }

    @Test
    fun pingRoundTripsToEqualValue() {
        val message = ControlMessage.Ping(123_456_789L)
        assertEquals(message, roundTrip(message))
    }

    @Test
    fun pongRoundTripsToEqualValue() {
        val message = ControlMessage.Pong(123_456_789L, 234_567_891L, 345_678_912L)
        assertEquals(message, roundTrip(message))
    }

    @Test
    fun byeRoundTripsToEqualValue() {
        val message = ControlMessage.Bye("正常关闭")
        assertEquals(message, roundTrip(message))
    }

    // ---------- 2. 每种报文在每个字节位置截断都必须抛 LinkProtocolException ----------

    @Test
    fun truncatingHelloAtEveryBytePositionThrows() {
        assertEveryTruncationThrows(ControlCodec.encode(ControlMessage.Hello(capabilities())))
    }

    @Test
    fun truncatingHelloAckAtEveryBytePositionThrows() {
        assertEveryTruncationThrows(ControlCodec.encode(ControlMessage.HelloAck(sessionConfig())))
    }

    @Test
    fun truncatingHelloRejectAtEveryBytePositionThrows() {
        assertEveryTruncationThrows(ControlCodec.encode(ControlMessage.HelloReject("能力不足")))
    }

    @Test
    fun truncatingPingAtEveryBytePositionThrows() {
        assertEveryTruncationThrows(ControlCodec.encode(ControlMessage.Ping(123_456_789L)))
    }

    @Test
    fun truncatingPongAtEveryBytePositionThrows() {
        assertEveryTruncationThrows(
            ControlCodec.encode(ControlMessage.Pong(123_456_789L, 234_567_891L, 345_678_912L))
        )
    }

    @Test
    fun truncatingByeAtEveryBytePositionThrows() {
        assertEveryTruncationThrows(ControlCodec.encode(ControlMessage.Bye("正常关闭")))
    }

    /**
     * 对编码结果的每一个可能切点（0 到 size-1，含空负载）截断后解码：
     * 必须抛 [LinkProtocolException] —— 不许返回一个值、不许返回 null、
     * 不许抛别的异常类型。这是「绝不容忍更短」硬约束的直接验证。
     */
    private fun assertEveryTruncationThrows(encoded: ByteArray) {
        for (cut in 0 until encoded.size) {
            assertThrows(
                "截断到 $cut 字节时必须抛 LinkProtocolException（共 ${encoded.size} 字节）",
                LinkProtocolException::class.java
            ) {
                ControlCodec.decode(encoded.copyOf(cut))
            }
        }
    }

    // ---------- 3. 尾部多余字节忽略（前向兼容） ----------

    @Test
    fun trailingUnknownBytesAreIgnoredAndKnownFieldsStillDecode() {
        for (message in allSixMessages()) {
            val extended = ControlCodec.encode(message) + byteArrayOf(0x11, 0x22, 0x33, 0x44)
            assertEquals(message, ControlCodec.decode(extended))
        }
    }

    // ---------- 4. 未知报文类型码抛异常 ----------

    @Test
    fun unknownMessageTypeCodeThrows() {
        for (code in intArrayOf(0x00, 0x07, 0x7F, 0xFF)) {
            assertThrows(LinkProtocolException::class.java) {
                ControlCodec.decode(byteArrayOf(code.toByte(), 0x01, 0x02, 0x03))
            }
        }
    }

    // ---------- 5. 边界值：空列表 / 空串 / 中文 UTF-8 ----------

    @Test
    fun emptyVideoModesEmptyDeviceModelAndChineseDeviceModelRoundTrip() {
        val base = capabilities().copy(
            deviceModel = "",
            videoModes = emptyList(),
            sensorTimestampSource = TimestampSource.UNKNOWN,
            sensorOrientationDegrees = 0
        )
        val chinese = base.copy(deviceModel = "星目 X3 Pro 中文版")

        assertEquals(ControlMessage.Hello(base), roundTrip(ControlMessage.Hello(base)))
        assertEquals(ControlMessage.Hello(chinese), roundTrip(ControlMessage.Hello(chinese)))
    }

    // ---------- 6. PING/PONG 时间戳字段顺序 ----------

    @Test
    fun pingPongTimestampFieldOrderIsPreservedWithDistinctValues() {
        val t1 = 123_456_789L
        val t2 = 234_567_891L
        val t3 = 345_678_912L

        val decodedPing = roundTrip(ControlMessage.Ping(t1)) as ControlMessage.Ping
        assertEquals(t1, decodedPing.t1)

        val decodedPong = roundTrip(ControlMessage.Pong(t1, t2, t3)) as ControlMessage.Pong
        assertEquals(t1, decodedPong.t1)
        assertEquals(t2, decodedPong.t2)
        assertEquals(t3, decodedPong.t3)
    }
}
