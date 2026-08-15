package com.example.blindassist.vision

import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.Locale
import kotlin.system.measureNanoTime

@RunWith(AndroidJUnit4::class)
class ImageUtilsInstrumentedTest {

    @Test
    fun packagedLibyuvProducesCorrectChwAndRgbBytes() {
        assertTrue("libyuv JNI backend did not load", ImageUtils.isLibyuvAvailable)
        val pixels = intArrayOf(
            0xFFFF0000.toInt(),
            0xFF00FF00.toInt(),
            0xFF0000FF.toInt(),
            0xFF123456.toInt(),
        )

        val floatOutput = directBuffer(pixels.size * 3 * Float.SIZE_BYTES)
        val floatBackend = ImageUtils.argbToNormalizedFloat(
            pixels,
            width = 2,
            height = 2,
            layout = ImageUtils.FloatLayout.CHW,
            output = floatOutput,
        )
        assertEquals(ImageUtils.Backend.LIBYUV, floatBackend)
        assertArrayEquals(
            floatArrayOf(
                1f, 0f, 0f, 0x12 / 255f,
                0f, 1f, 0f, 0x34 / 255f,
                0f, 0f, 1f, 0x56 / 255f,
            ),
            floatOutput.asFloatBuffer().toFloatArray(),
            1e-6f,
        )

        val byteOutput = directBuffer(pixels.size * 3)
        val byteBackend = ImageUtils.argbToRgbBytes(
            pixels,
            width = 2,
            height = 2,
            output = byteOutput,
        )
        assertEquals(ImageUtils.Backend.LIBYUV, byteBackend)
        val actual = ByteArray(pixels.size * 3)
        byteOutput.get(actual)
        assertArrayEquals(
            intArrayOf(
                0xFF, 0x00, 0x00,
                0x00, 0xFF, 0x00,
                0x00, 0x00, 0xFF,
                0x12, 0x34, 0x56,
            ),
            actual.map { it.toInt() and 0xFF }.toIntArray(),
        )
    }

    @Test
    fun benchmark640ChwConversionAgainstPreviousKotlinLoop() {
        assertTrue("libyuv JNI backend did not load", ImageUtils.isLibyuvAvailable)
        val width = 640
        val height = 640
        val pixels = IntArray(width * height) { index ->
            val red = index and 0xFF
            val green = (index * 3) and 0xFF
            val blue = (index * 7) and 0xFF
            (0xFF shl 24) or (red shl 16) or (green shl 8) or blue
        }
        val nativeOutput = directBuffer(pixels.size * 3 * Float.SIZE_BYTES)
        val kotlinOutput = directBuffer(pixels.size * 3 * Float.SIZE_BYTES)

        repeat(10) {
            val backend = ImageUtils.argbToNormalizedFloat(
                pixels,
                width,
                height,
                ImageUtils.FloatLayout.CHW,
                nativeOutput,
            )
            assertEquals(ImageUtils.Backend.LIBYUV, backend)
            previousKotlinChwConversion(pixels, kotlinOutput)
        }

        val nativeNanos = LongArray(25) {
            measureNanoTime {
                ImageUtils.argbToNormalizedFloat(
                    pixels,
                    width,
                    height,
                    ImageUtils.FloatLayout.CHW,
                    nativeOutput,
                )
            }
        }
        val kotlinNanos = LongArray(25) {
            measureNanoTime { previousKotlinChwConversion(pixels, kotlinOutput) }
        }
        val nativeP50Ms = nativeNanos.median() / 1_000_000.0
        val kotlinP50Ms = kotlinNanos.median() / 1_000_000.0
        val message = "640x640 CHW conversion p50: libyuv=%.3f ms kotlin=%.3f ms speedup=%.2fx"
            .format(Locale.US, nativeP50Ms, kotlinP50Ms, kotlinP50Ms / nativeP50Ms)
        Log.i(BENCHMARK_TAG, message)
        println("$BENCHMARK_TAG $message")

        assertTrue(
            "Expected libyuv p50 ($nativeP50Ms ms) to beat Kotlin ($kotlinP50Ms ms)",
            nativeP50Ms < kotlinP50Ms,
        )
    }

    private fun previousKotlinChwConversion(pixels: IntArray, output: ByteBuffer) {
        output.clear()
        for (pixel in pixels) output.putFloat(((pixel shr 16) and 0xFF) / 255f)
        for (pixel in pixels) output.putFloat(((pixel shr 8) and 0xFF) / 255f)
        for (pixel in pixels) output.putFloat((pixel and 0xFF) / 255f)
        output.position(0)
    }

    private fun directBuffer(size: Int): ByteBuffer =
        ByteBuffer.allocateDirect(size).order(ByteOrder.nativeOrder())

    private fun LongArray.median(): Long = sortedArray()[size / 2]

    private fun java.nio.FloatBuffer.toFloatArray(): FloatArray =
        FloatArray(remaining()).also { get(it) }

    companion object {
        private const val BENCHMARK_TAG = "LibyuvBenchmark"
    }
}
