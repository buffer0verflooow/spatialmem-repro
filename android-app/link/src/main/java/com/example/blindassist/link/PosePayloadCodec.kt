package com.example.blindassist.link

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * POSE (0x04) 通道的采样批编解码（工单 M1-05 §3.2）。
 *
 * 线格式（全部大端）：
 * ```
 * u16 sampleCount
 * 重复 sampleCount 次:
 *   i64 timestampNs
 *   f32 qx  f32 qy  f32 qz  f32 qw
 *   u8  accuracy
 * ```
 *
 * 四元数按 `SensorManager.getQuaternionFromVector()` 的输出顺序 (x,y,z,w)。
 * 时间戳是**传感器域**的值（Android 上 SensorEvent.timestamp 与
 * elapsedRealtimeNanos 同域，直接透传，不在此做任何换算）。
 *
 * 约束（与工单 M1-05 一致）：
 * - 一包必须包含至少一个采样（空批拒绝）；
 * - 长度必须与 sampleCount 完全一致（多/少字节都算格式错误）；
 * - 单包采样数受 [MAX_SAMPLES] 限制：批次按 100ms 间隔、100Hz 采样约 10 个，
 *   上限留足余量，同时保证负载远小于链路的 2 MiB 上限。
 */
object PosePayloadCodec {

    /** 单采样字节数：8(i64) + 4×4(f32) + 1(u8) = 25。 */
    const val SAMPLE_BYTES: Int = 25

    /**
     * 单包采样上限。线格式允许 u16 全量（65535），但实际负载会超出
     * [LinkWire.DEFAULT_MAX_PAYLOAD]（2 MiB），因此在此收紧到 4096
     * （≈135 KiB，批次间隔 100ms 时对应约 6.8 分钟，远超实际需要）。
     */
    const val MAX_SAMPLES: Int = 4096

    /** 一个 rotation vector 采样。 */
    data class PoseSample(
        val timestampNs: Long,
        val qx: Float,
        val qy: Float,
        val qz: Float,
        val qw: Float,
        val accuracy: Int
    )

    fun encode(samples: List<PoseSample>): ByteArray {
        require(samples.isNotEmpty()) { "空采样批不允许（工单 M1-05：空批拒绝）" }
        require(samples.size <= MAX_SAMPLES) {
            "单包采样数超过上限 $MAX_SAMPLES: ${samples.size}"
        }
        val buffer = ByteBuffer.allocate(2 + samples.size * SAMPLE_BYTES)
            .order(ByteOrder.BIG_ENDIAN)
        buffer.putShort(samples.size.toShort())
        for (sample in samples) {
            buffer.putLong(sample.timestampNs)
            buffer.putFloat(sample.qx)
            buffer.putFloat(sample.qy)
            buffer.putFloat(sample.qz)
            buffer.putFloat(sample.qw)
            buffer.put(sample.accuracy.toByte())
        }
        return buffer.array()
    }

    /**
     * 解码一包 POSE 负载。长度与 sampleCount 不符（截断或多余字节）一律抛
     * [IllegalArgumentException]，与 CONTROL 报文"字段不全即报错"的失败语义一致。
     */
    fun decode(payload: ByteArray): List<PoseSample> {
        if (payload.size < 2) {
            throw IllegalArgumentException("POSE 负载过短：至少需要 2 字节的 sampleCount，实际 ${payload.size}")
        }
        val buffer = ByteBuffer.wrap(payload).order(ByteOrder.BIG_ENDIAN)
        val count = buffer.short.toInt() and 0xFFFF
        if (count == 0) {
            throw IllegalArgumentException("空采样批不允许（工单 M1-05：空批拒绝）")
        }
        if (count > MAX_SAMPLES) {
            throw IllegalArgumentException("sampleCount 超过上限 $MAX_SAMPLES: $count")
        }
        val expectedBytes = 2 + count * SAMPLE_BYTES
        if (payload.size != expectedBytes) {
            throw IllegalArgumentException(
                "POSE 负载长度与 sampleCount=$count 不符：应为 $expectedBytes 字节，实际 ${payload.size}"
            )
        }
        return buildList {
            repeat(count) {
                add(
                    PoseSample(
                        timestampNs = buffer.long,
                        qx = buffer.float,
                        qy = buffer.float,
                        qz = buffer.float,
                        qw = buffer.float,
                        accuracy = buffer.get().toInt()
                    )
                )
            }
        }
    }
}
