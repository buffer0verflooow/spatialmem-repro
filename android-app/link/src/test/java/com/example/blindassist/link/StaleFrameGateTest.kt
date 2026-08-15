package com.example.blindassist.link

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StaleFrameGateTest {

    private val ms = 1_000_000L

    @Test
    fun freshFrameIsAccepted() {
        val gate = StaleFrameGate()
        val decision = gate.evaluate(capturePhoneNs = 1_000 * ms, arrivalNs = 1_100 * ms, nowNs = 1_150 * ms)

        assertTrue(decision.accepted)
        assertEquals(150.0, decision.ageMs, 0.001)
        assertTrue(decision.isExact)
        assertEquals(1, gate.acceptedCount)
        assertEquals(0, gate.droppedCount)
    }

    @Test
    fun frameOlderThanTheBudgetIsDropped() {
        val gate = StaleFrameGate()
        val decision = gate.evaluate(capturePhoneNs = 1_000 * ms, arrivalNs = 1_300 * ms, nowNs = 1_500 * ms)

        assertFalse(decision.accepted)
        assertEquals(500.0, decision.ageMs, 0.001)
        assertEquals(1, gate.droppedCount)
    }

    @Test
    fun theBoundaryIsInclusive() {
        val gate = StaleFrameGate(maxAgeNs = 400 * ms)
        assertTrue(gate.evaluate(0, 0, 400 * ms).accepted)
        assertFalse(gate.evaluate(0, 0, 400 * ms + 1).accepted)
    }

    /**
     * 对齐未收敛时仍然要能工作：改用到达时间，得到的是真实年龄的**下界**。
     */
    @Test
    fun withoutClockSyncTheGateFallsBackToArrivalTimeAndMarksItInexact() {
        val gate = StaleFrameGate()
        val decision = gate.evaluate(capturePhoneNs = null, arrivalNs = 1_000 * ms, nowNs = 1_100 * ms)

        assertTrue(decision.accepted)
        assertEquals(100.0, decision.ageMs, 0.001)
        assertFalse("到达时间只是下界，不能标为精确", decision.isExact)
        assertEquals(StaleFrameGate.AgeBasis.ARRIVAL_LOWER_BOUND, decision.basis)
        assertEquals(1, gate.acceptedWithoutSyncCount)
    }

    @Test
    fun arrivalBasedAgeUnderestimatesTheRealAge() {
        val gate = StaleFrameGate()
        val captureNs = 1_000 * ms
        val arrivalNs = 1_120 * ms // 传输花了 120ms
        val nowNs = 1_200 * ms

        val withSync = gate.evaluate(captureNs, arrivalNs, nowNs)
        val withoutSync = gate.evaluate(null, arrivalNs, nowNs)

        assertEquals(200.0, withSync.ageMs, 0.001)
        assertEquals(80.0, withoutSync.ageMs, 0.001)
        assertTrue("无对齐时必然低估", withoutSync.ageNs < withSync.ageNs)
    }

    /** 对齐误差或时钟跳变可能让时间戳超前于当前时刻，不能把它当成"非常新"。 */
    @Test
    fun negativeAgeIsClampedToZeroRatherThanTreatedAsVeryFresh() {
        val gate = StaleFrameGate()
        val decision = gate.evaluate(capturePhoneNs = 2_000 * ms, arrivalNs = 1_000 * ms, nowNs = 1_000 * ms)

        assertEquals(0L, decision.ageNs)
        assertTrue(decision.accepted)
    }

    @Test
    fun dropRateAndPeakAgeAreTrackedForSessionReporting() {
        val gate = StaleFrameGate(maxAgeNs = 100 * ms)
        repeat(19) { gate.evaluate(0, 0, 50 * ms) }
        gate.evaluate(0, 0, 900 * ms)

        assertEquals(20, gate.totalCount())
        assertEquals(1, gate.droppedCount)
        assertEquals(0.05, gate.dropRate(), 1e-9)
        assertEquals(900 * ms, gate.maxObservedAgeNs)
    }

    @Test
    fun emptyGateReportsZeroDropRate() {
        assertEquals(0.0, StaleFrameGate().dropRate(), 0.0)
    }

    @Test
    fun resetClearsCounters() {
        val gate = StaleFrameGate()
        gate.evaluate(0, 0, 10_000 * ms)
        gate.reset()

        assertEquals(0, gate.totalCount())
        assertEquals(0L, gate.maxObservedAgeNs)
    }

    @Test
    fun defaultBudgetIsFourHundredMilliseconds() {
        assertEquals(400 * ms, StaleFrameGate.DEFAULT_MAX_AGE_NS)
    }
}

class SequenceTrackerTest {

    @Test
    fun firstPacketEstablishesTheBaseline() {
        val tracker = SequenceTracker()
        assertEquals(SequenceTracker.Observation.FIRST, tracker.observe(100))
        assertEquals(0, tracker.lostCount)
    }

    @Test
    fun consecutiveSequencesAreInOrder() {
        val tracker = SequenceTracker()
        tracker.observe(1)
        for (seq in 2L..50L) {
            assertEquals(SequenceTracker.Observation.IN_ORDER, tracker.observe(seq))
        }
        assertEquals(0, tracker.lostCount)
        assertEquals(0.0, tracker.lossRate(), 0.0)
    }

    @Test
    fun gapCountsEveryMissingSequence() {
        val tracker = SequenceTracker()
        tracker.observe(1)
        assertEquals(SequenceTracker.Observation.GAP, tracker.observe(5))
        assertEquals("1 和 5 之间少了 3 个", 3, tracker.lostCount)
    }

    @Test
    fun duplicateIsDetectedAndNotCountedAsLoss() {
        val tracker = SequenceTracker()
        tracker.observe(1)
        tracker.observe(2)
        assertEquals(SequenceTracker.Observation.DUPLICATE, tracker.observe(2))
        assertEquals(1, tracker.duplicateCount)
        assertEquals(0, tracker.lostCount)
    }

    /** 迟到的包应把先前记的丢失抵消掉，否则丢包率会被系统性高估。 */
    @Test
    fun lateArrivalCancelsThePreviouslyCountedLoss() {
        val tracker = SequenceTracker()
        tracker.observe(1)
        tracker.observe(3) // 认为 2 丢了
        assertEquals(1, tracker.lostCount)

        assertEquals(SequenceTracker.Observation.REORDERED, tracker.observe(2))
        assertEquals("2 迟到了，不应再算作丢失", 0, tracker.lostCount)
        assertEquals(1, tracker.reorderedCount)
    }

    @Test
    fun sequenceWrapAroundIsHandledAsNormalProgression() {
        val tracker = SequenceTracker()
        tracker.observe(0xFFFF_FFFEL)
        assertEquals(SequenceTracker.Observation.IN_ORDER, tracker.observe(0xFFFF_FFFFL))
        assertEquals(
            "回绕到 0 应视为正常前进，不是丢了 40 亿个包",
            SequenceTracker.Observation.IN_ORDER,
            tracker.observe(0)
        )
        assertEquals(SequenceTracker.Observation.IN_ORDER, tracker.observe(1))
        assertEquals(0, tracker.lostCount)
    }

    @Test
    fun gapAcrossTheWrapBoundaryCountsCorrectly() {
        val tracker = SequenceTracker()
        tracker.observe(0xFFFF_FFFFL)
        assertEquals(SequenceTracker.Observation.GAP, tracker.observe(2))
        assertEquals("0 和 1 丢了", 2, tracker.lostCount)
    }

    /** 对端重启会让序号从头开始；这不该被记成 40 亿次丢包。 */
    @Test
    fun peerRestartRebaselinesInsteadOfCountingAbsurdLoss() {
        val tracker = SequenceTracker()
        tracker.observe(1_000_000)
        tracker.observe(1_000_001)
        tracker.observe(0)

        assertTrue("重启后不应记入天量丢包", tracker.lostCount < 1000)
        assertEquals(SequenceTracker.Observation.IN_ORDER, tracker.observe(1))
    }

    @Test
    fun lossRateMatchesTheFivePercentAcceptanceTarget() {
        val tracker = SequenceTracker()
        // 收到 19 个、丢 1 个 -> 1/20 = 5%，正好是 F6 的丢帧率验收线。
        tracker.observe(0)
        for (seq in 1L..17L) tracker.observe(seq)
        tracker.observe(19) // 18 丢了

        assertEquals(19, tracker.receivedCount)
        assertEquals(1, tracker.lostCount)
        assertEquals(0.05, tracker.lossRate(), 1e-9)
    }

    @Test
    fun resetClearsState() {
        val tracker = SequenceTracker()
        tracker.observe(1)
        tracker.observe(10)
        tracker.reset()

        assertEquals(0, tracker.lostCount)
        assertEquals(0, tracker.receivedCount)
        assertEquals(SequenceTracker.Observation.FIRST, tracker.observe(5))
    }
}
