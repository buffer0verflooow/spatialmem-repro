package com.example.blindassist.link

/**
 * 一次时钟对齐往返的四个时间戳（NTP 口径）。
 *
 * ```
 * 本端(手机) ──PING──▶ 对端(眼镜)
 *   t1=localSendNs        t2=remoteRecvNs
 *                              │ 对端处理
 *   t4=localRecvNs   ◀──PONG── t3=remoteSendNs
 * ```
 */
data class ClockSyncSample(
    val localSendNs: Long,
    val remoteRecvNs: Long,
    val remoteSendNs: Long,
    val localRecvNs: Long
) {
    /** 扣掉对端处理时间的纯往返耗时。 */
    val roundTripNs: Long
        get() = (localRecvNs - localSendNs) - (remoteSendNs - remoteRecvNs)

    /** 对端时钟减本端时钟。remoteNs - offsetNs 即为本端时钟域的值。 */
    val offsetNs: Long
        get() = ((remoteRecvNs - localSendNs) + (remoteSendNs - localRecvNs)) / 2

    /**
     * 本样本给出的偏移误差**硬上界**。
     *
     * 推导：设去程单向延迟 d1、回程 d2，则 d1 + d2 = rtt。测得偏移与真实偏移之差
     * 等于 (d1 − d2)/2，其绝对值在 d1 或 d2 取到 0 时最大，即 rtt/2。
     * 这是不依赖任何分布假设的确定性上界，因此可以直接用来支撑验收结论。
     */
    val uncertaintyNs: Long
        get() = roundTripNs / 2

    /** 时间戳自相矛盾的样本（网络不可能实现）必须丢弃，否则会污染估计。 */
    val isPlausible: Boolean
        get() = localRecvNs >= localSendNs && remoteSendNs >= remoteRecvNs && roundTripNs >= 0
}

/**
 * 估计眼镜与手机两个独立单调时钟之间的偏移。
 *
 * **为什么必须有这个东西**：`VideoFrame.captureTimestampNs` 会一路流到
 * `GuidanceCommand.captureTimestampNs`，再由 `GuidanceEvent.captureToEventLatencyMs`
 * 算端到端延迟。两台设备的 `elapsedRealtimeNanos()` 各自从自己开机起算，
 * 偏移是任意的（可能差几小时）。不做换算，这个延迟数不是「慢了多少」，
 * 而是「两台设备开机时间差」，完全无意义；`TemporalDistanceEstimator` 的
 * 速率计算和 `RiskEngine` 的冷却也会一起错。
 *
 * **选样策略**：取往返耗时最小的样本。理由是误差上界正好是 rtt/2，
 * 所以最小 rtt 的样本给出**最紧的确定性上界**。这比「多样本取平均」更适合
 * 安全相关判断——平均值的误差需要分布假设，而这里的上界不需要。
 *
 * **抗漂移**：晶振漂移约 20 ppm，即每小时可达约 72 ms。所以样本窗口按
 * **时间**淘汰而不只按条数淘汰，否则一条很早的低 rtt 样本会永久压住估计值。
 *
 * 本类不是线程安全的；调用方需自行序列化（实践中所有样本都来自同一条接收线程）。
 */
class ClockSyncEstimator(
    private val maxSamples: Int = DEFAULT_MAX_SAMPLES,
    private val maxSampleAgeNs: Long = DEFAULT_MAX_SAMPLE_AGE_NS,
    private val minSamplesForConvergence: Int = DEFAULT_MIN_SAMPLES,
    private val maxUncertaintyNs: Long = DEFAULT_MAX_UNCERTAINTY_NS
) {
    private val samples = ArrayDeque<ClockSyncSample>()
    private var rejectedCount = 0

    /**
     * 加入一次往返测量。返回 true 表示被采纳。
     *
     * 自相矛盾的样本被拒绝并计入 [rejectedSampleCount]；拒绝率高说明链路
     * 或时钟有更基础的问题，应作为告警而不是静默忽略。
     */
    fun addSample(sample: ClockSyncSample): Boolean {
        if (!sample.isPlausible) {
            rejectedCount++
            return false
        }
        samples.addLast(sample)
        evictStale(sample.localRecvNs)
        while (samples.size > maxSamples) {
            samples.removeFirst()
        }
        return true
    }

    fun addSample(localSendNs: Long, remoteRecvNs: Long, remoteSendNs: Long, localRecvNs: Long): Boolean =
        addSample(ClockSyncSample(localSendNs, remoteRecvNs, remoteSendNs, localRecvNs))

    /** 淘汰太旧的样本，防止漂移被旧样本掩盖。总是保留至少一条。 */
    private fun evictStale(nowNs: Long) {
        while (samples.size > 1 && nowNs - samples.first().localRecvNs > maxSampleAgeNs) {
            samples.removeFirst()
        }
    }

    private fun best(): ClockSyncSample? = samples.minByOrNull { it.roundTripNs }

    fun sampleCount(): Int = samples.size

    fun rejectedSampleCount(): Int = rejectedCount

    /** 对端时钟减本端时钟；无有效样本时为 null。 */
    fun offsetNs(): Long? = best()?.offsetNs

    /** 当前估计的误差硬上界；无有效样本时为 null。 */
    fun uncertaintyNs(): Long? = best()?.uncertaintyNs

    fun bestRoundTripNs(): Long? = best()?.roundTripNs

    /**
     * 对齐是否已收敛到可用于时间戳换算。
     *
     * 未收敛时**不得**调用 [toReceiverNs] 的结果去做安全判断；
     * 应改用包到达时间做保守下界（见 [StaleFrameGate]）。
     */
    fun isConverged(): Boolean {
        val current = best() ?: return false
        return samples.size >= minSamplesForConvergence && current.uncertaintyNs <= maxUncertaintyNs
    }

    /** 把对端时钟域的纳秒值换算到本端时钟域；未收敛或无样本时返回 null。 */
    fun toReceiverNs(remoteNs: Long): Long? {
        if (!isConverged()) return null
        val offset = offsetNs() ?: return null
        return remoteNs - offset
    }

    /**
     * 不检查收敛状态的强制换算，仅用于日志与诊断。
     *
     * 安全路径**不要**用它 —— 那正是「用未对齐的时间戳算出荒谬延迟」的来源。
     */
    fun toReceiverNsUnchecked(remoteNs: Long): Long? {
        val offset = offsetNs() ?: return null
        return remoteNs - offset
    }

    /**
     * 偏移随时间的漂移速率（纳秒/秒），对保留窗口内的样本做最小二乘。
     *
     * 用途是**验证再同步周期够不够**：若实测漂移为 r ns/s，同步周期 T 秒，
     * 则周期末的额外误差约 r·T。样本不足或时间跨度过短时返回 null。
     */
    fun driftNsPerSecond(): Double? {
        if (samples.size < 3) return null
        val origin = samples.first().localRecvNs
        val points = samples.map { (it.localRecvNs - origin) / 1e9 to it.offsetNs.toDouble() }
        val spanSeconds = points.last().first - points.first().first
        if (spanSeconds < MIN_DRIFT_SPAN_SECONDS) return null

        val n = points.size
        val meanX = points.sumOf { it.first } / n
        val meanY = points.sumOf { it.second } / n
        var numerator = 0.0
        var denominator = 0.0
        for ((x, y) in points) {
            val dx = x - meanX
            numerator += dx * (y - meanY)
            denominator += dx * dx
        }
        if (denominator == 0.0) return null
        return numerator / denominator
    }

    /**
     * 重连后必须调用。
     *
     * 任一端睡眠或重启都会让偏移跳变，旧样本会变成有害的错误信息，
     * 不能沿用。
     */
    fun reset() {
        samples.clear()
    }

    companion object {
        const val DEFAULT_MAX_SAMPLES: Int = 16
        const val DEFAULT_MIN_SAMPLES: Int = 3

        /** 120 秒。配合 30 秒同步周期，窗口内常驻约 4 条样本。 */
        const val DEFAULT_MAX_SAMPLE_AGE_NS: Long = 120_000_000_000L

        /**
         * 50 ms。对应 PRD F6-9 的「跨源时间戳误差 ≤ 50ms」。
         * 由 uncertainty = rtt/2 可知，等价于要求最优样本的 rtt ≤ 100 ms。
         */
        const val DEFAULT_MAX_UNCERTAINTY_NS: Long = 50_000_000L

        private const val MIN_DRIFT_SPAN_SECONDS: Double = 10.0
    }
}
