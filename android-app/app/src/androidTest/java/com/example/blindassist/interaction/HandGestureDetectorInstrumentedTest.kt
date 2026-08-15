package com.example.blindassist.interaction

import android.graphics.Bitmap
import android.graphics.Color
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.Locale
import kotlin.system.measureNanoTime

@RunWith(AndroidJUnit4::class)
class HandGestureDetectorInstrumentedTest {

    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun handLandmarkerAssetIsPackagedUncompressed() {
        // MediaPipe maps the bundle through an asset file descriptor, which only works
        // when the packaging step leaves .task uncompressed.
        context.assets.openFd(HandGestureDetector.MODEL_ASSET).use { descriptor ->
            assertTrue("hand_landmarker.task is empty", descriptor.length > 0)
        }
    }

    @Test
    fun detectorLoadsAndRunsInferenceOnDevice() {
        val detector = HandGestureDetector.create(context)
        assertNotNull("HandGestureDetector.create returned null on device", detector)

        detector!!.use {
            val blank = solidBitmap(640, 480, Color.DKGRAY)
            val results = it.detect(blank)

            // A blank frame must not hallucinate a hand, and any hand that is found has
            // to carry a full MediaPipe skeleton.
            assertTrue("Blank frame produced $results", results.isEmpty())
            results.forEach { hand ->
                assertEquals(
                    HandGestureClassifier.LANDMARK_COUNT,
                    hand.landmarks.size
                )
            }
        }
    }

    @Test
    fun benchmarkSingleHandInferenceLatency() {
        val detector = HandGestureDetector.create(context)
        assertNotNull("HandGestureDetector.create returned null on device", detector)

        detector!!.use {
            val frame = noiseBitmap(640, 480)
            repeat(5) { _ -> it.detect(frame) }

            val samples = LongArray(15) { _ ->
                measureNanoTime { it.detect(frame) }
            }
            val p50Ms = samples.sortedArray()[samples.size / 2] / 1_000_000.0
            val message = "hand_landmarker 640x480 detect p50=%.2f ms".format(Locale.US, p50Ms)
            Log.i(BENCHMARK_TAG, message)
            println("$BENCHMARK_TAG $message")

            assertTrue("Inference did not complete", p50Ms > 0.0)
        }
    }

    private fun solidBitmap(width: Int, height: Int, color: Int): Bitmap =
        Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).apply { eraseColor(color) }

    private fun noiseBitmap(width: Int, height: Int): Bitmap {
        val pixels = IntArray(width * height) { index ->
            val red = index and 0xFF
            val green = (index * 5) and 0xFF
            val blue = (index * 11) and 0xFF
            Color.rgb(red, green, blue)
        }
        return Bitmap.createBitmap(pixels, width, height, Bitmap.Config.ARGB_8888)
    }

    private companion object {
        const val BENCHMARK_TAG = "HandGestureBenchmark"
    }
}
