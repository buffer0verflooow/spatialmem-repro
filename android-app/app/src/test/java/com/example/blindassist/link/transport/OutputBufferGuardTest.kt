package com.example.blindassist.link.transport

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

/**
 * [OutputBufferGuard] 的 JVM 单测（工单 M1-04 打回 2 第 4 节第 2 条）：
 * `getOutputImage()` 返回 null 时输出缓冲仍被归还；异常路径同样归还且
 * Image 在 `releaseOutputBuffer` **之前**关闭（审查项 2/3）。
 *
 * 生产侧（H264Decoder.onOutputBufferAvailable）把真实的 obtain/close/release
 * 注入这个入口，因此这里验证的正是解码线程实际走的逻辑。
 */
class OutputBufferGuardTest {

    @Test
    fun nullImage_outputBufferStillReleased() {
        var releases = 0
        var closes = 0
        var consumes = 0
        OutputBufferGuard.process<Any>(
            obtainImage = { null },
            closeImage = { closes++ },
            releaseBuffer = { releases++ },
            consumeImage = { consumes++ },
            onFailure = {}
        )
        // getOutputImage() == null 时没有 Image 可关，但输出槽必须归还：
        // 漏一个就少一个槽，几帧后解码器停住（不崩但没画面）。
        assertEquals("getOutputImage() 返回 null 时必须还槽", 1, releases)
        assertEquals(0, closes)
        assertEquals(0, consumes)
    }

    @Test
    fun obtainImageThrows_outputBufferStillReleased() {
        var releases = 0
        var failures = 0
        OutputBufferGuard.process<Any>(
            obtainImage = { throw IllegalStateException("codec 取图失败") },
            closeImage = {},
            releaseBuffer = { releases++ },
            consumeImage = {},
            onFailure = { failures++ }
        )
        // 取图抛异常按 null 处理：只还槽，onFailure 不重复上报。
        assertEquals(1, releases)
        assertEquals(0, failures)
    }

    @Test
    fun consumeThrows_imageClosedBeforeReleaseAndBufferReleased() {
        val order = mutableListOf<String>()
        var failure: Throwable? = null
        OutputBufferGuard.process(
            obtainImage = { Any() },
            closeImage = { order += "close" },
            releaseBuffer = { order += "release" },
            consumeImage = { throw IndexOutOfBoundsException("bad plane") },
            onFailure = { failure = it }
        )
        // 审查项 3：Image 必须在 releaseOutputBuffer 之前关闭；异常路径也必须还槽。
        assertEquals(listOf("close", "release"), order)
        assertNotNull("转换异常必须上报 onFailure", failure)
    }

    @Test
    fun success_imageClosedBeforeRelease() {
        val order = mutableListOf<String>()
        var consumed = 0
        OutputBufferGuard.process(
            obtainImage = { Any() },
            closeImage = { order += "close" },
            releaseBuffer = { order += "release" },
            consumeImage = { consumed++ },
            onFailure = {}
        )
        assertEquals(listOf("close", "release"), order)
        assertEquals(1, consumed)
    }
}
