package com.example.blindassist.link.transport

import com.example.blindassist.link.GlassCapabilities
import com.example.blindassist.link.LinkChannel
import com.example.blindassist.link.SpeakPath
import com.example.blindassist.link.TimestampSource
import com.example.blindassist.link.VideoMode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 握手协商纯函数的 JVM 单测（工单 M1-04 第 4 节 7–8）。 */
class X3SessionNegotiatorTest {

    @Test
    fun noLocalChineseTts_speakPathMustNotBeGlassesLocalTts() {
        // M0 已证实眼镜上没有 TTS 引擎：hasLocalChineseTts=false 时
        // 协商结果不得是 GLASSES_LOCAL_TTS（PRD 决策 D2 兜底）。
        val config = X3SessionNegotiator.negotiate(capabilities(hasLocalChineseTts = false))

        assertNotEquals(SpeakPath.GLASSES_LOCAL_TTS, config.speakPath)
        assertEquals(SpeakPath.GLASSES_PRESET_AUDIO, config.speakPath)
    }

    @Test
    fun negotiatedResolution_isWithinReportedVideoModes() {
        // 640x360 在上报列表里 → 精确选中。
        val modesWithExact = listOf(
            VideoMode(1280, 720, 30),
            VideoMode(640, 360, 30),
            VideoMode(320, 240, 30)
        )
        val exact = X3SessionNegotiator.negotiate(capabilities(modes = modesWithExact))
        assertEquals(640, exact.videoWidth)
        assertEquals(360, exact.videoHeight)
        assertTrue(modesWithExact.any { it.width == exact.videoWidth && it.height == exact.videoHeight })

        // 没有 640x360 → 取曼哈顿距离最近的模式，协商结果仍必须落在上报列表内。
        val modesNear = listOf(VideoMode(1280, 720, 30), VideoMode(320, 240, 30))
        val near = X3SessionNegotiator.negotiate(capabilities(modes = modesNear))
        assertTrue(modesNear.any { it.width == near.videoWidth && it.height == near.videoHeight })
        assertEquals(320, near.videoWidth)
        assertEquals(240, near.videoHeight)
    }

    private fun capabilities(
        hasLocalChineseTts: Boolean = false,
        modes: List<VideoMode> = listOf(VideoMode(640, 360, 30))
    ): GlassCapabilities {
        return GlassCapabilities(
            protocolVersion = 1,
            deviceModel = "ARGF20",
            videoModes = modes,
            hasHardwareAvcEncoder = true,
            hasLocalChineseTts = hasLocalChineseTts,
            hasRotationVector = false,
            hasSixDof = false,
            hasTempleTouch = false,
            hasWearDetection = false,
            sensorTimestampSource = TimestampSource.REALTIME,
            sensorOrientationDegrees = 90
        )
    }
}
