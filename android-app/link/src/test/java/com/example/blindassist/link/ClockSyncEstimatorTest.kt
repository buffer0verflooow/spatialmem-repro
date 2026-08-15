package com.example.blindassist.link

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

class ClockSyncEstimatorTest {

    private val ms = 1_000_000L

    /**
     * 构造一次往返：真实偏移 trueOffset（对端时钟 − 本端时钟），
     * 去程 outboundNs、回程 inboundNs、对端处理 processingNs。
     */
    private fun sample(
        localSendNs: Long,
        trueOffsetNs: Long,
        outboundNs: Long,
        inboundNs: Long,
        processingNs: Long = 0
    ): ClockSyncSample {
        val remoteRecv = localSendNs + outboundNs + trueOffsetNs
        val remoteSend = remoteRecv + processingNs
        val localRecv = localSendNs + outboundNs + processingNs + inboundNs
        return ClockSyncSample(localSendNs, remoteRecv, remoteSend, localRecv)
    }

    @Test
    fun symmetricDelayRecoversTheOffsetExactly() {
        val trueOffset = 3_600_000_000_000L // 对端比本端早开机 1 小时
        val sample = sample(localSendNs = 1_000 * ms, trueOffsetNs = trueOffset, outboundNs = 20 * ms, inboundNs = 20 * ms)

        assertEquals(trueOffset, sample.offsetNs)
        assertEquals(40 * ms, sample.roundTripNs)
        assertEquals(20 * ms, sample.uncertaintyNs)
    }

    @Test
    fun processingTimeIsExcludedFromRoundTrip() {
        val sample = sample(
            localSendNs = 0,
            trueOffsetNs = 0,
            outboundNs = 15 * ms,
            inboundNs = 15 * ms,
            processingNs = 200 * ms
        )
        assertEquals("对端处理耗时不应算进往返", 30 * ms, sample.roundTripNs)
        assertEquals(0L, sample.offsetNs)
    }

    /** 单向延迟不对称时，误差必须落在 rtt/2 这个硬上界内。 */
    @Test
    fun asymmetricDelayErrorStaysWithinTheStatedBound() {
        val trueOffset = 500_000_000L
        for (outbound in listOf(1L, 5L, 20L, 39L)) {
            val inbound = 40L - outbound
            val s = sample(0, trueOffset, outbound * ms, inbound * ms)
            val error = abs(s.offsetNs - trueOffset)
            assertTrue(
                "去程${outbound}ms/回程${inbound}ms 时误差 $error 超出上界 ${s.uncertaintyNs}",
                error <= s.uncertaintyNs
            )
        }
    }

    @Test
    fun lowestRoundTripSampleWins() {
        val estimator = ClockSyncEstimator()
        val trueOffset = 1_000_000_000L

        // 先加一条高延迟且严重不对称的样本，再加一条低延迟样本。
        estimator.addSample(sample(0, trueOffset, outboundNs = 190 * ms, inboundNs = 10 * ms))
        estimator.addSample(sample(1000 * ms, trueOffset, outboundNs = 5 * ms, inboundNs = 5 * ms))
        estimator.addSample(sample(2000 * ms, trueOffset, outboundNs = 150 * ms, inboundNs = 20 * ms))

        assertEquals(10 * ms, estimator.bestRoundTripNs())
        assertEquals(trueOffset, estimator.offsetNs())
        assertEquals(5 * ms, estimator.uncertaintyNs())
    }

    @Test
    fun implausibleSamplesAreRejectedAndCounted() {
        val estimator = ClockSyncEstimator()

        // 收到早于发出
        assertFalse(estimator.addSample(ClockSyncSample(1000, 500, 600, 900)))
        // 对端发出早于对端收到
        assertFalse(estimator.addSample(ClockSyncSample(0, 1000, 500, 2000)))
        // 处理时间超过总往返 -> rtt 为负
        assertFalse(estimator.addSample(ClockSyncSample(0, 10, 5000, 100)))

        assertEquals(3, estimator.rejectedSampleCount())
        assertEquals(0, estimator.sampleCount())
        assertNull(estimator.offsetNs())
    }

    @Test
    fun convergenceRequiresBothEnoughSamplesAndTightUncertainty() {
        val estimator = ClockSyncEstimator(minSamplesForConvergence = 3, maxUncertaintyNs = 50 * ms)

        estimator.addSample(sample(0, 0, 5 * ms, 5 * ms))
        assertFalse("样本数不足时不应判为收敛", estimator.isConverged())

        estimator.addSample(sample(1000 * ms, 0, 5 * ms, 5 * ms))
        assertFalse(estimator.isConverged())

        estimator.addSample(sample(2000 * ms, 0, 5 * ms, 5 * ms))
        assertTrue(estimator.isConverged())
    }

    @Test
    fun highLatencyLinkNeverConvergesSoTimestampsStayUnusable() {
        // rtt 300ms -> uncertainty 150ms，超过 50ms 门槛。
        val estimator = ClockSyncEstimator(minSamplesForConvergence = 3, maxUncertaintyNs = 50 * ms)
        repeat(10) { index ->
            estimator.addSample(sample(index * 1000L * ms, 0, 150 * ms, 150 * ms))
        }

        assertEquals(10, estimator.sampleCount())
        assertFalse("rtt 300ms 不该被判为满足 50ms 误差要求", estimator.isConverged())
        assertNull("未收敛时换算必须返回 null", estimator.toReceiverNs(12345L))
        assertNotNull("诊断用的强制换算仍应可用", estimator.toReceiverNsUnchecked(12345L))
    }

    @Test
    fun conversionMapsRemoteTimestampsIntoTheLocalDomain() {
        val estimator = ClockSyncEstimator(minSamplesForConvergence = 1)
        val trueOffset = 7_200_000_000_000L // 对端早 2 小时
        estimator.addSample(sample(0, trueOffset, 5 * ms, 5 * ms))

        val remoteCapture = trueOffset + 500 * ms
        assertEquals(500 * ms, estimator.toReceiverNs(remoteCapture))
    }

    /**
     * 这条测的是这个类存在的理由：不换算的话，端到端延迟会等于两机时钟偏移。
     */
    @Test
    fun withoutConversionTheLatencyWouldBeTheClockOffsetNotTheRealDelay() {
        val estimator = ClockSyncEstimator(minSamplesForConvergence = 1)
        val trueOffset = 3_600_000_000_000L // 1 小时
        estimator.addSample(sample(0, trueOffset, 5 * ms, 5 * ms))

        val localNow = 10_000 * ms
        val remoteCaptureNs = trueOffset + localNow - 120 * ms // 真实是 120ms 之前采集的

        val naiveLatencyMs = (localNow - remoteCaptureNs) / 1e6
        assertTrue("不换算得到的是负的一小时，显然不可用", naiveLatencyMs < -3_000_000)

        val correctedNs = estimator.toReceiverNs(remoteCaptureNs)!!
        assertEquals(120.0, (localNow - correctedNs) / 1e6, 0.001)
    }

    @Test
    fun staleSamplesAreEvictedSoDriftIsNotMasked() {
        val estimator = ClockSyncEstimator(maxSampleAgeNs = 60_000_000_000L)

        // 一条很早的、极低延迟的样本。
        estimator.addSample(sample(0, 1_000_000_000L, 1 * ms, 1 * ms))
        assertEquals(1_000_000_000L, estimator.offsetNs())

        // 120 秒后，偏移已漂移，且新样本延迟更高。
        estimator.addSample(sample(120_000 * ms, 1_000_100_000L, 10 * ms, 10 * ms))

        assertEquals("超龄样本应被淘汰", 1, estimator.sampleCount())
        assertEquals(
            "淘汰后应采用较新的样本，即使它 rtt 更大",
            1_000_100_000L,
            estimator.offsetNs()
        )
    }

    @Test
    fun atLeastOneSampleIsAlwaysRetained() {
        val estimator = ClockSyncEstimator(maxSampleAgeNs = 1_000L)
        estimator.addSample(sample(0, 0, 1 * ms, 1 * ms))
        estimator.addSample(sample(10_000_000 * ms, 0, 1 * ms, 1 * ms))
        assertEquals(1, estimator.sampleCount())
        assertNotNull(estimator.offsetNs())
    }

    @Test
    fun windowIsBoundedByCount() {
        val estimator = ClockSyncEstimator(maxSamples = 4)
        repeat(20) { index ->
            estimator.addSample(sample(index * 1000L * ms, 0, 5 * ms, 5 * ms))
        }
        assertEquals(4, estimator.sampleCount())
    }

    @Test
    fun driftIsEstimatedFromTheRetainedWindow() {
        val estimator = ClockSyncEstimator()
        // 每 2 秒一条，共 12 条，跨度 22 秒（需超过 10 秒的最小跨度门槛）。
        // 每秒漂移 1000ns（1 µs/s，约 1 ppm）。
        for (index in 0 until 12) {
            val seconds = index * 2L
            val offset = 1_000_000L + seconds * 1_000L
            estimator.addSample(sample(seconds * 1000L * ms, offset, 2 * ms, 2 * ms))
        }

        val drift = estimator.driftNsPerSecond()
        assertNotNull("时间跨度足够时应能给出漂移估计", drift)
        assertEquals(1_000.0, drift!!, 1.0)
    }

    /**
     * 用实测漂移验证再同步周期。
     *
     * 若漂移 r ns/s、同步周期 T 秒，则周期末的额外误差约 r·T。这条测试固定
     * 了这个推算方式，方便真机拿到实测漂移后直接算出该用多长的同步周期。
     */
    @Test
    fun measuredDriftBoundsTheAcceptableResyncInterval() {
        val estimator = ClockSyncEstimator()
        // 20 ppm 是常见晶振指标，即 20_000 ns/s。
        val driftNsPerSec = 20_000L
        for (index in 0 until 12) {
            val seconds = index * 2L
            estimator.addSample(
                sample(seconds * 1000L * ms, 1_000_000L + seconds * driftNsPerSec, 2 * ms, 2 * ms)
            )
        }

        val measured = estimator.driftNsPerSecond()!!
        assertEquals(20_000.0, measured, 100.0)

        // 30 秒同步周期下，周期末因漂移引入的额外误差约 0.6ms，远小于 50ms 预算。
        val errorAt30sMs = measured * 30 / 1e6
        assertTrue("30 秒同步周期下漂移误差应远小于 50ms 预算，实际 ${errorAt30sMs}ms", errorAt30sMs < 5.0)

        // 但一小时不同步就会超预算，所以周期性再同步是必须的。
        val errorAt1hMs = measured * 3600 / 1e6
        assertTrue("一小时不同步会超出 50ms 预算，实际 ${errorAt1hMs}ms", errorAt1hMs > 50.0)
    }

    @Test
    fun driftIsNullWhenTheWindowIsTooShort() {
        val estimator = ClockSyncEstimator()
        repeat(5) { index ->
            estimator.addSample(sample(index * 100L * ms, 0, 2 * ms, 2 * ms))
        }
        assertNull("跨度不足时不该给出漂移数字", estimator.driftNsPerSecond())
    }

    /** 重连后必须清空：任一端睡眠都会让偏移跳变，旧样本是有害的。 */
    @Test
    fun resetClearsEverythingSoAReconnectDoesNotReuseAStaleOffset() {
        val estimator = ClockSyncEstimator(minSamplesForConvergence = 1)
        estimator.addSample(sample(0, 1_000_000_000L, 2 * ms, 2 * ms))
        assertTrue(estimator.isConverged())

        estimator.reset()

        assertEquals(0, estimator.sampleCount())
        assertFalse(estimator.isConverged())
        assertNull(estimator.offsetNs())
        assertNull(estimator.toReceiverNs(1L))
    }

    /** 默认门槛应当正好对应 PRD F6-9 的 50ms 要求，即 rtt ≤ 100ms。 */
    @Test
    fun defaultThresholdMatchesTheFiftyMillisecondRequirement() {
        assertEquals(50 * ms, ClockSyncEstimator.DEFAULT_MAX_UNCERTAINTY_NS)

        val justInside = ClockSyncEstimator(minSamplesForConvergence = 1)
        justInside.addSample(sample(0, 0, 50 * ms, 50 * ms)) // rtt 100ms -> uncertainty 50ms
        assertTrue(justInside.isConverged())

        val justOutside = ClockSyncEstimator(minSamplesForConvergence = 1)
        justOutside.addSample(sample(0, 0, 51 * ms, 51 * ms)) // rtt 102ms -> uncertainty 51ms
        assertFalse(justOutside.isConverged())
    }
}
