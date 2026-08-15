package com.example.blindassist.link

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LinkStateMachineTest {

    private fun capabilities() = GlassCapabilities(
        protocolVersion = LinkWire.VERSION,
        deviceModel = "X3Pro",
        videoModes = listOf(VideoMode(640, 360, 30)),
        hasHardwareAvcEncoder = true,
        hasLocalChineseTts = true,
        hasRotationVector = true,
        hasSixDof = false,
        hasTempleTouch = true,
        hasWearDetection = true
    )

    private fun announcements(actions: List<LinkAction>) =
        actions.filterIsInstance<LinkAction.AnnounceToUser>()

    private fun connects(actions: List<LinkAction>) =
        actions.filterIsInstance<LinkAction.ScheduleConnect>()

    private fun LinkStateMachine.driveToStreaming() {
        onEvent(LinkEvent.StartRequested)
        onEvent(LinkEvent.TransportConnected)
        onEvent(LinkEvent.HandshakeCompleted(capabilities()))
        onEvent(LinkEvent.ClockConverged)
    }

    @Test
    fun happyPathReachesStreamingAndAnnouncesOnce() {
        val machine = LinkStateMachine()

        val start = machine.onEvent(LinkEvent.StartRequested)
        assertEquals(LinkState.CONNECTING, machine.state)
        assertEquals(0L, connects(start).single().delayMs)

        val connected = machine.onEvent(LinkEvent.TransportConnected)
        assertEquals(LinkState.HANDSHAKING, machine.state)
        assertTrue(connected.contains(LinkAction.SendHello))

        val shook = machine.onEvent(LinkEvent.HandshakeCompleted(capabilities()))
        assertEquals(LinkState.SYNCING, machine.state)
        assertTrue("握手完成后必须重新做时钟对齐", shook.contains(LinkAction.StartClockSync))

        val synced = machine.onEvent(LinkEvent.ClockConverged)
        assertEquals(LinkState.STREAMING, machine.state)
        assertEquals("眼镜已连接", announcements(synced).single().message)
        assertEquals(capabilities(), machine.negotiatedCapabilities)
    }

    /** 时钟未收敛前不进入 STREAMING —— 否则会用未对齐的时间戳做安全判断。 */
    @Test
    fun streamingIsNotReachedUntilTheClockConverges() {
        val machine = LinkStateMachine()
        machine.onEvent(LinkEvent.StartRequested)
        machine.onEvent(LinkEvent.TransportConnected)
        machine.onEvent(LinkEvent.HandshakeCompleted(capabilities()))

        assertEquals(LinkState.SYNCING, machine.state)
        assertFalse(machine.state == LinkState.STREAMING)
    }

    @Test
    fun losingAWorkingLinkAnnouncesImmediatelyAndCritically() {
        val machine = LinkStateMachine()
        machine.driveToStreaming()

        val lost = machine.onEvent(LinkEvent.HeartbeatTimeout)

        assertEquals(LinkState.RECONNECTING, machine.state)
        assertTrue(lost.contains(LinkAction.CloseTransport))
        val announcement = announcements(lost).single()
        assertEquals("眼镜连接断开，正在重连", announcement.message)
        assertTrue("用户正在依赖它，掉线必须是 critical", announcement.critical)
    }

    /** 每次重试都出声对盲人用户是灾难。掉线只播一次，直到恢复。 */
    @Test
    fun repeatedRetriesDoNotSpamTheUser() {
        val machine = LinkStateMachine()
        machine.driveToStreaming()

        val first = machine.onEvent(LinkEvent.HeartbeatTimeout)
        assertEquals(1, announcements(first).size)

        repeat(10) { attempt ->
            val retry = machine.onEvent(LinkEvent.TransportFailed("第 $attempt 次"))
            assertTrue("第 $attempt 次重试不应再出声", announcements(retry).isEmpty())
        }
    }

    @Test
    fun recoveryAfterAnAnnouncedOutageAnnouncesReconnection() {
        val machine = LinkStateMachine()
        machine.driveToStreaming()
        machine.onEvent(LinkEvent.HeartbeatTimeout)

        machine.onEvent(LinkEvent.TransportConnected)
        machine.onEvent(LinkEvent.HandshakeCompleted(capabilities()))
        val recovered = machine.onEvent(LinkEvent.ClockConverged)

        assertEquals(LinkState.STREAMING, machine.state)
        assertEquals("眼镜已重新连接", announcements(recovered).single().message)
    }

    @Test
    fun afterRecoveryANewOutageIsAnnouncedAgain() {
        val machine = LinkStateMachine()
        machine.driveToStreaming()
        machine.onEvent(LinkEvent.HeartbeatTimeout)
        machine.onEvent(LinkEvent.TransportConnected)
        machine.onEvent(LinkEvent.HandshakeCompleted(capabilities()))
        machine.onEvent(LinkEvent.ClockConverged)

        val secondOutage = machine.onEvent(LinkEvent.PeerDisconnected("眼镜被摘下"))
        assertEquals(1, announcements(secondOutage).size)
        assertTrue(announcements(secondOutage).single().critical)
    }

    /** 从未连上过时，前几次失败保持安静，超过阈值才提示。 */
    @Test
    fun initialConnectFailuresStaySilentUntilTheThreshold() {
        val machine = LinkStateMachine()
        machine.onEvent(LinkEvent.StartRequested)

        val quiet = mutableListOf<LinkAction.AnnounceToUser>()
        repeat(LinkStateMachine.ANNOUNCE_FAILURE_AFTER_ATTEMPTS - 1) {
            quiet += announcements(machine.onEvent(LinkEvent.TransportFailed("no route")))
        }
        assertTrue("阈值以内应保持安静", quiet.isEmpty())

        val loud = announcements(machine.onEvent(LinkEvent.TransportFailed("no route")))
        assertEquals("连接不上眼镜，请检查眼镜是否开机并靠近手机", loud.single().message)
        assertTrue(loud.single().critical)
    }

    /** 5 秒内必须有多次重试机会，否则达不到 F6-7 的重连目标。 */
    @Test
    fun backoffGivesMultipleRetriesWithinFiveSeconds() {
        val machine = LinkStateMachine()

        var cumulative = 0L
        var retriesWithinFiveSeconds = 0
        for (failure in 1..10) {
            cumulative += machine.backoffDelayMs(failure)
            if (cumulative <= 5_000) retriesWithinFiveSeconds++
        }

        assertTrue(
            "5 秒内只有 $retriesWithinFiveSeconds 次重试，不足以支撑 ≤5s 重连",
            retriesWithinFiveSeconds >= 4
        )
        assertEquals(250L, machine.backoffDelayMs(1))
        assertEquals(3_750L, machine.cumulativeDelayMsForAttempts(4))
    }

    @Test
    fun backoffIsCappedSoItNeverGrowsUnbounded() {
        val machine = LinkStateMachine()
        val cap = LinkStateMachine.DEFAULT_BACKOFF_MS.last()
        for (failure in 5..100) {
            assertEquals(cap, machine.backoffDelayMs(failure))
        }
    }

    @Test
    fun eachFailureSchedulesExactlyOneReconnect() {
        val machine = LinkStateMachine()
        machine.driveToStreaming()

        val actions = machine.onEvent(LinkEvent.HeartbeatTimeout)
        val scheduled = connects(actions).single()
        assertEquals(250L, scheduled.delayMs)
        assertEquals(2, scheduled.attempt)
    }

    /** 握手被拒通常是版本不符，重试无意义，应停下并告知。 */
    @Test
    fun handshakeRejectionStopsInsteadOfRetryingForever() {
        val machine = LinkStateMachine()
        machine.onEvent(LinkEvent.StartRequested)
        machine.onEvent(LinkEvent.TransportConnected)

        val rejected = machine.onEvent(LinkEvent.HandshakeRejected("protocol 2 vs 1"))

        assertEquals(LinkState.IDLE, machine.state)
        assertTrue("被拒后不应再安排重连", connects(rejected).isEmpty())
        assertEquals("眼镜连接不兼容，请更新眼镜端应用", announcements(rejected).single().message)
    }

    @Test
    fun stopFromAnyStateReturnsToIdleAndClosesTransport() {
        for (drive in listOf<(LinkStateMachine) -> Unit>(
            { it.onEvent(LinkEvent.StartRequested) },
            {
                it.onEvent(LinkEvent.StartRequested)
                it.onEvent(LinkEvent.TransportConnected)
            },
            { it.driveToStreaming() },
            {
                it.driveToStreaming()
                it.onEvent(LinkEvent.HeartbeatTimeout)
            }
        )) {
            val machine = LinkStateMachine()
            drive(machine)
            val stopped = machine.onEvent(LinkEvent.StopRequested)

            assertEquals(LinkState.IDLE, machine.state)
            assertTrue(stopped.contains(LinkAction.CloseTransport))
        }
    }

    @Test
    fun eventsThatDoNotApplyToTheCurrentStateAreIgnored() {
        val machine = LinkStateMachine()

        assertTrue("IDLE 下的心跳超时应被忽略", machine.onEvent(LinkEvent.HeartbeatTimeout).isEmpty())
        assertEquals(LinkState.IDLE, machine.state)

        machine.onEvent(LinkEvent.StartRequested)
        assertTrue(
            "CONNECTING 下不该接受 ClockConverged",
            machine.onEvent(LinkEvent.ClockConverged).isEmpty()
        )
        assertEquals(LinkState.CONNECTING, machine.state)

        assertTrue("重复 StartRequested 应被忽略", machine.onEvent(LinkEvent.StartRequested).isEmpty())
    }

    @Test
    fun successfulHandshakeClearsTheFailureCounter() {
        val machine = LinkStateMachine()
        machine.onEvent(LinkEvent.StartRequested)
        repeat(5) { machine.onEvent(LinkEvent.TransportFailed("x")) }
        assertEquals(5, machine.consecutiveFailures)

        machine.onEvent(LinkEvent.TransportConnected)
        machine.onEvent(LinkEvent.HandshakeCompleted(capabilities()))

        assertEquals(0, machine.consecutiveFailures)
    }
}
