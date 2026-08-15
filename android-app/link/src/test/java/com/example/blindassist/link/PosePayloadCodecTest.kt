package com.example.blindassist.link

import com.example.blindassist.link.PosePayloadCodec.PoseSample
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class PosePayloadCodecTest {

    private val single = PoseSample(
        timestampNs = 1_700_000_000_123_456_789L,
        qx = 0.1f, qy = -0.2f, qz = 0.3f, qw = 0.927f,
        accuracy = 3
    )

    @Test
    fun singleSampleRoundTripsBitExactly() {
        val decoded = PosePayloadCodec.decode(PosePayloadCodec.encode(listOf(single)))

        assertEquals(1, decoded.size)
        assertEquals(single, decoded[0])
    }

    @Test
    fun multiSampleBatchRoundTripsInOrder() {
        val samples = (1..100).map { index ->
            PoseSample(
                timestampNs = 1_700_000_000_000_000_000L + index * 10_000_000L,
                qx = index * 0.001f,
                qy = -index * 0.002f,
                qz = 0.5f,
                qw = (index * 0.0001f + 1f).toFloat().let { if (it > 1f) 1f else it },
                accuracy = index % 4
            )
        }

        val decoded = PosePayloadCodec.decode(PosePayloadCodec.encode(samples))

        assertEquals(samples, decoded)
    }

    @Test
    fun encodedLayoutMatchesM105WireFormat() {
        val encoded = PosePayloadCodec.encode(listOf(single))
        val buffer = ByteBuffer.wrap(encoded).order(ByteOrder.BIG_ENDIAN)

        assertEquals("sampleCount 应为 u16 大端", 1, buffer.short.toInt())
        assertEquals("timestampNs 应为 i64 大端", single.timestampNs, buffer.long)
        assertEquals("qx 应为 f32 大端", single.qx.toBits(), buffer.float.toBits())
        assertEquals("qy 应为 f32 大端", single.qy.toBits(), buffer.float.toBits())
        assertEquals("qz 应为 f32 大端", single.qz.toBits(), buffer.float.toBits())
        assertEquals("qw 应为 f32 大端", single.qw.toBits(), buffer.float.toBits())
        assertEquals("accuracy 应为 u8", single.accuracy, buffer.get().toInt())
        assertTrue("不应有多余字节", !buffer.hasRemaining())

        assertEquals(2 + PosePayloadCodec.SAMPLE_BYTES, encoded.size)
    }

    @Test
    fun encodeRejectsEmptyBatch() {
        val error = assertThrows(IllegalArgumentException::class.java) {
            PosePayloadCodec.encode(emptyList())
        }
        assertTrue(error.message.orEmpty().contains("空采样批"))
    }

    @Test
    fun decodeRejectsZeroSampleCount() {
        val payload = ByteBuffer.allocate(2).order(ByteOrder.BIG_ENDIAN)
            .putShort(0).array()
        val error = assertThrows(IllegalArgumentException::class.java) {
            PosePayloadCodec.decode(payload)
        }
        assertTrue(error.message.orEmpty().contains("空采样批"))
    }

    @Test
    fun decodeRejectsPayloadShorterThanTwoBytes() {
        val error = assertThrows(IllegalArgumentException::class.java) {
            PosePayloadCodec.decode(ByteArray(1) { 1 })
        }
        assertTrue(error.message.orEmpty().contains("过短"))
    }

    @Test
    fun decodeRejectsTruncatedSample() {
        val full = PosePayloadCodec.encode(listOf(single, single))
        for (cut in 2 until full.size) {
            val truncated = full.copyOf(cut)
            val error = assertThrows(
                "在第 $cut 字节截断必须报错",
                IllegalArgumentException::class.java
            ) {
                PosePayloadCodec.decode(truncated)
            }
            assertTrue(error.message.orEmpty().contains("长度与 sampleCount"))
        }
    }

    @Test
    fun decodeRejectsTrailingBytes() {
        val full = PosePayloadCodec.encode(listOf(single))
        val padded = full + ByteArray(7) { 0 }
        val error = assertThrows(IllegalArgumentException::class.java) {
            PosePayloadCodec.decode(padded)
        }
        assertTrue(error.message.orEmpty().contains("长度与 sampleCount"))
    }

    @Test
    fun decodeRejectsSampleCountBeyondCodecCap() {
        val payload = ByteBuffer.allocate(2 + PosePayloadCodec.SAMPLE_BYTES)
            .order(ByteOrder.BIG_ENDIAN)
            .putShort((PosePayloadCodec.MAX_SAMPLES + 1).toShort())
            .putLong(0L)
            .putFloat(0f).putFloat(0f).putFloat(0f).putFloat(1f)
            .put(3.toByte())
            .array()
        val error = assertThrows(IllegalArgumentException::class.java) {
            PosePayloadCodec.decode(payload)
        }
        assertTrue(error.message.orEmpty().contains("超过上限"))
    }

    @Test
    fun maxSizedBatchRoundTripsAndStaysUnderWireLimit() {
        val samples = List(PosePayloadCodec.MAX_SAMPLES) { index ->
            PoseSample(
                timestampNs = 1_700_000_000_000_000_000L + index,
                qx = 0f, qy = 0f, qz = 0f, qw = 1f,
                accuracy = 3
            )
        }
        val encoded = PosePayloadCodec.encode(samples)

        assertTrue(
            "上限批不应超过链路 2 MiB 负载上限",
            encoded.size <= LinkWire.DEFAULT_MAX_PAYLOAD
        )
        assertEquals(samples, PosePayloadCodec.decode(encoded))
    }

    @Test
    fun encodeRejectsOverCapBatch() {
        val tooMany = List(PosePayloadCodec.MAX_SAMPLES + 1) { index ->
            PoseSample(index.toLong(), 0f, 0f, 0f, 1f, 3)
        }
        val error = assertThrows(IllegalArgumentException::class.java) {
            PosePayloadCodec.encode(tooMany)
        }
        assertTrue(error.message.orEmpty().contains("超过上限"))
    }

    @Test
    fun batchBoundaryKeepsSampleOrderDeterministic() {
        val samples = (0 until 37).map { index ->
            PoseSample(1_000L + index, index.toFloat(), -index.toFloat(), 0f, 1f, 1)
        }
        val decoded = PosePayloadCodec.decode(PosePayloadCodec.encode(samples))
        assertArrayEquals(
            samples.map { it.timestampNs }.toLongArray(),
            decoded.map { it.timestampNs }.toLongArray()
        )
        assertEquals(samples.map { it.qx }, decoded.map { it.qx })
    }
}
