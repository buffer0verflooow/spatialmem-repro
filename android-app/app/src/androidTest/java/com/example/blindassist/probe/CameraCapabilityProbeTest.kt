package com.example.blindassist.probe

import android.annotation.SuppressLint
import android.Manifest
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import android.util.Size
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.abs
import kotlin.math.atan

/**
 * M0 相机能力探针（清单 Q2 / Q3 / Q8）。
 *
 * 这不是通过/失败测试，而是**测量与记录**：断言只检查"测到了有效数字"，
 * 具体数值由 logcat 取回并写入《X3 Pro 设备能力报告》。因此本文件在任何
 * 能开相机的设备上都应当通过，包括当前的开发手机——手机结果作为对照基线。
 *
 * CAMERA 权限由运行脚本用 `adb shell pm grant` 预先授予（见
 * scripts/m0_instrumented_probe.sh）；未授予时需要开相机的用例会记录原因并跳过，
 * 而不是失败。这样做是为了不引入 androidx.test:rules —— 该依赖不在离线
 * Gradle 缓存里，会破坏 `--offline` 构建。
 *
 * 取回方式：
 *   adb logcat -d -s M0Camera:I '*:S'
 */
@RunWith(AndroidJUnit4::class)
class CameraCapabilityProbeTest {

    /** 相机权限未授予时，需要真的开相机的用例记录原因并跳过。 */
    private fun hasCameraPermission(): Boolean {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val granted = context.checkSelfPermission(Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) {
            Log.i(
                TAG,
                "跳过：未授予 CAMERA 权限。先执行 " +
                    "adb shell pm grant com.example.blindassist android.permission.CAMERA"
            )
        }
        return granted
    }

    /** Q2 + Q8：枚举相机、输出配置，并推导视场角与像素焦距。 */
    @Test
    fun enumerateCamerasAndDeriveIntrinsics() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = context.getSystemService(CameraManager::class.java)
        val ids = manager.cameraIdList

        Log.i(TAG, "=== Q2/Q8 相机枚举: ${ids.size} 个相机 id=${ids.joinToString()} ===")
        assertTrue("设备未暴露任何 Camera2 相机", ids.isNotEmpty())

        for (id in ids) {
            val chars = manager.getCameraCharacteristics(id)
            val facing = when (chars.get(CameraCharacteristics.LENS_FACING)) {
                CameraCharacteristics.LENS_FACING_FRONT -> "FRONT"
                CameraCharacteristics.LENS_FACING_BACK -> "BACK"
                CameraCharacteristics.LENS_FACING_EXTERNAL -> "EXTERNAL"
                else -> "UNKNOWN"
            }
            val level = when (chars.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL)) {
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> "LEGACY"
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_EXTERNAL -> "EXTERNAL"
                else -> "UNKNOWN"
            }
            Log.i(TAG, "--- camera id=$id facing=$facing hardwareLevel=$level ---")

            val fpsRanges = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
            Log.i(TAG, "id=$id AE_TARGET_FPS_RANGES=${fpsRanges?.joinToString() ?: "N/A"}")
            Log.i(
                TAG,
                "id=$id 支持固定 15FPS = ${fpsRanges?.any { it.lower == 15 && it.upper == 15 } ?: false}"
            )

            val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            if (map == null) {
                Log.i(TAG, "id=$id 无 SCALER_STREAM_CONFIGURATION_MAP")
                continue
            }
            for (format in intArrayOf(ImageFormat.YUV_420_888, ImageFormat.JPEG)) {
                val sizes = map.getOutputSizes(format)
                val name = if (format == ImageFormat.YUV_420_888) "YUV_420_888" else "JPEG"
                if (sizes == null || sizes.isEmpty()) {
                    Log.i(TAG, "id=$id format=$name 无输出尺寸")
                    continue
                }
                Log.i(TAG, "id=$id format=$name 输出尺寸(${sizes.size}): ${sizes.joinToString { "${it.width}x${it.height}" }}")
                if (format == ImageFormat.YUV_420_888) {
                    for (size in sizes.sortedBy { it.width.toLong() * it.height }) {
                        val minDurNs = map.getOutputMinFrameDuration(format, size)
                        val maxFps = if (minDurNs > 0) 1_000_000_000.0 / minDurNs else Double.NaN
                        Log.i(
                            TAG,
                            "id=$id YUV ${size.width}x${size.height} minFrameDuration=${minDurNs}ns maxFps=%.1f"
                                .format(maxFps)
                        )
                    }
                }
            }

            logIntrinsics(id, chars)
        }
    }

    /**
     * Q8：从传感器物理尺寸与焦距推导视场角与像素焦距。
     *
     * 这两个数直接决定 GroundPlaneDistanceEstimator 的 `Z ≈ h·fy/(v − cy)`：
     * 当前代码硬编码 vFov=50°、相机高 1.35m（手持手机），换到眼镜上两者都会变
     * （眼镜相机高度约等于眼高 1.55–1.65m），必须用这里的实测值重新标定。
     */
    private fun logIntrinsics(id: String, chars: CameraCharacteristics) {
        val focalLengths = chars.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
        val physical = chars.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
        val activeArray = chars.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
        if (focalLengths == null || focalLengths.isEmpty() || physical == null || activeArray == null) {
            Log.i(TAG, "id=$id 内参推导不可用：焦距或传感器物理尺寸未暴露")
            return
        }

        val sensorW = physical.width
        val sensorH = physical.height
        val arrayW = activeArray.width()
        val arrayH = activeArray.height()
        Log.i(
            TAG,
            "id=$id 传感器物理尺寸=%.3fx%.3fmm activeArray=%dx%dpx 焦距=%s mm"
                .format(sensorW, sensorH, arrayW, arrayH, focalLengths.joinToString())
        )

        for (f in focalLengths) {
            val fullH = 2.0 * Math.toDegrees(atan((sensorW / (2.0 * f))))
            val fullV = 2.0 * Math.toDegrees(atan((sensorH / (2.0 * f))))
            Log.i(TAG, "id=$id f=%.2fmm 全传感器 hFov=%.2f° vFov=%.2f°".format(f, fullH, fullV))

            // 分析流实际用的是 16:9 裁切，有效视场角会小于全传感器视场角。
            for (target in ANALYSIS_SIZES) {
                val (effW, effH) = effectiveSensorMillimeters(sensorW, sensorH, target)
                val hFov = 2.0 * Math.toDegrees(atan(effW / (2.0 * f)))
                val vFov = 2.0 * Math.toDegrees(atan(effH / (2.0 * f)))
                val fx = f * target.width / effW
                val fy = f * target.height / effH
                Log.i(
                    TAG,
                    ("id=%s f=%.2fmm 输出%dx%d -> hFov=%.2f° vFov=%.2f° fx=%.1fpx fy=%.1fpx " +
                        "cx=%.1f cy=%.1f")
                        .format(
                            id, f, target.width, target.height, hFov, vFov, fx, fy,
                            target.width / 2.0, target.height / 2.0
                        )
                )
            }
        }
        Log.i(
            TAG,
            "id=$id 说明: 以上按无额外数字变焦、方形像素、居中裁切假设推导；" +
                "最终标定仍须用 scripts/calibrate_ground_geometry.py 以已知距离拟合"
        )
    }

    /** 输出宽高比与传感器不一致时的居中裁切后有效传感器尺寸（mm）。 */
    private fun effectiveSensorMillimeters(
        sensorW: Float,
        sensorH: Float,
        output: Size
    ): Pair<Double, Double> {
        val sensorAspect = sensorW.toDouble() / sensorH
        val outputAspect = output.width.toDouble() / output.height
        return if (outputAspect > sensorAspect) {
            // 输出更宽 -> 上下裁切
            sensorW.toDouble() to (sensorW.toDouble() / outputAspect)
        } else {
            // 输出更高 -> 左右裁切
            (sensorH.toDouble() * outputAspect) to sensorH.toDouble()
        }
    }

    /** Q2：真的开一次相机，量实际交付帧率与帧间抖动。规格表能力 ≠ 实际吞吐。 */
    @Test
    fun measureDeliveredFrameRateAtAnalysisResolution() {
        if (!hasCameraPermission()) return
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = context.getSystemService(CameraManager::class.java)
        val id = pickPrimaryCameraId(manager)
        if (id == null) {
            Log.i(TAG, "=== Q2 帧率测量：跳过，没有可用的后置/外置相机 ===")
            return
        }

        val chars = manager.getCameraCharacteristics(id)
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val size = map?.getOutputSizes(ImageFormat.YUV_420_888)
            ?.minByOrNull { distanceTo(it, PREFERRED_ANALYSIS_SIZE) }
        if (size == null) {
            Log.i(TAG, "=== Q2 帧率测量：跳过，相机不支持 YUV_420_888 输出 ===")
            return
        }
        val fpsRange = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
            ?.firstOrNull { it.lower == TARGET_FPS && it.upper == TARGET_FPS }
            ?: chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
                ?.minByOrNull { abs(it.upper - TARGET_FPS) }

        Log.i(
            TAG,
            "=== Q2 帧率测量: id=$id size=${size.width}x${size.height} " +
                "请求fpsRange=${fpsRange ?: "默认"} 时长=${MEASURE_SECONDS}s ==="
        )

        val thread = HandlerThread("m0-camera").apply { start() }
        val handler = Handler(thread.looper)
        val executor = Executors.newSingleThreadExecutor()
        val frameCount = AtomicInteger(0)
        val firstFrameNs = AtomicLong(0)
        val lastFrameNs = AtomicLong(0)
        val gaps = java.util.Collections.synchronizedList(mutableListOf<Double>())

        val reader = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 4)
        reader.setOnImageAvailableListener({ r ->
            val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                val now = SystemClock.elapsedRealtimeNanos()
                val previous = lastFrameNs.getAndSet(now)
                if (previous == 0L) {
                    firstFrameNs.set(now)
                } else {
                    gaps.add((now - previous) / 1_000_000.0)
                }
                frameCount.incrementAndGet()
            } finally {
                image.close()
            }
        }, handler)

        var device: CameraDevice? = null
        var session: CameraCaptureSession? = null
        try {
            device = openCamera(manager, id, handler) ?: run {
                Log.i(TAG, "Q2 帧率测量：相机打开失败，跳过")
                return
            }
            session = createSession(device, reader, executor) ?: run {
                Log.i(TAG, "Q2 帧率测量：会话配置失败，跳过")
                return
            }

            val request = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                addTarget(reader.surface)
                fpsRange?.let { set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
            }.build()
            session.setRepeatingRequest(request, null, handler)

            Thread.sleep(MEASURE_SECONDS * 1000L)
            session.stopRepeating()
        } finally {
            runCatching { session?.close() }
            runCatching { device?.close() }
            runCatching { reader.close() }
            executor.shutdownNow()
            thread.quitSafely()
        }

        val frames = frameCount.get()
        val elapsedS = (lastFrameNs.get() - firstFrameNs.get()) / 1_000_000_000.0
        val actualFps = if (elapsedS > 0) (frames - 1) / elapsedS else 0.0
        val sortedGaps = gaps.toList().sorted()
        val p50 = sortedGaps.getOrNull(sortedGaps.size / 2) ?: Double.NaN
        val p95 = sortedGaps.getOrNull((sortedGaps.size * 95 / 100).coerceAtMost(sortedGaps.size - 1))
            ?: Double.NaN
        val maxGap = sortedGaps.lastOrNull() ?: Double.NaN

        Log.i(
            TAG,
            ("Q2 结果: size=%dx%d 帧数=%d 时长=%.2fs 实际FPS=%.2f 帧间隔 p50=%.1fms " +
                "p95=%.1fms max=%.1fms")
                .format(size.width, size.height, frames, elapsedS, actualFps, p50, p95, maxGap)
        )
        Log.i(
            TAG,
            ("Q2 判定: 实际FPS 与请求 %d 的偏差 = %.2f；" +
                "偏差大或 max 帧间隔远大于 p95 说明 HAL 不接受固定帧率请求，需在 M1 调整采集策略")
                .format(TARGET_FPS, abs(actualFps - TARGET_FPS))
        )
        assertTrue("相机未交付任何帧", frames > 0)
    }

    /**
     * Q3：相机取流与姿态传感器并行。
     *
     * 注意边界：这里验证的是 **Camera2 + Android Sensor（3DoF Rotation Vector）** 能否并行，
     * 不是 6DoF SLAM。X3 Pro 的 6DoF 走 Unity/OpenXR 运行时，instrumented 测试触及不到，
     * 必须另做 Unity 侧验证（见清单 Q3 的第二半）。
     */
    @Test
    fun cameraStreamsWhilePoseSensorsAreSampled() {
        if (!hasCameraPermission()) return
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = context.getSystemService(CameraManager::class.java)
        val sensorManager = context.getSystemService(SensorManager::class.java)
        val id = pickPrimaryCameraId(manager)
        if (id == null) {
            Log.i(TAG, "=== Q3 并行测试：跳过，没有可用相机 ===")
            return
        }

        val wanted = listOf(
            Sensor.TYPE_ACCELEROMETER to "accelerometer",
            Sensor.TYPE_GYROSCOPE to "gyroscope",
            Sensor.TYPE_ROTATION_VECTOR to "rotation_vector"
        )
        val counters = wanted.associate { (type, _) -> type to AtomicInteger(0) }
        val listeners = mutableListOf<SensorEventListener>()

        Log.i(TAG, "=== Q3 相机+姿态并行 ${MEASURE_SECONDS}s ===")
        for ((type, name) in wanted) {
            val sensor = sensorManager.getDefaultSensor(type)
            if (sensor == null) {
                Log.i(TAG, "Q3 传感器缺失: $name")
                continue
            }
            val listener = object : SensorEventListener {
                override fun onSensorChanged(event: SensorEvent) {
                    counters[type]?.incrementAndGet()
                }

                override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
            }
            // 20_000us ≈ 50Hz，与 S21SensorSource 的请求周期一致。
            sensorManager.registerListener(listener, sensor, 20_000)
            listeners.add(listener)
        }

        val chars = manager.getCameraCharacteristics(id)
        val size = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?.getOutputSizes(ImageFormat.YUV_420_888)
            ?.minByOrNull { distanceTo(it, PREFERRED_ANALYSIS_SIZE) }
        if (size == null) {
            listeners.forEach { sensorManager.unregisterListener(it) }
            Log.i(TAG, "=== Q3 并行测试：跳过，无 YUV 输出 ===")
            return
        }

        val thread = HandlerThread("m0-camera-parallel").apply { start() }
        val handler = Handler(thread.looper)
        val executor = Executors.newSingleThreadExecutor()
        val frames = AtomicInteger(0)
        val reader = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 4)
        reader.setOnImageAvailableListener({ r ->
            r.acquireLatestImage()?.use { frames.incrementAndGet() }
        }, handler)

        var device: CameraDevice? = null
        var session: CameraCaptureSession? = null
        try {
            device = openCamera(manager, id, handler)
            session = device?.let { createSession(it, reader, executor) }
            if (device != null && session != null) {
                val request = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                    addTarget(reader.surface)
                }.build()
                session.setRepeatingRequest(request, null, handler)
                Thread.sleep(MEASURE_SECONDS * 1000L)
                session.stopRepeating()
            } else {
                Log.i(TAG, "Q3 并行测试：相机打开或会话配置失败")
            }
        } finally {
            runCatching { session?.close() }
            runCatching { device?.close() }
            runCatching { reader.close() }
            executor.shutdownNow()
            thread.quitSafely()
            listeners.forEach { sensorManager.unregisterListener(it) }
        }

        val cameraFps = frames.get().toDouble() / MEASURE_SECONDS
        Log.i(TAG, "Q3 结果: 相机 %.2f FPS（并行采样期间）".format(cameraFps))
        for ((type, name) in wanted) {
            val hz = (counters[type]?.get() ?: 0).toDouble() / MEASURE_SECONDS
            Log.i(TAG, "Q3 结果: $name %.1f Hz".format(hz))
        }
        Log.i(
            TAG,
            "Q3 判定: 若相机 FPS 与单独测量（见 Q2 结果）接近、且传感器达到约 50Hz，" +
                "则 Camera2 与 3DoF 姿态可并行；6DoF 另测"
        )
    }

    @SuppressLint("MissingPermission")
    private fun openCamera(manager: CameraManager, id: String, handler: Handler): CameraDevice? {
        val latch = CountDownLatch(1)
        var opened: CameraDevice? = null
        var failure: String? = null
        manager.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                opened = camera
                latch.countDown()
            }

            override fun onDisconnected(camera: CameraDevice) {
                camera.close()
                failure = "disconnected"
                latch.countDown()
            }

            override fun onError(camera: CameraDevice, error: Int) {
                camera.close()
                failure = "error=$error"
                latch.countDown()
            }
        }, handler)

        if (!latch.await(OPEN_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            Log.i(TAG, "openCamera 超时（${OPEN_TIMEOUT_SECONDS}s）")
            return null
        }
        failure?.let { Log.i(TAG, "openCamera 失败: $it") }
        return opened
    }

    private fun createSession(
        device: CameraDevice,
        reader: ImageReader,
        executor: java.util.concurrent.Executor
    ): CameraCaptureSession? {
        val latch = CountDownLatch(1)
        var configured: CameraCaptureSession? = null
        val callback = object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(session: CameraCaptureSession) {
                configured = session
                latch.countDown()
            }

            override fun onConfigureFailed(session: CameraCaptureSession) {
                latch.countDown()
            }
        }
        device.createCaptureSession(
            SessionConfiguration(
                SessionConfiguration.SESSION_REGULAR,
                listOf(OutputConfiguration(reader.surface)),
                executor,
                callback
            )
        )
        if (!latch.await(OPEN_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            Log.i(TAG, "createCaptureSession 超时（${OPEN_TIMEOUT_SECONDS}s）")
            return null
        }
        return configured
    }

    /** 优先后置，其次外置（眼镜相机可能报 EXTERNAL），最后任意。 */
    private fun pickPrimaryCameraId(manager: CameraManager): String? {
        val ids = manager.cameraIdList
        if (ids.isEmpty()) return null
        for (target in intArrayOf(
            CameraCharacteristics.LENS_FACING_BACK,
            CameraCharacteristics.LENS_FACING_EXTERNAL
        )) {
            ids.firstOrNull {
                manager.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) == target
            }?.let { return it }
        }
        return ids.first()
    }

    private fun distanceTo(size: Size, target: Size): Long {
        val dw = (size.width - target.width).toLong()
        val dh = (size.height - target.height).toLong()
        return dw * dw + dh * dh
    }

    companion object {
        private const val TAG = "M0Camera"
        private const val TARGET_FPS = 15
        private const val MEASURE_SECONDS = 10L
        private const val OPEN_TIMEOUT_SECONDS = 10L
        private val PREFERRED_ANALYSIS_SIZE = Size(640, 360)
        private val ANALYSIS_SIZES = listOf(
            Size(640, 360),
            Size(640, 480),
            Size(1280, 720),
            Size(1920, 1080)
        )
    }
}
