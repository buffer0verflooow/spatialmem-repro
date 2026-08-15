package com.example.blindassist.audio

import android.os.SystemClock
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.security.MessageDigest

@RunWith(AndroidJUnit4::class)
class RokidVadEngineInstrumentedTest {

    @Test
    fun packagedModelHasExpectedSha256AndLoads() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val bytes = context.assets.open(RokidVadEngine.DEFAULT_MODEL_ASSET).use { it.readBytes() }
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(bytes)
            .joinToString("") { "%02x".format(it) }

        assertEquals(MODEL_SHA256, digest)
        RokidVadEngine(context).use { engine ->
            val windows = VadFeatureExtractor().accept(deterministicSamples())
            assertEquals(windows.size, engine.infer(windows).probabilities.size)
        }
    }

    @Test
    fun kotlinFrontendAndAndroidOrtMatchPythonGoldenProbabilities() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val windows = VadFeatureExtractor().accept(deterministicSamples())

        RokidVadEngine(context).use { engine ->
            val result = engine.infer(windows)

            assertArrayEquals(PYTHON_GOLDEN_PROBABILITIES, result.probabilities, 2e-4f)
            assertTrue(result.latencyMs.isFinite() && result.latencyMs > 0.0)
        }
    }

    @Test
    fun stateFeedbackMakesSplitInferenceIdenticalToWholeBatch() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val windows = VadFeatureExtractor().accept(deterministicSamples())

        RokidVadEngine(context).use { engine ->
            val whole = engine.infer(windows).probabilities
            engine.reset()
            val first = engine.infer(windows.take(2)).probabilities
            val second = engine.infer(windows.drop(2)).probabilities
            val split = first + second

            assertArrayEquals(whole, split, 1e-6f)
        }
    }

    @Test
    fun warmBatchLatencyIsRecorded() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val windows = VadFeatureExtractor().accept(deterministicSamples(size = 4_800))

        RokidVadEngine(context).use { engine ->
            repeat(2) {
                engine.reset()
                engine.infer(windows)
            }
            val samples = List(8) {
                engine.reset()
                val startNs = SystemClock.elapsedRealtimeNanos()
                engine.infer(windows)
                (SystemClock.elapsedRealtimeNanos() - startNs) / 1_000_000.0
            }.sorted()
            val p50 = samples[samples.size / 2]
            val p95 = samples.last()
            Log.i(
                TAG,
                "Rokid VAD ${windows.size} windows warm latency p50=%.3fms p95=%.3fms samples=%s"
                    .format(p50, p95, samples.joinToString { "%.3f".format(it) })
            )
            assertTrue("Invalid VAD p50: $p50", p50.isFinite() && p50 > 0.0)
        }
    }

    private fun deterministicSamples(size: Int = 1_600): ShortArray = ShortArray(size) { index ->
        (((index * 7_919 + (index / 37) * 104_729) % 24_001) - 12_000).toShort()
    }

    companion object {
        private const val TAG = "RokidVadBenchmark"
        private const val MODEL_SHA256 =
            "e10b98a0cab1c98e847fbdda14cb3d45a38336d47535a3f63a0fb6c4e0f4cdf4"
        private val PYTHON_GOLDEN_PROBABILITIES = floatArrayOf(
            0.505761385f,
            0.458432436f,
            0.392817020f,
            0.385074914f,
            0.355814457f,
        )
    }
}
