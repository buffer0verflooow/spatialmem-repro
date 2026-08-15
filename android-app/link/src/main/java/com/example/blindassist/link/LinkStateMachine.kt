package com.example.blindassist.link

/**
 * 链路连接状态机（PRD F6-7）。
 *
 * 不做任何 IO：吃 [LinkEvent]，吐 [LinkAction] 让调用方执行。这样重连时序、
 * 退避节奏和播报策略都能在 JVM 单测里验证，不需要真设备。
 *
 * **角色约定**：手机监听，眼镜连接。理由是"会消失的那一端"是眼镜（被摘下、
 * 休眠、走出范围），把重试循环放在会消失的一端，比让服务端反复重新 listen 简单。
 * 本状态机描述的是**发起方（眼镜端）**的视角。
 *
 * **播报克制**：不是每次重试都出声。盲人用户听不了每 250ms 一句"正在重连"。
 * 只在三个时刻播报：曾经连上后掉线、重试超过 [ANNOUNCE_FAILURE_AFTER_ATTEMPTS]
 * 次仍失败、以及恢复。
 */
class LinkStateMachine(
    private val backoffScheduleMs: LongArray = DEFAULT_BACKOFF_MS
) {
    var state: LinkState = LinkState.IDLE
        private set

    /** 连续失败次数；成功握手后清零。 */
    var consecutiveFailures: Int = 0
        private set

    /** 本会话是否曾进入过 STREAMING。决定掉线播报用"断开"还是"连接不上"。 */
    var hasStreamedBefore: Boolean = false
        private set

    private var announcedCurrentOutage = false

    var negotiatedCapabilities: GlassCapabilities? = null
        private set

    fun onEvent(event: LinkEvent): List<LinkAction> = when (event) {
        is LinkEvent.StopRequested -> stop()
        is LinkEvent.StartRequested -> start()
        is LinkEvent.TransportConnected -> transportConnected()
        is LinkEvent.TransportFailed -> failure("传输连接失败: ${event.reason}")
        is LinkEvent.HandshakeCompleted -> handshakeCompleted(event.capabilities)
        is LinkEvent.HandshakeRejected -> handshakeRejected(event.reason)
        is LinkEvent.ClockConverged -> clockConverged()
        is LinkEvent.HeartbeatTimeout -> failure("心跳超时")
        is LinkEvent.PeerDisconnected -> failure("对端断开: ${event.reason}")
    }

    private fun start(): List<LinkAction> {
        if (state != LinkState.IDLE) return emptyList()
        state = LinkState.CONNECTING
        consecutiveFailures = 0
        announcedCurrentOutage = false
        return listOf(LinkAction.ScheduleConnect(delayMs = 0, attempt = 1))
    }

    private fun stop(): List<LinkAction> {
        if (state == LinkState.IDLE) return emptyList()
        state = LinkState.IDLE
        consecutiveFailures = 0
        announcedCurrentOutage = false
        negotiatedCapabilities = null
        return listOf(LinkAction.CloseTransport)
    }

    private fun transportConnected(): List<LinkAction> {
        if (state != LinkState.CONNECTING && state != LinkState.RECONNECTING) return emptyList()
        state = LinkState.HANDSHAKING
        return listOf(LinkAction.SendHello)
    }

    private fun handshakeCompleted(capabilities: GlassCapabilities): List<LinkAction> {
        if (state != LinkState.HANDSHAKING) return emptyList()
        state = LinkState.SYNCING
        negotiatedCapabilities = capabilities
        consecutiveFailures = 0
        // StartClockSync 隐含「先 reset 估计器」：任一端睡眠或重启都会让时钟偏移跳变，
        // 沿用旧样本会得到一个自信但错误的偏移。
        return listOf(LinkAction.StartClockSync)
    }

    private fun handshakeRejected(reason: String): List<LinkAction> {
        if (state != LinkState.HANDSHAKING) return emptyList()
        // 握手被拒（多为协议版本不符）重试也不会变，直接停并告知用户。
        state = LinkState.IDLE
        announcedCurrentOutage = false
        return listOf(
            LinkAction.CloseTransport,
            LinkAction.AnnounceToUser("眼镜连接不兼容，请更新眼镜端应用", critical = true)
        )
    }

    private fun clockConverged(): List<LinkAction> {
        if (state != LinkState.SYNCING) return emptyList()
        val recovering = hasStreamedBefore || announcedCurrentOutage
        state = LinkState.STREAMING
        hasStreamedBefore = true
        val wasAnnounced = announcedCurrentOutage
        announcedCurrentOutage = false
        return if (recovering && wasAnnounced) {
            listOf(LinkAction.AnnounceToUser("眼镜已重新连接", critical = false))
        } else if (recovering) {
            emptyList()
        } else {
            listOf(LinkAction.AnnounceToUser("眼镜已连接", critical = false))
        }
    }

    private fun failure(reason: String): List<LinkAction> {
        if (state == LinkState.IDLE) return emptyList()

        val wasStreaming = state == LinkState.STREAMING
        state = LinkState.RECONNECTING
        consecutiveFailures++

        val actions = mutableListOf<LinkAction>()
        actions.add(LinkAction.CloseTransport)

        // 只在两种情况下出声：刚从可用状态掉下来，或重试很久仍不成功。
        if (!announcedCurrentOutage) {
            if (wasStreaming) {
                announcedCurrentOutage = true
                actions.add(
                    LinkAction.AnnounceToUser("眼镜连接断开，正在重连", critical = true)
                )
            } else if (consecutiveFailures >= ANNOUNCE_FAILURE_AFTER_ATTEMPTS) {
                announcedCurrentOutage = true
                actions.add(
                    LinkAction.AnnounceToUser(
                        if (hasStreamedBefore) "眼镜还没连上，请检查眼镜是否开机"
                        else "连接不上眼镜，请检查眼镜是否开机并靠近手机",
                        critical = true
                    )
                )
            }
        }

        actions.add(
            LinkAction.ScheduleConnect(
                delayMs = backoffDelayMs(consecutiveFailures),
                attempt = consecutiveFailures + 1
            )
        )
        return actions
    }

    /**
     * 第 n 次失败后的退避时长。
     *
     * 默认表 [0, 250, 500, 1000, 2000] 之后恒为 2000，因此失败发生在 t=0 时，
     * 重试点为 0.25s / 0.75s / 1.75s / 3.75s / 5.75s ——**5 秒内有 4 次重试机会**，
     * 满足 F6-7「重连 ≤ 5s」（前提是传输侧确实恢复了）。
     */
    fun backoffDelayMs(failureCount: Int): Long {
        if (failureCount <= 0) return 0
        val index = (failureCount - 1).coerceAtMost(backoffScheduleMs.size - 1)
        return backoffScheduleMs[index]
    }

    /** 从第一次失败起，到第 n 次重试发起为止的累计时间，用于验证 5 秒目标。 */
    fun cumulativeDelayMsForAttempts(attempts: Int): Long {
        var total = 0L
        for (i in 1..attempts) {
            total += backoffDelayMs(i)
        }
        return total
    }

    fun reset() {
        state = LinkState.IDLE
        consecutiveFailures = 0
        hasStreamedBefore = false
        announcedCurrentOutage = false
        negotiatedCapabilities = null
    }

    companion object {
        val DEFAULT_BACKOFF_MS = longArrayOf(250, 500, 1000, 2000)

        /** 从未连上时，连续失败几次才出声。避免开机瞬间就吵。 */
        const val ANNOUNCE_FAILURE_AFTER_ATTEMPTS: Int = 3
    }
}
