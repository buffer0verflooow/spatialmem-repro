package com.example.blindassist.interaction

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.example.blindassist.source.VideoFrame
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/**
 * Exercises the live wiring used by `CaptureCoordinator`: a camera [VideoFrame] goes in,
 * and a debounced [GestureCommand] comes out.
 *
 * The on-device recognition test proves the model and classifier agree on real hands;
 * this one proves the production path around them — RGBA conversion, rotation, the
 * worker handoff, the rate limiter and the debounce — actually delivers a command.
 * The clock is injected so pacing is deterministic rather than wall-clock dependent.
 */
@RunWith(AndroidJUnit4::class)
class GestureFrameControllerInstrumentedTest {

    private val testContext get() = InstrumentationRegistry.getInstrumentation().context
    private val appContext get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun heldThumbsUpOnCameraFramesEmitsConfirm() {
        assertEquals(GestureCommand.CONFIRM, runFramesUntilCommand("thumb_up.jpg"))
    }

    @Test
    fun heldOpenPalmOnCameraFramesEmitsNothing() {
        // OPEN_PALM used to fire PAUSE. Measured against everyday photographs, an
        // ordinary grip — a cup, a phone — classifies as OPEN_PALM far more often than a
        // deliberate open hand does, so the binding was removed. This drives the real
        // production path with a genuine open-hand photo and asserts it stays silent.
        assertEquals(null, runFramesExpectingNoCommand("left_hands.jpg"))
    }

    @Test
    fun heldPointOnCameraFramesEmitsDescribeTarget() {
        assertEquals(GestureCommand.DESCRIBE_TARGET, runFramesUntilCommand("pointing_up.jpg"))
    }

    @Test
    fun handFreeFramesNeverEmitACommand() {
        // A scene with no hand must stay silent: a spurious PAUSE would silence the
        // obstacle warnings a blind user depends on.
        val fired = AtomicReference<GestureCommand?>(null)
        val clock = AtomicLong(0L)
        val controller = GestureFrameController.create(
            appContext,
            nowNs = { clock.get() },
            minIntervalMs = 0L
        ) { command, _ -> fired.compareAndSet(null, command) }
        assertNotNull(controller)

        controller!!.use {
            val frame = frameFrom(solidBitmap(640, 480))
            repeat(20) { index ->
                clock.set(index * 300L * 1_000_000L)
                it.submit(frame.copy(frameIndex = index.toLong()))
                Thread.sleep(60)
            }
            Thread.sleep(500)
        }

        assertEquals("A hand-free scene produced a command", null, fired.get())
    }

    @Test
    fun rateLimiterDropsFramesInsideTheInterval() {
        val processed = AtomicLong(0)
        val clock = AtomicLong(0L)
        val controller = GestureFrameController.create(
            appContext,
            nowNs = { clock.get() },
            minIntervalMs = 250L
        ) { _, _ -> processed.incrementAndGet() }
        assertNotNull(controller)

        controller!!.use {
            val frame = frameFrom(loadBitmap("thumb_up.jpg"))
            // 30 frames spaced 33ms apart is one second of 30fps camera output.
            repeat(30) { index ->
                clock.set(index * 33L * 1_000_000L)
                it.submit(frame.copy(frameIndex = index.toLong()))
                Thread.sleep(20)
            }
            Thread.sleep(800)
        }

        // At 4Hz over ~1s only a handful of frames are sampled, so the 3-frame debounce
        // cannot possibly have fired more than once.
        assertTrue("commands=${processed.get()}", processed.get() <= 1)
    }

    /** Runs the same production path but asserts the gesture stays inert. */
    private fun runFramesExpectingNoCommand(fileName: String): GestureCommand? {
        val fired = AtomicReference<GestureCommand?>(null)
        val clock = AtomicLong(0L)

        val controller = GestureFrameController.create(
            appContext,
            nowNs = { clock.get() },
            minIntervalMs = 0L
        ) { command, _ -> fired.compareAndSet(null, command) }
        assertNotNull("GestureFrameController.create returned null", controller)

        controller!!.use {
            val frame = frameFrom(loadBitmap(fileName))
            repeat(12) { index ->
                clock.set(index * 300L * 1_000_000L)
                it.submit(frame.copy(frameIndex = index.toLong()))
                Thread.sleep(120)
            }
            Thread.sleep(800)
        }
        return fired.get()
    }

    private fun runFramesUntilCommand(fileName: String): GestureCommand? {
        val latch = CountDownLatch(1)
        val fired = AtomicReference<GestureCommand?>(null)
        val clock = AtomicLong(0L)

        val controller = GestureFrameController.create(
            appContext,
            nowNs = { clock.get() },
            minIntervalMs = 0L
        ) { command, _ ->
            if (fired.compareAndSet(null, command)) latch.countDown()
        }
        assertNotNull("GestureFrameController.create returned null", controller)

        controller!!.use {
            val frame = frameFrom(loadBitmap(fileName))
            repeat(12) { index ->
                if (latch.count == 0L) return@repeat
                clock.set(index * 300L * 1_000_000L)
                it.submit(frame.copy(frameIndex = index.toLong()))
                Thread.sleep(120)
            }
            assertTrue(
                "No command emitted for $fileName within the timeout",
                latch.await(5, TimeUnit.SECONDS)
            )
        }
        return fired.get()
    }

    /** Packs a bitmap the way `S21CameraSource` delivers frames: tight RGBA8888 bytes. */
    private fun frameFrom(bitmap: Bitmap): VideoFrame {
        val pixels = IntArray(bitmap.width * bitmap.height)
        bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
        val bytes = ByteArray(pixels.size * 4)
        var offset = 0
        for (pixel in pixels) {
            bytes[offset++] = ((pixel shr 16) and 0xFF).toByte()
            bytes[offset++] = ((pixel shr 8) and 0xFF).toByte()
            bytes[offset++] = (pixel and 0xFF).toByte()
            bytes[offset++] = ((pixel ushr 24) and 0xFF).toByte()
        }
        return VideoFrame(
            frameIndex = 0L,
            captureTimestampNs = 0L,
            width = bitmap.width,
            height = bitmap.height,
            rotationDegrees = 0,
            rgba8888 = bytes
        )
    }

    private fun loadBitmap(fileName: String): Bitmap {
        val options = BitmapFactory.Options().apply {
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        return testContext.assets.open("hands/$fileName").use { stream ->
            requireNotNull(BitmapFactory.decodeStream(stream, null, options))
        }
    }

    private fun solidBitmap(width: Int, height: Int): Bitmap =
        Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
            .apply { eraseColor(android.graphics.Color.DKGRAY) }
}
