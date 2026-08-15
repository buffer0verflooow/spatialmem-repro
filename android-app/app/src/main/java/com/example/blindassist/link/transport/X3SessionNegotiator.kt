package com.example.blindassist.link.transport

import com.example.blindassist.link.GlassCapabilities
import com.example.blindassist.link.LinkChannel
import com.example.blindassist.link.SessionConfig
import com.example.blindassist.link.SpeakPath
import com.example.blindassist.link.VideoMode
import kotlin.math.abs

/**
 * 握手协商：`GlassCapabilities` → [SessionConfig] 的纯函数，可 JVM 单测。
 *
 * 口径与 `scripts/m1_mock_phone.py` 的 `encode_hello_ack(640, 360, 15, 1_200_000,
 * [0x01, 0x10, 0x11, 0x12], 1, 1000, 30000)` 完全一致：
 * - 分辨率：优先精确 640×360；不在上报的 videoModes 里时取最接近的模式
 *   （协商结果**必须**落在眼镜上报的 videoModes 内）；
 * - 帧率：min(模式 maxFps, 15)；
 * - 码率 1_200_000 bps；启用 VIDEO/CONTROL/SPEAK/SPEAK_STATUS；
 * - speakPath：眼镜无本地 TTS（`hasLocalChineseTts=false`，M0 已证实）时
 *   走 GLASSES_PRESET_AUDIO，不得选 GLASSES_LOCAL_TTS；
 * - heartbeatIntervalMs=1000：手机端周期 PING 间隔（约束 1，保活 + 时钟对齐）；
 * - clockSyncIntervalMs=30000：协商的时钟对齐周期。
 */
object X3SessionNegotiator {

    const val DEFAULT_VIDEO_WIDTH = 640
    const val DEFAULT_VIDEO_HEIGHT = 360
    const val DEFAULT_VIDEO_FPS = 15
    /** 高画质录制档（设置页开关）：720p@10、1.8Mbps，用于离线会话录制/评测。 */
    const val HIGH_QUALITY_VIDEO_WIDTH = 1280
    const val HIGH_QUALITY_VIDEO_HEIGHT = 720
    const val HIGH_QUALITY_VIDEO_FPS = 10
    const val HIGH_QUALITY_VIDEO_BITRATE_BPS = 1_800_000
    /**
     * 与眼镜端实测稳定的码率保持一致（GlassLinkService.VIDEO_BIT_RATE=600kbps）：
     * 眼镜 Wi-Fi 链路吞吐约 1.5–2 Mbps，1.2Mbps 会把链路打满导致十几秒刷新。
     * 高画质录制走 m1_record.py --bitrate 显式覆盖，不影响实时链路。
     */
    const val DEFAULT_VIDEO_BITRATE_BPS = 600_000
    const val DEFAULT_HEARTBEAT_INTERVAL_MS = 1_000L
    const val DEFAULT_CLOCK_SYNC_INTERVAL_MS = 30_000L

    fun negotiate(capabilities: GlassCapabilities, highQuality: Boolean = false): SessionConfig {
        val mode = if (highQuality) {
            selectHighQualityMode(capabilities.videoModes)
        } else {
            selectVideoMode(capabilities.videoModes)
        }
        val fpsCap = if (highQuality) HIGH_QUALITY_VIDEO_FPS else DEFAULT_VIDEO_FPS
        val fps = if (mode.maxFps > 0) {
            mode.maxFps.coerceAtMost(fpsCap)
        } else {
            fpsCap
        }
        val speakPath = if (capabilities.hasLocalChineseTts) {
            SpeakPath.GLASSES_LOCAL_TTS
        } else {
            SpeakPath.GLASSES_PRESET_AUDIO
        }
        return SessionConfig(
            videoWidth = mode.width,
            videoHeight = mode.height,
            videoFps = fps,
            videoBitrateBps = if (highQuality) {
                HIGH_QUALITY_VIDEO_BITRATE_BPS
            } else {
                DEFAULT_VIDEO_BITRATE_BPS
            },
            enabledChannels = setOf(
                LinkChannel.VIDEO,
                LinkChannel.CONTROL,
                LinkChannel.SPEAK,
                LinkChannel.SPEAK_STATUS
            ),
            speakPath = speakPath,
            heartbeatIntervalMs = DEFAULT_HEARTBEAT_INTERVAL_MS,
            clockSyncIntervalMs = DEFAULT_CLOCK_SYNC_INTERVAL_MS
        )
    }

    /**
     * 优先精确 640×360；否则取曼哈顿距离最近的已上报模式；videoModes 为空时
     * 回退默认 640×360（并如实记日志——此时协商结果不在上报列表内）。
     */
    fun selectVideoMode(modes: List<VideoMode>): VideoMode {
        modes.firstOrNull {
            it.width == DEFAULT_VIDEO_WIDTH && it.height == DEFAULT_VIDEO_HEIGHT
        }?.let { return it }
        return modes.minByOrNull {
            abs(it.width - DEFAULT_VIDEO_WIDTH) + abs(it.height - DEFAULT_VIDEO_HEIGHT)
        } ?: VideoMode(DEFAULT_VIDEO_WIDTH, DEFAULT_VIDEO_HEIGHT, DEFAULT_VIDEO_FPS)
    }

    /** 高画质录制：优先精确 720p；否则取最接近的 16:9 模式（协商结果必须落在上报列表内）。 */
    fun selectHighQualityMode(modes: List<VideoMode>): VideoMode {
        modes.firstOrNull {
            it.width == HIGH_QUALITY_VIDEO_WIDTH && it.height == HIGH_QUALITY_VIDEO_HEIGHT
        }?.let { return it }
        return modes.filter { it.width * 9L == it.height * 16L }
            .minByOrNull {
                abs(it.width - HIGH_QUALITY_VIDEO_WIDTH) +
                    abs(it.height - HIGH_QUALITY_VIDEO_HEIGHT)
            }
            ?: VideoMode(HIGH_QUALITY_VIDEO_WIDTH, HIGH_QUALITY_VIDEO_HEIGHT, HIGH_QUALITY_VIDEO_FPS)
    }
}
