package com.example.blindassist.vision

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.os.SystemClock
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class MlKitTextReaderInstrumentedTest {

    @Test
    fun bundledChineseModelRecognizesLargeLatinText() {
        val bitmap = createTextBitmap()
        try {
            MlKitTextReader(timeoutSeconds = 30).use { reader ->
                val result = reader.recognize(bitmap)
                val compact = result.normalizedText.uppercase().replace(" ", "")

                assertEquals(MlKitTextReader.ENGINE_NAME, result.engine)
                assertTrue("OCR returned no lines: ${result.text}", result.lines.isNotEmpty())
                assertTrue(
                    "Expected BLINDASSIST or 120 in OCR output, got: ${result.text}",
                    compact.contains("BLINDASSIST") || compact.contains("120")
                )
            }
        } finally {
            bitmap.recycle()
        }
    }

    @Test
    fun blankImageDoesNotInventText() {
        val bitmap = Bitmap.createBitmap(640, 480, Bitmap.Config.ARGB_8888).apply {
            eraseColor(Color.WHITE)
        }
        try {
            MlKitTextReader(timeoutSeconds = 30).use { reader ->
                val result = reader.recognize(bitmap)
                assertTrue("Blank image produced text: ${result.text}", result.normalizedText.isBlank())
            }
        } finally {
            bitmap.recycle()
        }
    }

    @Test
    fun warmRecognitionLatencyIsRecorded() {
        val bitmap = createTextBitmap()
        try {
            MlKitTextReader(timeoutSeconds = 30).use { reader ->
                repeat(2) { reader.recognize(bitmap) }
                val samples = List(8) {
                    val startNs = SystemClock.elapsedRealtimeNanos()
                    reader.recognize(bitmap)
                    (SystemClock.elapsedRealtimeNanos() - startNs) / 1_000_000.0
                }.sorted()
                val p50 = samples[samples.size / 2]
                val p95 = samples.last()
                Log.i(
                    TAG,
                    "ML Kit Chinese OCR 1600x600 warm latency p50=%.2fms p95=%.2fms samples=%s"
                        .format(p50, p95, samples.joinToString { "%.2f".format(it) })
                )
                assertTrue("Invalid OCR p50: $p50", p50.isFinite() && p50 > 0.0)
            }
        } finally {
            bitmap.recycle()
        }
    }

    @Test
    fun externalMedicineFixtureReportsCerAndFieldsWhenPresent() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val fixture = File(context.getExternalFilesDir(null), MEDICINE_FIXTURE_PATH)
        assumeTrue("Optional medicine OCR fixture is not installed", fixture.isFile)
        val bitmap = BitmapFactory.decodeFile(fixture.absolutePath)
        assertTrue("Unable to decode ${fixture.absolutePath}", bitmap != null)

        try {
            MlKitTextReader(timeoutSeconds = 30).use { reader ->
                val result = reader.recognize(bitmap!!)
                val cer = OcrEvaluation.characterErrorRate(MEDICINE_REFERENCE, result.text)
                val matchedFields = OcrEvaluation.exactFieldMatches(
                    MEDICINE_FIELDS,
                    result.text
                )
                val decision = OcrReadoutPolicy.evaluate(result)
                Log.i(
                    TAG,
                    (
                        "Medicine OCR cer=%.4f fields=%d/%d matched=%s confidence=%s " +
                            "policy=%s/%s output=%s"
                    ).format(
                            cer,
                            matchedFields.size,
                            MEDICINE_FIELDS.size,
                            matchedFields.joinToString("|"),
                            result.meanConfidence?.let { "%.4f".format(it) } ?: "unknown",
                            decision.status,
                            decision.reason,
                            result.normalizedText.replace('\n', '|')
                        )
                )
                assertTrue("Real medicine fixture produced no text", result.normalizedText.isNotBlank())
                assertTrue(
                    "Expected at least one exact medicine field, got: ${result.text}",
                    matchedFields.isNotEmpty()
                )
            }
        } finally {
            bitmap?.recycle()
        }
    }

    private fun createTextBitmap(): Bitmap =
        Bitmap.createBitmap(1600, 600, Bitmap.Config.ARGB_8888).also { bitmap ->
            Canvas(bitmap).apply {
                drawColor(Color.WHITE)
                val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = Color.BLACK
                    textSize = 150f
                    typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
                }
                drawText("BLINDASSIST", 70f, 220f, paint)
                drawText("120 TABLETS", 70f, 460f, paint)
            }
        }

    companion object {
        private const val TAG = "MlKitOcrBenchmark"
        private const val MEDICINE_FIXTURE_PATH = "ocr/medicine_text_roi.jpg"
        private const val MEDICINE_REFERENCE =
            "Swisse ULTIBOOST LIVER DETOX+ MILK THISTLE + CHOLINE " +
                "LIVER HEALTH SUPPORT HELPS RELIEVE SYMPTOMS OF INDIGESTION " +
                "ABDOMINAL PAIN & BLOATING"
        private val MEDICINE_FIELDS = listOf(
            "Swisse",
            "ULTIBOOST",
            "LIVER DETOX+",
            "MILK THISTLE + CHOLINE",
            "LIVER HEALTH SUPPORT",
            "HELPS RELIEVE SYMPTOMS OF INDIGESTION",
            "ABDOMINAL PAIN & BLOATING",
        )
    }
}
