package com.example.blindassist.link

/**
 * 过期帧丢弃（PRD F6-8）。
 *
 * 用**旧画面**播报障碍比不播报更危险：用户已经走过去了，系统还在说"前方有椅子"。
 * 所以超过阈值的帧必须在进入风险判断前丢掉。
 *
 * 关键设计：时钟对齐**未收敛时也要能工作**。此时无法得到真实采集时刻，
 * 退而用包到达本机的时间算年龄——这个值系统性地**低估**真实年龄
 * （少算了采集到到达之间的传输耗时），因此是个下界。下界超阈值就一定该丢；
 * 下界没超也不能断言不该丢，故结果标记为 [AgeBasis.ARRIVAL_LOWER_BOUND]，
 * 由上层决定要不要更保守。
 */
class StaleFrameGate(
    private val maxAgeNs: Long = DEFAULT_MAX_AGE_NS
) {
    enum class AgeBasis {
        /** 年龄基于已收敛的对齐时钟换算出的真实采集时刻。 */
        SYNCED_CAPTURE,

        /** 对齐未收敛，年龄基于包到达时间，是真实年龄的下界。 */
        ARRIVAL_LOWER_BOUND
    }

    data class Decision(
        val accepted: Boolean,
        val ageNs: Long,
        val basis: AgeBasis
    ) {
        val ageMs: Double get() = ageNs / 1_000_000.0
        val isExact: Boolean get() = basis == AgeBasis.SYNCED_CAPTURE
    }

    var acceptedCount: Long = 0
        private set
    var droppedCount: Long = 0
        private set

    /** 在对齐未收敛的情况下放行的帧数。这个数应随对齐收敛而停止增长。 */
    var acceptedWithoutSyncCount: Long = 0
        private set

    var maxObservedAgeNs: Long = 0
        private set

    /**
     * @param capturePhoneNs 换算到本机时钟域的采集时刻；对齐未收敛时传 null
     * @param arrivalNs      本包到达本机的时刻（本机时钟）
     * @param nowNs          当前时刻（本机时钟）
     */
    fun evaluate(capturePhoneNs: Long?, arrivalNs: Long, nowNs: Long): Decision {
        val basis = if (capturePhoneNs != null) AgeBasis.SYNCED_CAPTURE else AgeBasis.ARRIVAL_LOWER_BOUND
        val reference = capturePhoneNs ?: arrivalNs
        // 负年龄意味着时间戳超前于当前时刻（对齐误差或时钟跳变），按 0 处理而不是当成"很新"。
        val age = (nowNs - reference).coerceAtLeast(0L)
        val accepted = age <= maxAgeNs

        if (age > maxObservedAgeNs) maxObservedAgeNs = age
        if (accepted) {
            acceptedCount++
            if (basis == AgeBasis.ARRIVAL_LOWER_BOUND) acceptedWithoutSyncCount++
        } else {
            droppedCount++
        }
        return Decision(accepted, age, basis)
    }

    fun totalCount(): Long = acceptedCount + droppedCount

    /** 丢弃率，用于会话总结与 F6 验收（目标 ≤ 5%）。 */
    fun dropRate(): Double {
        val total = totalCount()
        return if (total == 0L) 0.0 else droppedCount.toDouble() / total
    }

    fun reset() {
        acceptedCount = 0
        droppedCount = 0
        acceptedWithoutSyncCount = 0
        maxObservedAgeNs = 0
    }

    companion object {
        /** 400 ms，对应 PRD F6-8。 */
        const val DEFAULT_MAX_AGE_NS: Long = 400_000_000L
    }
}

/**
 * 按通道统计序号，得出丢包/乱序/重复。
 *
 * 序号是线格式里的 uint32，长会话会回绕（15 FPS 下约 9 年才绕一圈，
 * 但音频/IMU 通道速率高得多，且重连后序号可能重置），所以必须正确处理回绕，
 * 不能简单比大小。
 */
class SequenceTracker(
    private val reorderWindow: Long = DEFAULT_REORDER_WINDOW
) {
    enum class Observation {
        /** 紧接上一个序号。 */
        IN_ORDER,

        /** 跳号，中间有丢失。 */
        GAP,

        /** 序号回退且落在乱序窗口内。 */
        REORDERED,

        /** 重复收到同一序号。 */
        DUPLICATE,

        /** 本通道的第一个包。 */
        FIRST
    }

    private var lastSequence: Long? = null
    private val recentlySeen = HashSet<Long>()
    private val recentOrder = ArrayDeque<Long>()

    var receivedCount: Long = 0
        private set
    var lostCount: Long = 0
        private set
    var duplicateCount: Long = 0
        private set
    var reorderedCount: Long = 0
        private set

    fun observe(sequence: Long): Observation {
        require(sequence in 0..0xFFFF_FFFFL) { "序号超出 uint32 范围: $sequence" }
        receivedCount++

        if (recentlySeen.contains(sequence)) {
            duplicateCount++
            return Observation.DUPLICATE
        }
        remember(sequence)

        val previous = lastSequence
        if (previous == null) {
            lastSequence = sequence
            return Observation.FIRST
        }

        val forwardDistance = forwardDistance(previous, sequence)
        return when {
            forwardDistance == 1L -> {
                lastSequence = sequence
                Observation.IN_ORDER
            }
            // 前跳且在合理范围内 -> 中间丢了 forwardDistance-1 个。
            forwardDistance in 2..reorderWindow -> {
                lostCount += forwardDistance - 1
                lastSequence = sequence
                Observation.GAP
            }
            // 回退且在乱序窗口内 -> 迟到的包。之前已把它算作丢失，这里抵消回来。
            forwardDistance >= UINT32_SPAN - reorderWindow -> {
                reorderedCount++
                if (lostCount > 0) lostCount--
                Observation.REORDERED
            }
            // 跳得太远：通常是对端重启或序号重置，重新建立基线而不是记一大笔丢包。
            else -> {
                lastSequence = sequence
                Observation.GAP
            }
        }
    }

    private fun remember(sequence: Long) {
        recentlySeen.add(sequence)
        recentOrder.addLast(sequence)
        while (recentOrder.size > reorderWindow) {
            recentlySeen.remove(recentOrder.removeFirst())
        }
    }

    /** previous 到 current 的前向距离，按 uint32 回绕计算。 */
    private fun forwardDistance(previous: Long, current: Long): Long =
        (current - previous + UINT32_SPAN) % UINT32_SPAN

    /** 丢包率 = 丢失 / (丢失 + 收到)。 */
    fun lossRate(): Double {
        val denominator = lostCount + receivedCount
        return if (denominator == 0L) 0.0 else lostCount.toDouble() / denominator
    }

    fun reset() {
        lastSequence = null
        recentlySeen.clear()
        recentOrder.clear()
        receivedCount = 0
        lostCount = 0
        duplicateCount = 0
        reorderedCount = 0
    }

    companion object {
        const val UINT32_SPAN: Long = 0x1_0000_0000L
        const val DEFAULT_REORDER_WINDOW: Long = 64
    }
}
