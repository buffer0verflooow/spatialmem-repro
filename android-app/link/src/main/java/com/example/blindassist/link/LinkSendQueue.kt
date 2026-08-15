package com.example.blindassist.link

/**
 * 发送队列：按优先级出队、队满时背压丢弃。
 *
 * 容量按**包数**计。上限的意义是防止链路变慢时视频包把眼镜端有限的堆（192MB）
 * 吃掉；[offer] 只在队列满且来的是低优先级包时丢包，**高优先级通道
 * （[LinkChannel.CONTROL] / [LinkChannel.SPEAK] / [LinkChannel.SPEAK_STATUS]）
 * 永不丢弃** —— 因此极端情况下队列可能短暂超过 [capacity]（控制类包本身很小，
 * 且速率由调用方控制，不会因此 OOM）。
 *
 * 丢弃策略（被丢的包**只可能是 VIDEO**）：
 * 1. 优先丢最老的非关键帧。丢关键帧的代价不是"少一帧"，而是从它到下一个
 *    关键帧之间的所有帧都解不出来；
 * 2. 若队列里只剩关键帧，丢最老的，并计入 [droppedKeyframeCount] —— 这是
 *    画面会明显卡顿的信号，上层需要知道；
 * 3. **所有丢弃都计数**：每丢一个视频包 [droppedVideoPacketCount] 加一；若该包
 *    是关键帧，[droppedKeyframeCount] 同时加一（它是前者的子集）。
 *
 * 非阻塞：队列空时 [poll] 返回 null，不做 `wait()` / `sleep()`，也不用
 * `BlockingQueue` —— 真正的等待由后续工单的发送线程负责，这样才能保证
 * 测试确定、不依赖真实时间。
 *
 * 线程安全：所有读写走同一把锁（[synchronized]），计数读取是 [Volatile]。
 */
class LinkSendQueue(private val capacity: Int) {

    init {
        require(capacity > 0) { "队列容量必须为正: $capacity" }
    }

    private val lock = Any()
    private val packets = ArrayDeque<LinkPacket>()

    /** 丢弃的视频包总数（含关键帧）。 */
    @Volatile
    var droppedVideoPacketCount: Int = 0
        private set

    /** 丢弃的关键帧数，是 [droppedVideoPacketCount] 的子集。 */
    @Volatile
    var droppedKeyframeCount: Int = 0
        private set

    /**
     * 入队一个包。
     *
     * 高优先级包一定入队，永不丢弃；视频包在队列满时挤掉最老的可丢弃视频包后
     * 入队。返回 true 表示该包已入队（当前实现下调用方总是能入队）。
     */
    fun offer(packet: LinkPacket): Boolean {
        synchronized(lock) {
            if (packet.channel.isHighPriority || packets.size < capacity) {
                packets.addLast(packet)
                return true
            }
            val dropped = dropOldestVideo()
            if (dropped != null) {
                droppedVideoPacketCount++
                if (dropped.isKeyframe) {
                    droppedKeyframeCount++
                }
            }
            packets.addLast(packet)
            return true
        }
    }

    /**
     * 取出下一个要发送的包；队列空时返回 null，不阻塞。
     *
     * 高优先级通道的包先出；同优先级内保持 FIFO。
     */
    fun poll(): LinkPacket? {
        synchronized(lock) {
            if (packets.isEmpty()) return null
            val highPriorityIndex = packets.indexOfFirst { it.channel.isHighPriority }
            return if (highPriorityIndex >= 0) {
                packets.removeAt(highPriorityIndex)
            } else {
                packets.removeFirst()
            }
        }
    }

    /** 当前包数（含超出容量的控制包）。 */
    fun size(): Int = synchronized(lock) { packets.size }

    /**
     * 清空队列（断线重连前调用）。上一会话积压的视频帧若不清掉，会让新会话的
     * CODEC_CONFIG 排在旧帧后面迟迟发不出去，手机端因此收不到参数集、整条新
     * 会话的帧都被丢弃。
     */
    fun clear() {
        synchronized(lock) { packets.clear() }
    }

    /**
     * 丢最老的可丢弃视频包：先找最老的非关键帧；找不到（只剩关键帧）时丢最老的
     * 关键帧。**只可能选中 VIDEO**，绝不碰控制类通道的包；队列里一个视频都没有
     * 时返回 null（此时入队方直接入队，队列短暂超容）。
     */
    private fun dropOldestVideo(): LinkPacket? {
        val nonKeyframeIndex = packets.indexOfFirst {
            it.channel == LinkChannel.VIDEO && !it.isKeyframe
        }
        val index = if (nonKeyframeIndex >= 0) {
            nonKeyframeIndex
        } else {
            packets.indexOfFirst { it.channel == LinkChannel.VIDEO }
        }
        if (index < 0) return null
        return packets.removeAt(index)
    }
}
