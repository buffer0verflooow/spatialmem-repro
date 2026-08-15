package com.example.blindassist.link

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioPayloadCodecTest {

    /** 20ms 满包：640 字节 PCM16（320 帧），值取每个字节可复现的伪随机。 */
    private fun pcm16Bytes(count: Int): ByteArray = ByteArray(count) { index ->
        ((index * 31 + 7) and 0xFF).toByte()
    }

    // ---------- 1. 往返一致 ----------

    @Test
    fun fullPacketRoundTripsBitExactly() {
        val bytes = pcm16Bytes(AudioPayloadCodec.PACKET_BYTES)
        val decoded = AudioPayloadCodec.decode(
            AudioPayloadCodec.encode(bytes, AudioPayloadCodec.FRAMES_PER_PACKET)
        )
        assertArrayEquals("640 字节满包必须逐字节一致", bytes, decoded)
    }

    @Test
    fun shortLastPacketRoundTrips() {
        // 最后一包允许不足 640 字节（工单 M1-05 §3.3）。
        val frames = 50
        val bytes = pcm16Bytes(frames * AudioPayloadCodec.BYTES_PER_SAMPLE)
        val decoded = AudioPayloadCodec.decode(AudioPayloadCodec.encode(bytes, frames))
        assertArrayEquals("不足 20ms 的尾包必须原样往返", bytes, decoded)
    }

    @Test
    fun arbitraryEvenLengthsRoundTripWithinCap() {
        for (frames in intArrayOf(1, 2, 159, 319, AudioPayloadCodec.FRAMES_PER_PACKET)) {
            val bytes = pcm16Bytes(frames * AudioPayloadCodec.BYTES_PER_SAMPLE)
            assertArrayEquals(
                "$frames 帧往返不一致",
                bytes,
                AudioPayloadCodec.decode(AudioPayloadCodec.encode(bytes, frames))
            )
        }
    }

    // ---------- 2. 空包拒绝（0 字节边界） ----------

    @Test
    fun encodeRejectsEmptyPacket() {
        val error = assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.encode(ByteArray(0), 0)
        }
        assertTrue(error.message.orEmpty().contains("空音频包"))
    }

    @Test
    fun decodeRejectsEmptyPayload() {
        val error = assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.decode(ByteArray(0))
        }
        assertTrue(error.message.orEmpty().contains("空音频包"))
    }

    // ---------- 3. 长度截断报错 ----------

    @Test
    fun encodeRejectsByteCountMismatchingSampleCount() {
        // 声称 320 帧（640 字节）但只给了 638 字节：半帧截断必须报「长度与 sampleCount 不符」。
        val error = assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.encode(pcm16Bytes(638), AudioPayloadCodec.FRAMES_PER_PACKET)
        }
        assertTrue(error.message.orEmpty().contains("长度与 sampleCount"))
    }

    @Test
    fun encodeRejectsZeroSampleCountWithNonEmptyBytes() {
        val error = assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.encode(pcm16Bytes(640), 0)
        }
        assertTrue(error.message.orEmpty().contains("sampleCount"))
    }

    // ---------- 4. 超长拒绝（641 字节边界） ----------

    @Test
    fun encodeRejectsSampleCountBeyondCap() {
        // 321 帧 > 320 帧上限：无论字节多少都必须拒绝。
        val error = assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.encode(pcm16Bytes(642), AudioPayloadCodec.FRAMES_PER_PACKET + 1)
        }
        assertTrue(error.message.orEmpty().contains("超过上限"))
    }

    @Test
    fun decodeRejectsOversizedPayload() {
        // 641 字节超出 640 字节常规包上限。
        val error = assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.decode(ByteArray(AudioPayloadCodec.PACKET_BYTES + 1))
        }
        assertTrue(error.message.orEmpty().contains("超过上限"))
    }

    // ---------- 5. 边界 0 / 640 / 641 字节 ----------

    @Test
    fun boundaryZeroBytesRejectedByBothEnds() {
        assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.encode(ByteArray(0), 0)
        }
        assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.decode(ByteArray(0))
        }
    }

    @Test
    fun boundaryFullPacketAccepted() {
        val bytes = pcm16Bytes(AudioPayloadCodec.PACKET_BYTES)
        val encoded = AudioPayloadCodec.encode(bytes, AudioPayloadCodec.FRAMES_PER_PACKET)
        assertEquals(AudioPayloadCodec.PACKET_BYTES, encoded.size)
        assertEquals(AudioPayloadCodec.PACKET_BYTES, AudioPayloadCodec.decode(encoded).size)
    }

    @Test
    fun boundaryOverSizedRejected() {
        assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.decode(ByteArray(AudioPayloadCodec.PACKET_BYTES + 1))
        }
        // encode 侧 641 字节不可能对应合法 sampleCount（641 不是偶数），同样必须拒绝。
        assertThrows(IllegalArgumentException::class.java) {
            AudioPayloadCodec.encode(
                ByteArray(AudioPayloadCodec.PACKET_BYTES + 1),
                AudioPayloadCodec.FRAMES_PER_PACKET
            )
        }
    }

    // ---------- 6. 常量自洽 ----------

    @Test
    fun wireConstantsAreSelfConsistent() {
        assertEquals(
            "PACKET_BYTES 必须等于 FRAMES_PER_PACKET × BYTES_PER_SAMPLE",
            AudioPayloadCodec.FRAMES_PER_PACKET * AudioPayloadCodec.BYTES_PER_SAMPLE,
            AudioPayloadCodec.PACKET_BYTES
        )
        assertEquals(
            "FRAME_DURATION_NS 必须等于 1e9 / SAMPLE_RATE_HZ",
            62_500L,
            AudioPayloadCodec.FRAME_DURATION_NS
        )
        assertTrue(
            "满包应远小于链路 2 MiB 负载上限",
            AudioPayloadCodec.PACKET_BYTES <= LinkWire.DEFAULT_MAX_PAYLOAD
        )
    }
}
