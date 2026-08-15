package com.example.blindassist.link

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class LinkProtocolTest {

    @Test
    fun encodeThenDecodeRoundTripsEveryField() {
        val packet = LinkPacket(
            channel = LinkChannel.VIDEO,
            flags = LinkFlags.KEYFRAME,
            sequence = 123_456L,
            senderTimestampNs = 9_876_543_210_123L,
            payload = ByteArray(300) { (it * 7).toByte() }
        )

        val reader = LinkFrameReader()
        reader.append(LinkProtocol.encode(packet))

        assertEquals(packet, reader.next())
        assertNull("只写了一个包，不应解出第二个", reader.next())
    }

    @Test
    fun headerIsExactlyTwentyBytesAndPayloadFollowsIt() {
        val payload = byteArrayOf(1, 2, 3, 4, 5)
        val encoded = LinkProtocol.encode(
            LinkPacket(LinkChannel.IMU, LinkFlags.NONE, 1L, 1L, payload)
        )

        assertEquals(LinkWire.HEADER_SIZE + payload.size, encoded.size)
        assertEquals(LinkWire.MAGIC_0, encoded[0])
        assertEquals(LinkWire.MAGIC_1, encoded[1])
        assertEquals(LinkWire.VERSION, encoded[2].toInt())
        assertArrayEquals(payload, encoded.copyOfRange(LinkWire.HEADER_SIZE, encoded.size))
    }

    /**
     * TCP 会在任意位置切断。一个包被逐字节喂进来也必须能正确还原 ——
     * 这是自研链路最常见的 bug 来源。
     */
    @Test
    fun packetSplitAcrossEveryPossibleByteBoundaryStillReassembles() {
        val packet = LinkPacket(
            channel = LinkChannel.VIDEO,
            flags = LinkFlags.KEYFRAME,
            sequence = 42L,
            senderTimestampNs = 1_000_000_000L,
            payload = ByteArray(97) { it.toByte() }
        )
        val encoded = LinkProtocol.encode(packet)

        for (splitAt in 1 until encoded.size) {
            val reader = LinkFrameReader()
            reader.append(encoded, 0, splitAt)
            assertNull("在第 $splitAt 字节处切断时不该解出完整包", reader.next())
            reader.append(encoded, splitAt, encoded.size - splitAt)
            assertEquals("在第 $splitAt 字节处切断后重组失败", packet, reader.next())
        }
    }

    @Test
    fun oneByteAtATimeReassembles() {
        val packet = LinkPacket(LinkChannel.POSE, LinkFlags.NONE, 7L, 77L, ByteArray(40) { it.toByte() })
        val encoded = LinkProtocol.encode(packet)
        val reader = LinkFrameReader()

        for (index in encoded.indices) {
            assertNull(reader.next())
            reader.append(encoded, index, 1)
        }
        assertEquals(packet, reader.next())
    }

    @Test
    fun multiplePacketsInOneReadAreAllDrained() {
        val packets = listOf(
            LinkPacket(LinkChannel.CONTROL, LinkFlags.NONE, 1L, 100L, byteArrayOf(1)),
            LinkPacket(LinkChannel.VIDEO, LinkFlags.KEYFRAME, 2L, 200L, ByteArray(500) { 3 }),
            LinkPacket(LinkChannel.AUDIO, LinkFlags.NONE, 3L, 300L, ByteArray(160) { 5 })
        )
        val combined = packets.flatMap { LinkProtocol.encode(it).toList() }.toByteArray()

        val reader = LinkFrameReader()
        reader.append(combined)

        for (expected in packets) {
            assertEquals(expected, reader.next())
        }
        assertNull(reader.next())
        assertEquals(0, reader.pendingBytes())
    }

    @Test
    fun emptyPayloadIsValid() {
        val packet = LinkPacket(LinkChannel.CONTROL, LinkFlags.NONE, 0L, 0L, ByteArray(0))
        val reader = LinkFrameReader()
        reader.append(LinkProtocol.encode(packet))
        assertEquals(packet, reader.next())
    }

    @Test
    fun maximumUint32SequenceSurvivesTheRoundTrip() {
        val packet = LinkPacket(LinkChannel.IMU, LinkFlags.NONE, 0xFFFF_FFFFL, -1L, byteArrayOf(9))
        val reader = LinkFrameReader()
        reader.append(LinkProtocol.encode(packet))

        val decoded = reader.next()!!
        assertEquals(0xFFFF_FFFFL, decoded.sequence)
        assertEquals(-1L, decoded.senderTimestampNs)
    }

    @Test
    fun corruptedMagicFailsLoudlyInsteadOfResynchronising() {
        val encoded = LinkProtocol.encode(
            LinkPacket(LinkChannel.VIDEO, LinkFlags.NONE, 1L, 1L, byteArrayOf(1, 2, 3))
        )
        encoded[0] = 0x00

        val reader = LinkFrameReader()
        reader.append(encoded)

        val error = assertThrows(LinkProtocolException::class.java) { reader.next() }
        assertTrue(error.message!!.contains("magic"))
    }

    @Test
    fun versionMismatchIsRejected() {
        val encoded = LinkProtocol.encode(
            LinkPacket(LinkChannel.VIDEO, LinkFlags.NONE, 1L, 1L, byteArrayOf(1))
        )
        encoded[2] = 99

        val reader = LinkFrameReader()
        reader.append(encoded)

        assertThrows(LinkProtocolException::class.java) { reader.next() }
    }

    @Test
    fun unknownChannelIsRejected() {
        val encoded = LinkProtocol.encode(
            LinkPacket(LinkChannel.VIDEO, LinkFlags.NONE, 1L, 1L, byteArrayOf(1))
        )
        encoded[3] = 0x7F

        val reader = LinkFrameReader()
        reader.append(encoded)

        assertThrows(LinkProtocolException::class.java) { reader.next() }
    }

    /** 损坏的长度字段不得导致巨额分配 —— 这是「包长有上限」那条要求的直接验证。 */
    @Test
    fun oversizedLengthIsRejectedBeforeAllocating() {
        val header = ByteArray(LinkWire.HEADER_SIZE)
        header[0] = LinkWire.MAGIC_0
        header[1] = LinkWire.MAGIC_1
        header[2] = LinkWire.VERSION.toByte()
        header[3] = LinkChannel.VIDEO.code.toByte()
        header[5] = 0xFF.toByte()
        header[6] = 0xFF.toByte()
        header[7] = 0xFF.toByte()

        val reader = LinkFrameReader(maxPayloadBytes = 1024)
        reader.append(header)

        val error = assertThrows(LinkProtocolException::class.java) { reader.next() }
        assertTrue(error.message!!.contains("超过上限"))
    }

    @Test
    fun encodingAPayloadOverTheLimitIsRejected() {
        val packet = LinkPacket(
            LinkChannel.VIDEO, LinkFlags.NONE, 1L, 1L, ByteArray(2048)
        )
        assertThrows(LinkProtocolException::class.java) {
            LinkProtocol.encode(packet, maxPayloadBytes = 1024)
        }
    }

    /** 长连接下读缓冲不能无限增长。 */
    @Test
    fun readerBufferDoesNotGrowUnboundedOverManyPackets() {
        val reader = LinkFrameReader(initialCapacity = 1024)
        val packet = LinkPacket(LinkChannel.VIDEO, LinkFlags.NONE, 1L, 1L, ByteArray(200) { 1 })
        val encoded = LinkProtocol.encode(packet)

        repeat(10_000) {
            reader.append(encoded)
            assertNotNull(reader.next())
        }
        assertEquals(0, reader.pendingBytes())
    }

    @Test
    fun controlChannelsArePrioritisedAndSpeakIsTheOnlyDownlink() {
        assertTrue(LinkChannel.CONTROL.isHighPriority)
        assertTrue(LinkChannel.SPEAK.isHighPriority)
        assertTrue(LinkChannel.SPEAK_STATUS.isHighPriority)
        assertTrue("视频不能插队，否则控制包会被大帧压住", !LinkChannel.VIDEO.isHighPriority)

        val downlink = LinkChannel.entries.filter { !it.isUplink }
        assertEquals(listOf(LinkChannel.SPEAK), downlink)
    }

    @Test
    fun channelCodesAreUniqueAndRoundTrip() {
        val codes = LinkChannel.entries.map { it.code }
        assertEquals("通道码有重复", codes.size, codes.toSet().size)
        for (channel in LinkChannel.entries) {
            assertEquals(channel, LinkChannel.fromCode(channel.code))
        }
        assertNull(LinkChannel.fromCode(0xEE))
    }
}
