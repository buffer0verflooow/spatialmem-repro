package com.example.blindassist.link

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LinkSendQueueTest {

    private fun video(flags: Int, sequence: Long) = LinkPacket(
        channel = LinkChannel.VIDEO,
        flags = flags,
        sequence = sequence,
        senderTimestampNs = sequence,
        payload = ByteArray(0)
    )

    private fun control(sequence: Long) = LinkPacket(
        channel = LinkChannel.CONTROL,
        flags = LinkFlags.NONE,
        sequence = sequence,
        senderTimestampNs = sequence,
        payload = ByteArray(0)
    )

    private fun speak(sequence: Long) = LinkPacket(
        channel = LinkChannel.SPEAK,
        flags = LinkFlags.NONE,
        sequence = sequence,
        senderTimestampNs = sequence,
        payload = ByteArray(0)
    )

    private fun speakStatus(sequence: Long) = LinkPacket(
        channel = LinkChannel.SPEAK_STATUS,
        flags = LinkFlags.NONE,
        sequence = sequence,
        senderTimestampNs = sequence,
        payload = ByteArray(0)
    )

    // ---------- 7. 高优先级插队 ----------

    @Test
    fun laterControlLeavesTheQueueBeforeEarlierVideo() {
        val queue = LinkSendQueue(capacity = 4)
        queue.offer(video(LinkFlags.NONE, 1))
        queue.offer(control(2))

        assertEquals(LinkChannel.CONTROL, queue.poll()!!.channel)
    }

    // ---------- 8. 同优先级内 FIFO ----------

    @Test
    fun samePriorityPacketsStayFifo() {
        val queue = LinkSendQueue(capacity = 4)

        queue.offer(video(LinkFlags.NONE, 1))
        queue.offer(video(LinkFlags.NONE, 2))
        assertEquals(1, queue.poll()!!.sequence)
        assertEquals(2, queue.poll()!!.sequence)

        queue.offer(control(3))
        queue.offer(control(4))
        assertEquals(3, queue.poll()!!.sequence)
        assertEquals(4, queue.poll()!!.sequence)
    }

    // ---------- 9. 队满丢最老的非关键帧并计数 ----------

    @Test
    fun whenFullOfferingVideoDropsOldestNonKeyframeAndCountsIt() {
        val queue = LinkSendQueue(capacity = 2)
        queue.offer(video(LinkFlags.NONE, 1))
        queue.offer(video(LinkFlags.KEYFRAME, 2))
        queue.offer(video(LinkFlags.NONE, 3)) // 队满：丢最老的非关键帧（seq=1）

        assertEquals(1, queue.droppedVideoPacketCount)
        assertEquals(0, queue.droppedKeyframeCount)
        assertEquals(2, queue.poll()!!.sequence)
        assertEquals(3, queue.poll()!!.sequence)
    }

    // ---------- 10. 队满时 CONTROL 必须入队且不挤掉任何 CONTROL ----------

    @Test
    fun whenFullOfferingControlSucceedsAndNeverDropsControl() {
        val queue = LinkSendQueue(capacity = 2)
        queue.offer(video(LinkFlags.NONE, 1))
        queue.offer(video(LinkFlags.NONE, 2))

        assertTrue(queue.offer(control(3)))
        assertEquals(0, queue.droppedVideoPacketCount)
        assertEquals(0, queue.droppedKeyframeCount)
        assertEquals(3, queue.size())

        // 两个视频都还在，控制包先出。
        assertEquals(LinkChannel.CONTROL, queue.poll()!!.channel)
        assertEquals(1, queue.poll()!!.sequence)
        assertEquals(2, queue.poll()!!.sequence)
    }

    // ---------- 11. 队列塞满控制类通道时一个都不丢 ----------

    @Test
    fun controlSpeakAndSpeakStatusAreNeverDroppedEvenWhenQueueIsFull() {
        val queue = LinkSendQueue(capacity = 3)
        queue.offer(control(1))
        queue.offer(speak(2))
        queue.offer(speakStatus(3))

        // 继续塞控制类：必须全部入队，计数保持 0。
        queue.offer(control(4))
        queue.offer(speak(5))
        queue.offer(speakStatus(6))

        assertEquals(0, queue.droppedVideoPacketCount)
        assertEquals(0, queue.droppedKeyframeCount)
        assertEquals(6, queue.size())
    }

    // ---------- 12. 只剩关键帧时走单独的关键帧计数 ----------

    @Test
    fun whenOnlyKeyframesRemainDropUsesTheSeparateKeyframeCounter() {
        val queue = LinkSendQueue(capacity = 2)
        queue.offer(video(LinkFlags.KEYFRAME, 1))
        queue.offer(video(LinkFlags.KEYFRAME, 2))
        queue.offer(video(LinkFlags.NONE, 3)) // 只剩关键帧：丢最老的关键帧（seq=1）

        assertEquals(1, queue.droppedVideoPacketCount)
        assertEquals(1, queue.droppedKeyframeCount)
        assertEquals(2, queue.poll()!!.sequence)
        assertEquals(3, queue.poll()!!.sequence)

        // 随后的普通（非关键帧）丢弃只走视频计数，不碰关键帧计数 —— 两计数确实分开。
        queue.offer(video(LinkFlags.NONE, 4))
        queue.offer(video(LinkFlags.NONE, 5))
        queue.offer(video(LinkFlags.NONE, 6))
        assertEquals(2, queue.droppedVideoPacketCount)
        assertEquals(1, queue.droppedKeyframeCount)
    }

    // ---------- 13. 空队列 poll 返回 null，不抛异常、不阻塞 ----------

    @Test
    fun emptyQueuePollReturnsNullWithoutBlocking() {
        val queue = LinkSendQueue(capacity = 2)
        assertNull(queue.poll())
        assertNull(queue.poll())
        assertEquals(0, queue.droppedVideoPacketCount)
        assertEquals(0, queue.droppedKeyframeCount)
    }
}
