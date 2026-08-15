package com.example.blindassist.link

/**
 * AUDIO (0x02) 通道的 PCM16 载荷编解码（工单 M1-05 §3.3 / V-01）。
 *
 * 线格式：PCM16 LE，16kHz，单声道，无头；每包 20ms（640 字节），最后一包允许不足。
 * 载荷就是裸 PCM 字节，不附加长度或时间戳字段（采样时刻在 20 字节帧头
 * [LinkPacket.senderTimestampNs] 里，由采集端用 [AudioRecord] 时间戳换算）。
 *
 * 约束（与 [PosePayloadCodec] 的失败语义一致）：
 * - 空包拒绝；
 * - `sampleCount` 必须与字节数完全一致（半帧截断即格式错误，报「长度与 sampleCount 不符」）；
 * - 常规包不得超过 640 字节（16kHz × 20ms × 2B），不足 640 只允许出现在流末。
 */
object AudioPayloadCodec {

    /** 采样率：16kHz（工单 M1-05 固定假设，HELLO_ACK 暂不扩展协商）。 */
    const val SAMPLE_RATE_HZ: Int = 16_000

    /** PCM16 单声道每采样字节数。 */
    const val BYTES_PER_SAMPLE: Int = 2

    /** 20ms 块帧数：16_000 × 0.02 = 320。 */
    const val FRAMES_PER_PACKET: Int = 320

    /** 20ms 块字节数：320 × 2 = 640。 */
    const val PACKET_BYTES: Int = 640

    /**
     * 一帧（一个 PCM16 采样）的时长，纳秒：1e9 / 16000 = 62500。
     * 采集端用 `AudioRecord.getTimestamp` 的帧位置换算首采样时刻时使用。
     */
    const val FRAME_DURATION_NS: Long = 62_500L

    /**
     * 编码一包 AUDIO 负载。`sampleCount` 必须与 `pcm16Bytes` 一一对应
     * （PCM16 每采样 2 字节），空包与超限包一律拒绝。
     */
    fun encode(pcm16Bytes: ByteArray, sampleCount: Int): ByteArray {
        require(pcm16Bytes.isNotEmpty()) { "空音频包不允许（工单 M1-05：空包拒绝）" }
        require(sampleCount > 0) { "sampleCount 必须 > 0: $sampleCount" }
        require(sampleCount <= FRAMES_PER_PACKET) {
            "单包帧数超过上限 $FRAMES_PER_PACKET: $sampleCount"
        }
        val expectedBytes = sampleCount * BYTES_PER_SAMPLE
        require(pcm16Bytes.size == expectedBytes) {
            "音频负载长度与 sampleCount=$sampleCount 不符：应为 $expectedBytes 字节，实际 ${pcm16Bytes.size}"
        }
        return pcm16Bytes
    }

    /**
     * 解码一包 AUDIO 负载：裸 PCM16 原样返回，只做空包与超长校验。
     * 无头格式在解码侧没有「长度字段」，截断/越界由 [encode] 的 sampleCount
     * 对齐检查与帧头上限（[LinkWire.DEFAULT_MAX_PAYLOAD]）兜住。
     */
    fun decode(payload: ByteArray): ByteArray {
        require(payload.isNotEmpty()) { "空音频包不允许（工单 M1-05：空包拒绝）" }
        require(payload.size <= PACKET_BYTES) {
            "音频包超过上限 $PACKET_BYTES 字节: ${payload.size}"
        }
        return payload
    }
}
