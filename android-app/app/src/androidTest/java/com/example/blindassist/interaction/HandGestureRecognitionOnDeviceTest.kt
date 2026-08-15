package com.example.blindassist.interaction

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * End-to-end gesture recognition on device: real photographs decoded on the handset,
 * run through the packaged MediaPipe bundle, and classified by the shipping Kotlin
 * [HandGestureClassifier].
 *
 * This is the test that actually exercises the classifier. The other instrumented test
 * only proves the model loads and does not hallucinate hands on a blank frame, which it
 * can satisfy without ever classifying anything. Every case here asserts that a hand was
 * found first, so a regression that stops detecting hands fails loudly instead of
 * passing vacuously.
 *
 * Photographs are MediaPipe's public reference images, bundled into the test APK under
 * `androidTest/assets/hands/`.
 */
@RunWith(AndroidJUnit4::class)
class HandGestureRecognitionOnDeviceTest {

    private val testContext get() = InstrumentationRegistry.getInstrumentation().context
    private val appContext get() = InstrumentationRegistry.getInstrumentation().targetContext

    private lateinit var detector: HandGestureDetector

    @Before
    fun setUp() {
        // Two hands: several reference photos contain both.
        val created = HandGestureDetector.create(appContext, maxHands = 2)
        assertNotNull("HandGestureDetector.create returned null on device", created)
        detector = created!!
    }

    @After
    fun tearDown() {
        if (this::detector.isInitialized) detector.close()
    }

    @Test
    fun realPhotographsClassifyCorrectlyOnDevice() {
        val failures = mutableListOf<String>()

        EXPECTED.forEach { (fileName, expected) ->
            val results = detector.detect(loadHandImage(fileName))
            val actual = results.map { it.gesture.name }.sorted()

            Log.i(TAG, "$fileName -> $actual (expected $expected)")

            // A photo that stops producing hands must fail, not silently pass.
            if (results.isEmpty()) {
                failures += "$fileName: no hand detected at all"
                return@forEach
            }
            results.forEach { hand ->
                assertEquals(
                    "$fileName: incomplete skeleton",
                    HandGestureClassifier.LANDMARK_COUNT,
                    hand.landmarks.size
                )
                assertEquals(
                    "$fileName: missing world landmarks",
                    HandGestureClassifier.LANDMARK_COUNT,
                    hand.worldLandmarks.size
                )
            }
            if (actual != expected.sorted()) {
                failures += "$fileName: expected ${expected.sorted()} but got $actual"
            }
        }

        assertTrue(
            "On-device gesture mismatches:\n" + failures.joinToString("\n"),
            failures.isEmpty()
        )
    }

    @Test
    fun relaxedOpenHandIsNotReadAsPinchOnDevice() {
        // The false positive that the 0.5 pinch threshold produced: this hand is open,
        // and must not fire SELECT in place of PAUSE.
        val results = detector.detect(loadHandImage("woman_hands.jpg"))

        assertTrue("no hand detected", results.isNotEmpty())
        assertTrue(
            "A relaxed open hand was classified as PINCH: ${results.map { it.gesture }}",
            results.none { it.gesture == HandGesture.PINCH }
        )
        results.forEach { assertEquals(HandGesture.OPEN_PALM, it.gesture) }
    }

    @Test
    fun pointingHandDrivesTheDescribeTargetCommand() {
        val results = detector.detect(loadHandImage("pointing_up.jpg"))
        assertTrue("no hand detected", results.isNotEmpty())

        val stabilizer = GestureStabilizer(requiredFrames = 2)
        val gesture = results.first().gesture
        assertEquals(HandGesture.POINT, gesture)

        // Whole path: gesture -> debounce -> command.
        assertEquals(GestureCommand.NONE, stabilizer.update(gesture))
        assertEquals(GestureCommand.DESCRIBE_TARGET, stabilizer.update(gesture))
    }

    @Test
    fun handednessIsReportedForRealHands() {
        val results = detector.detect(loadHandImage("right_hands.jpg"))

        assertTrue("no hand detected", results.isNotEmpty())
        results.forEach {
            assertTrue(
                "handedness score ${it.handednessScore} out of range",
                it.handednessScore > 0f && it.handednessScore <= 1f
            )
            assertTrue(
                "handedness not resolved",
                it.handedness == Handedness.LEFT || it.handedness == Handedness.RIGHT
            )
        }
    }

    private fun loadHandImage(fileName: String): Bitmap {
        val options = BitmapFactory.Options().apply {
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        return testContext.assets.open("hands/$fileName").use { stream ->
            requireNotNull(BitmapFactory.decodeStream(stream, null, options)) {
                "Could not decode hands/$fileName"
            }
        }
    }

    private companion object {
        const val TAG = "HandGestureOnDevice"

        /**
         * Expected gesture per reference photo, one entry per hand in the frame.
         *
         * `thumbs_down.jpg` is expected to read as THUMBS_UP: the classifier is
         * rotation invariant by design and cannot tell the two apart without a gravity
         * reference. `victory.jpg` maps to UNKNOWN because a victory sign is
         * deliberately bound to no command.
         */
        val EXPECTED = listOf(
            "fist.jpg" to listOf("FIST"),
            "pointing_up.jpg" to listOf("POINT"),
            "pointing_up_rotated.jpg" to listOf("POINT"),
            "thumb_up.jpg" to listOf("THUMBS_UP"),
            "thumbs_down.jpg" to listOf("THUMBS_UP"),
            "victory.jpg" to listOf("UNKNOWN"),
            "left_hands.jpg" to listOf("OPEN_PALM", "OPEN_PALM"),
            "right_hands.jpg" to listOf("OPEN_PALM", "OPEN_PALM"),
            "woman_hands.jpg" to listOf("OPEN_PALM", "OPEN_PALM")
        )
    }
}
