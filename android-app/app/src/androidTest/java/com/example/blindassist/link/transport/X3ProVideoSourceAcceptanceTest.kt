package com.example.blindassist.link.transport

import android.graphics.Bitmap
import android.os.Environment
import androidx.test.platform.app.InstrumentationRegistry
import com.example.blindassist.source.SourceState
import com.example.blindassist.source.VideoFrame
import com.example.blindassist.source.VideoSourceRequest
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 工单 M1-04 第 6.2 节的真机验收夹具（**验收工具，不是产品代码**）。
 *
 * 它把 [X3ProVideoSource] 起在手机上等眼镜端连过来，收满帧后断言尺寸/旋转，
 * 并把其中一帧存成 PNG 供肉眼确认（专抓「颜色错乱但不报错」那类失效）。
 *
 * 对端可以是真眼镜，也可以是 `scripts/m1_mock_glasses.py` 回放的真实录制码流：
 * ```
 * adb -s <phone> forward tcp:47810 tcp:47810
 * adb -s <phone> shell am instrument -w -r \
 *   -e class 'com.example.blindassist.link.transport.X3ProVideoSourceAcceptanceTest' \
 *   com.example.blindassist.test/androidx.test.runner.AndroidJUnitRunner
 * # 另一个终端：
 * python3 scripts/m1_mock_glasses.py --stream /tmp/glass2.h264 --seconds 60
 * ```
 * 没有对端连进来时判为跳过而不是失败——它不该在没有设备的 CI 上变红。
 */
class X3ProVideoSourceAcceptanceTest {

    @Test
    fun receivesDecodableFramesFromGlassLink() {
        val source = X3ProVideoSource()
        val frames = CopyOnWriteArrayList<VideoFrame>()
        val enough = CountDownLatch(TARGET_FRAMES)
        val states = CopyOnWriteArrayList<String>()
        val errors = CopyOnWriteArrayList<Throwable>()

        source.start(
            request = VideoSourceRequest(),
            onFrame = { frame ->
                frames += frame
                enough.countDown()
            },
            onState = { _: SourceState, message: String -> states += message },
            onError = { errors += it }
        )

        val got = try {
            enough.await(WAIT_SECONDS, TimeUnit.SECONDS)
        } finally {
            source.stop()
        }

        val report = StringBuilder()
        report.appendLine("=== X3ProVideoSource 真机验收 ===")
        states.forEach { report.appendLine("  state: $it") }
        errors.forEach { report.appendLine("  error: ${it.javaClass.simpleName}: ${it.message}") }
        report.appendLine("  收到帧数: ${frames.size}")

        if (frames.isEmpty()) {
            report.appendLine("  结论: 跳过 —— 没有任何对端连进来（不是失败）")
            writeReport(report.toString())
            println(report)
            return
        }

        val first = frames.first()
        val last = frames.last()
        val spanNs = last.captureTimestampNs - first.captureTimestampNs
        val fps = if (spanNs > 0) frames.size * 1e9 / spanNs else 0.0
        report.appendLine("  尺寸: ${first.width}x${first.height}  旋转: ${first.rotationDegrees}°")
        report.appendLine("  rgba 长度: ${first.rgba8888.size}（应为 ${first.width * first.height * 4}）")
        report.appendLine("  时间跨度: ${spanNs / 1e9} s  实测帧率: ${"%.2f".format(fps)} FPS")
        report.appendLine("  frameIndex: ${first.frameIndex} → ${last.frameIndex}")

        // 存一帧 PNG 供肉眼确认（约束 2 的失效模式只能靠看）
        val png = savePng(frames[frames.size / 2])
        report.appendLine("  样帧 PNG: $png")

        // 画面统计：全黑/全同色说明解码或转换出了问题
        val stats = channelStats(frames[frames.size / 2])
        report.appendLine("  通道均值 R=${stats[0]} G=${stats[1]} B=${stats[2]}  A=${stats[3]}")
        report.appendLine("  亮度标准差: ${"%.2f".format(stats[4].toDouble())}")

        writeReport(report.toString())
        println(report)

        assertTrue("应收到至少 $TARGET_FRAMES 帧，实际 ${frames.size}", got)
        assertTrue("rgba 长度不对", first.rgba8888.size == first.width * first.height * 4)
        assertTrue("alpha 应恒为 255，实际均值 ${stats[3]}", stats[3] >= 250)
        assertTrue("画面看起来是纯色（标准差 ${stats[4]}），疑似解码或 YUV 转换失败", stats[4] > 5)
    }

    /** 返回 [R均值, G均值, B均值, A均值, 亮度标准差]。 */
    private fun channelStats(frame: VideoFrame): IntArray {
        val d = frame.rgba8888
        var r = 0L; var g = 0L; var b = 0L; var a = 0L
        val n = d.size / 4
        val luma = IntArray(n)
        for (i in 0 until n) {
            val rr = d[i * 4].toInt() and 0xFF
            val gg = d[i * 4 + 1].toInt() and 0xFF
            val bb = d[i * 4 + 2].toInt() and 0xFF
            r += rr; g += gg; b += bb; a += d[i * 4 + 3].toInt() and 0xFF
            luma[i] = (rr * 299 + gg * 587 + bb * 114) / 1000
        }
        val mean = luma.sum().toDouble() / n
        var varSum = 0.0
        for (v in luma) varSum += (v - mean) * (v - mean)
        return intArrayOf(
            (r / n).toInt(), (g / n).toInt(), (b / n).toInt(), (a / n).toInt(),
            Math.sqrt(varSum / n).toInt()
        )
    }

    private fun savePng(frame: VideoFrame): String {
        val bmp = Bitmap.createBitmap(frame.width, frame.height, Bitmap.Config.ARGB_8888)
        bmp.copyPixelsFromBuffer(ByteBuffer.wrap(frame.rgba8888))
        val dir = outputDir()
        val file = File(dir, "x3pro_frame.png")
        file.outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
        bmp.recycle()
        return file.absolutePath
    }

    private fun writeReport(text: String) {
        File(outputDir(), "x3pro_acceptance.txt").writeText(text)
    }

    private fun outputDir(): File {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val dir = ctx.getExternalFilesDir(Environment.DIRECTORY_PICTURES) ?: ctx.filesDir
        dir.mkdirs()
        return dir
    }

    private companion object {
        const val TARGET_FRAMES = 60
        const val WAIT_SECONDS = 45L
    }
}
