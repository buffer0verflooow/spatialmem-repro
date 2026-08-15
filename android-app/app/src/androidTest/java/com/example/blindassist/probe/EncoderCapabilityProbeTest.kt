package com.example.blindassist.probe

import android.content.Context
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.nio.ByteBuffer

/**
 * M0 视频编码能力探针（清单 Q9）。
 *
 * 这是验证 PRD 决策 D1（视觉推理放手机、眼镜只做采集编码传输）的关键测量：
 * 如果**仅编码**这一项在眼镜上就已经吃掉热预算，那么"眼镜端独立跑全套视觉"
 * 更无从谈起，D1 得到加强；反过来如果编码轻松且温升可忽略，说明眼镜端还有
 * 余量，可以考虑把唤醒词以外的轻量模型也放上去。
 *
 * 长跑用法（60 分钟，配合 scripts/m0_endurance.sh）：
 *   adb shell am instrument -w -r \
 *     -e class 'com.example.blindassist.probe.EncoderCapabilityProbeTest#sustainedEncodeUnderConfiguredDuration' \
 *     -e m0DurationMinutes 60 \
 *     com.example.blindassist.test/androidx.test.runner.AndroidJUnitRunner
 *
 * 取回：adb logcat -d -s M0Encoder:I '*:S'
 */
@RunWith(AndroidJUnit4::class)
class EncoderCapabilityProbeTest {

    /** Q9-a：枚举硬件视频编码器及其能力上限。 */
    @Test
    fun enumerateHardwareVideoEncoders() {
        Log.i(TAG, "=== Q9-a 视频编码器枚举 ===")
        val codecs = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos
        var encoderCount = 0

        for (info in codecs) {
            if (!info.isEncoder) continue
            for (type in info.supportedTypes) {
                if (!type.equals(MIME_AVC, ignoreCase = true) &&
                    !type.equals(MIME_HEVC, ignoreCase = true)
                ) {
                    continue
                }
                encoderCount++
                val hardware = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    "hw=${info.isHardwareAccelerated} sw=${info.isSoftwareOnly}"
                } else {
                    "hw=unknown"
                }
                Log.i(TAG, "--- 编码器 ${info.name} type=$type $hardware ---")

                val caps = runCatching { info.getCapabilitiesForType(type) }.getOrNull() ?: continue
                val video = caps.videoCapabilities
                if (video != null) {
                    Log.i(
                        TAG,
                        "${info.name} 宽=${video.supportedWidths} 高=${video.supportedHeights} " +
                            "码率=${video.bitrateRange}bps 帧率=${video.supportedFrameRates}"
                    )
                    for (size in PROBE_SIZES) {
                        val supported = runCatching {
                            video.isSizeSupported(size.first, size.second)
                        }.getOrDefault(false)
                        val fpsRange = if (supported) {
                            runCatching {
                                video.getSupportedFrameRatesFor(size.first, size.second).toString()
                            }.getOrDefault("N/A")
                        } else {
                            "-"
                        }
                        Log.i(
                            TAG,
                            "${info.name} ${size.first}x${size.second} supported=$supported maxFps=$fpsRange"
                        )
                    }
                }
                Log.i(TAG, "${info.name} colorFormats=${caps.colorFormats.joinToString()}")
                caps.encoderCapabilities?.let {
                    Log.i(
                        TAG,
                        "${info.name} 支持码率模式 CQ=${it.isBitrateModeSupported(
                            MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CQ
                        )} VBR=${it.isBitrateModeSupported(
                            MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR
                        )} CBR=${it.isBitrateModeSupported(
                            MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR
                        )}"
                    )
                }
            }
        }
        Log.i(TAG, "Q9-a 结果: 找到 $encoderCount 个 H.264/H.265 编码器条目")
        assertTrue("设备没有任何 H.264/H.265 编码器", encoderCount > 0)
    }

    /** Q9-b：短时编码吞吐与单帧延迟（默认 640x360@15，与分析流一致）。 */
    @Test
    fun measureEncodeThroughputAtAnalysisResolution() {
        Log.i(TAG, "=== Q9-b 短时编码吞吐 ===")
        val result = runEncode(
            width = 640,
            height = 360,
            fps = 15,
            bitrate = 1_500_000,
            durationMs = 20_000L,
            progressIntervalMs = Long.MAX_VALUE
        )
        result?.log("Q9-b") ?: Log.i(TAG, "Q9-b 编码失败，见上方错误")
        assertTrue("编码器未产出任何帧", (result?.frames ?: 0) > 0)
    }

    /**
     * Q9-c：长跑编码，测温升与掉电。
     *
     * 时长由 instrumentation 参数 `m0DurationMinutes` 控制，缺省 2 分钟以便日常回归；
     * 正式的 D1 判定跑 60 分钟，由 scripts/m0_endurance.sh 传参。
     */
    @Test
    fun sustainedEncodeUnderConfiguredDuration() {
        val minutes = InstrumentationRegistry.getArguments()
            .getString(ARG_DURATION_MINUTES)
            ?.toLongOrNull()
            ?: DEFAULT_SUSTAINED_MINUTES
        Log.i(TAG, "=== Q9-c 长跑编码 ${minutes} 分钟（参数 $ARG_DURATION_MINUTES）===")

        // 未佩戴且断电时设备会反复 suspend：实测 3 分钟编码被拉长到 401.4s、
        // 实际 6.23 FPS，而单帧提交延迟 p50 10.41ms 与唤醒时几乎一致——说明是
        // 冻结而非降频。不持锁的话这里测到的是休眠调度策略，不是编码负载，
        // 对 D1 没有解释力。超时设为时长的两倍并留 1 分钟余量，避免测试异常退出时锁不释放。
        val powerManager = InstrumentationRegistry.getInstrumentation()
            .targetContext
            .getSystemService(Context.POWER_SERVICE) as PowerManager
        val wakeLock = powerManager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "blindassist:m0-sustained-encode"
        )
        wakeLock.acquire(minutes * 2 * 60_000L + 60_000L)
        Log.i(TAG, "Q9-c 已持 PARTIAL_WAKE_LOCK（held=${wakeLock.isHeld}）")

        val result = try {
            runEncode(
                width = 640,
                height = 360,
                fps = 15,
                bitrate = 1_500_000,
                durationMs = minutes * 60_000L,
                progressIntervalMs = PROGRESS_INTERVAL_MS
            )
        } finally {
            if (wakeLock.isHeld) wakeLock.release()
            Log.i(TAG, "Q9-c 已释放 PARTIAL_WAKE_LOCK")
        }
        result?.log("Q9-c") ?: Log.i(TAG, "Q9-c 编码失败")
        Log.i(
            TAG,
            "Q9-c 判定: 对照 Q9-b 的单帧延迟；若长跑后 p95 显著抬升或 thermalStatus > 0，" +
                "说明仅编码就已进入热受限区间，眼镜端不应再叠加视觉推理（D1 成立）"
        )
        assertTrue("长跑编码未产出任何帧", (result?.frames ?: 0) > 0)
    }

    private class EncodeResult(
        val width: Int,
        val height: Int,
        val frames: Int,
        val elapsedMs: Double,
        val outputBytes: Long,
        val latenciesMs: List<Double>,
        val startBattery: Int,
        val endBattery: Int,
        val startThermal: Int,
        val endThermal: Int,
        val codecName: String
    ) {
        fun log(prefix: String) {
            val sorted = latenciesMs.sorted()
            val p50 = sorted.getOrNull(sorted.size / 2) ?: Double.NaN
            val p95 = sorted.getOrNull((sorted.size * 95 / 100).coerceAtMost(sorted.size - 1))
                ?: Double.NaN
            val fps = if (elapsedMs > 0) frames * 1000.0 / elapsedMs else 0.0
            val kbps = if (elapsedMs > 0) outputBytes * 8.0 / elapsedMs else 0.0
            Log.i(
                TAG,
                ("%s 结果: codec=%s %dx%d 帧数=%d 时长=%.1fs 实际FPS=%.2f 码流=%.0fkbps " +
                    "单帧提交延迟 p50=%.2fms p95=%.2fms")
                    .format(prefix, codecName, width, height, frames, elapsedMs / 1000.0, fps, kbps, p50, p95)
            )
            Log.i(
                TAG,
                "%s 资源: 电量 %d%% -> %d%%（掉 %d）thermalStatus %d -> %d"
                    .format(prefix, startBattery, endBattery, startBattery - endBattery, startThermal, endThermal)
            )
        }
    }

    private fun runEncode(
        width: Int,
        height: Int,
        fps: Int,
        bitrate: Int,
        durationMs: Long,
        progressIntervalMs: Long
    ): EncodeResult? {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val format = MediaFormat.createVideoFormat(MIME_AVC, width, height).apply {
            setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible
            )
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
        }

        val codec = runCatching { MediaCodec.createEncoderByType(MIME_AVC) }.getOrElse {
            Log.i(TAG, "创建 H.264 编码器失败: $it")
            return null
        }
        val codecName = runCatching { codec.name }.getOrDefault("unknown")

        val startBattery = batteryPercent(context)
        val startThermal = thermalStatus(context)
        val latencies = mutableListOf<Double>()
        var frames = 0
        var outputBytes = 0L
        val bufferInfo = MediaCodec.BufferInfo()
        val frameIntervalUs = 1_000_000L / fps
        val startNs = SystemClock.elapsedRealtimeNanos()
        var lastProgressNs = startNs

        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()

            var presentationUs = 0L
            while ((SystemClock.elapsedRealtimeNanos() - startNs) / 1_000_000 < durationMs) {
                val inputIndex = codec.dequeueInputBuffer(DEQUEUE_TIMEOUT_US)
                if (inputIndex >= 0) {
                    val submitNs = SystemClock.elapsedRealtimeNanos()
                    val image = codec.getInputImage(inputIndex)
                    if (image != null) {
                        fillSyntheticFrame(image, frames)
                    } else {
                        // 极少数编码器不支持 Image 视图，退回裸 ByteBuffer 填充。
                        codec.getInputBuffer(inputIndex)?.let { fillSyntheticBuffer(it, width, height, frames) }
                    }
                    codec.queueInputBuffer(
                        inputIndex,
                        0,
                        width * height * 3 / 2,
                        presentationUs,
                        0
                    )
                    presentationUs += frameIntervalUs
                    latencies.add((SystemClock.elapsedRealtimeNanos() - submitNs) / 1_000_000.0)
                    frames++
                }

                var outputIndex = codec.dequeueOutputBuffer(bufferInfo, 0)
                while (outputIndex >= 0) {
                    outputBytes += bufferInfo.size
                    codec.releaseOutputBuffer(outputIndex, false)
                    outputIndex = codec.dequeueOutputBuffer(bufferInfo, 0)
                }

                val nowNs = SystemClock.elapsedRealtimeNanos()
                if (nowNs - lastProgressNs >= progressIntervalMs * 1_000_000) {
                    lastProgressNs = nowNs
                    Log.i(
                        TAG,
                        "进度: t=%.1fmin frames=%d 电量=%d%% thermalStatus=%d 累计码流=%.1fMB"
                            .format(
                                (nowNs - startNs) / 60_000_000_000.0,
                                frames,
                                batteryPercent(context),
                                thermalStatus(context),
                                outputBytes / 1_048_576.0
                            )
                    )
                }

                // 按目标帧率节流，避免测成"编码器最大吞吐"而不是"15FPS 下的负载"。
                val expectedElapsedMs = frames * 1000L / fps
                val actualElapsedMs = (SystemClock.elapsedRealtimeNanos() - startNs) / 1_000_000
                val sleepMs = expectedElapsedMs - actualElapsedMs
                if (sleepMs > 1) Thread.sleep(minOf(sleepMs, 100L))
            }
        } catch (t: Throwable) {
            Log.i(TAG, "编码过程异常: $t")
            return null
        } finally {
            runCatching { codec.stop() }
            runCatching { codec.release() }
        }

        val elapsedMs = (SystemClock.elapsedRealtimeNanos() - startNs) / 1_000_000.0
        return EncodeResult(
            width = width,
            height = height,
            frames = frames,
            elapsedMs = elapsedMs,
            outputBytes = outputBytes,
            latenciesMs = latencies,
            startBattery = startBattery,
            endBattery = batteryPercent(context),
            startThermal = startThermal,
            endThermal = thermalStatus(context),
            codecName = codecName
        )
    }

    /**
     * 生成随帧变化的合成画面。
     *
     * 不能用纯色或静止图：那样帧间残差近乎为零，编码器几乎不干活，
     * 测出来的功耗和延迟会系统性偏低，无法代表真实相机输入。
     */
    private fun fillSyntheticFrame(image: android.media.Image, frameIndex: Int) {
        val width = image.width
        val height = image.height
        val planes = image.planes

        val yPlane = planes[0]
        val yBuffer = yPlane.buffer
        val yRowStride = yPlane.rowStride
        val yPixelStride = yPlane.pixelStride
        val row = ByteArray(yRowStride)
        for (y in 0 until height) {
            for (x in 0 until width) {
                val value = ((x * 3 + y * 5 + frameIndex * 11) and 0xFF)
                row[x * yPixelStride] = value.toByte()
            }
            yBuffer.position(y * yRowStride)
            yBuffer.put(row, 0, minOf(yRowStride, yBuffer.remaining()))
        }

        for (planeIndex in 1..2) {
            val plane = planes[planeIndex]
            val buffer = plane.buffer
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val chromaRow = ByteArray(rowStride)
            for (y in 0 until height / 2) {
                for (x in 0 until width / 2) {
                    val value = ((x * 7 + y * 3 + frameIndex * 5) and 0xFF)
                    chromaRow[x * pixelStride] = value.toByte()
                }
                buffer.position(y * rowStride)
                buffer.put(chromaRow, 0, minOf(rowStride, buffer.remaining()))
            }
        }
    }

    private fun fillSyntheticBuffer(buffer: ByteBuffer, width: Int, height: Int, frameIndex: Int) {
        buffer.clear()
        val ySize = width * height
        val total = minOf(buffer.capacity(), ySize * 3 / 2)
        val bytes = ByteArray(total)
        for (i in 0 until total) {
            bytes[i] = ((i * 3 + frameIndex * 11) and 0xFF).toByte()
        }
        buffer.put(bytes)
        buffer.position(0)
    }

    private fun batteryPercent(context: Context): Int =
        runCatching {
            context.getSystemService(BatteryManager::class.java)
                .getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        }.getOrDefault(-1)

    private fun thermalStatus(context: Context): Int =
        runCatching {
            context.getSystemService(PowerManager::class.java).currentThermalStatus
        }.getOrDefault(-1)

    companion object {
        private const val TAG = "M0Encoder"
        private const val MIME_AVC = MediaFormat.MIMETYPE_VIDEO_AVC
        private const val MIME_HEVC = MediaFormat.MIMETYPE_VIDEO_HEVC
        private const val DEQUEUE_TIMEOUT_US = 10_000L
        private const val PROGRESS_INTERVAL_MS = 60_000L
        private const val DEFAULT_SUSTAINED_MINUTES = 2L
        private const val ARG_DURATION_MINUTES = "m0DurationMinutes"
        private val PROBE_SIZES = listOf(
            640 to 360,
            640 to 480,
            1280 to 720,
            1920 to 1080
        )
    }
}
