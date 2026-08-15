package com.example.blindassist.probe

import android.app.ActivityManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.os.SystemClock
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * M0 平台能力探针（清单 Q1 / Q4 / Q5 / Q6 / Q7）。
 *
 * 与相机/编码探针一样：只测量与记录，不做通过/失败判定。
 * 取回：adb logcat -d -s M0Platform:I '*:S'
 */
@RunWith(AndroidJUnit4::class)
class PlatformCapabilityProbeTest {

    /** Q1：SoC 识别与计算资源。 */
    @Test
    fun reportSocAndComputeResources() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        Log.i(TAG, "=== Q1 SoC 与计算资源 ===")
        Log.i(TAG, "Q1 model=${Build.MODEL} manufacturer=${Build.MANUFACTURER} device=${Build.DEVICE}")
        Log.i(TAG, "Q1 board=${Build.BOARD} hardware=${Build.HARDWARE} product=${Build.PRODUCT}")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            Log.i(TAG, "Q1 socManufacturer=${Build.SOC_MANUFACTURER} socModel=${Build.SOC_MODEL}")
        } else {
            Log.i(TAG, "Q1 socManufacturer/socModel 需 API 31+，当前 API=${Build.VERSION.SDK_INT}")
        }
        Log.i(
            TAG,
            "Q1 android=${Build.VERSION.RELEASE} api=${Build.VERSION.SDK_INT} " +
                "abis=${Build.SUPPORTED_ABIS.joinToString()}"
        )

        val activityManager = context.getSystemService(ActivityManager::class.java)
        val memoryInfo = ActivityManager.MemoryInfo().also { activityManager.getMemoryInfo(it) }
        Log.i(
            TAG,
            "Q1 cpuCores=${Runtime.getRuntime().availableProcessors()} " +
                "memoryClass=${activityManager.memoryClass}MB " +
                "largeMemoryClass=${activityManager.largeMemoryClass}MB " +
                "totalMem=%.2fGB availMem=%.2fGB lowRamDevice=${activityManager.isLowRamDevice}"
                    .format(memoryInfo.totalMem / 1.0e9, memoryInfo.availMem / 1.0e9)
        )
        Log.i(
            TAG,
            "Q1 基线: 电量=${batteryPercent(context)}% thermalStatus=${thermalStatus(context)}"
        )
        Log.i(
            TAG,
            "Q1 说明: 推理后端（GPU delegate / NNAPI / QNN）的实际可用性不在 M0 范围，" +
                "属 PRD 遗留技术债，M3 用同一模型跑 A/B 才能下结论"
        )
        assertTrue(Build.SUPPORTED_ABIS.isNotEmpty())
    }

    /**
     * Q4 / Q6：厂商 SDK 暴露面探测。
     *
     * 这里探测的是**可发现的表面**，不是完整答案：
     *   1. 系统 feature 列表（厂商常注册 com.xxx.yyy feature）
     *   2. 可 uses-library 的共享库（厂商 SDK 的常见暴露方式）
     *   3. 候选类名反射命中
     *
     * 第 3 项的候选列表是按常见命名习惯**猜**的，命中说明存在，未命中**不能**说明不存在。
     * 镜腿触控与佩戴检测的权威答案只能来自雷鸟官方开发文档；本探测用于在拿到文档前
     * 快速判断"有没有可直接调用的东西"。
     */
    @Test
    fun discoverVendorSdkSurface() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val pm = context.packageManager
        Log.i(TAG, "=== Q4/Q6 厂商 SDK 暴露面 ===")

        val features = pm.systemAvailableFeatures.mapNotNull { it.name }.sorted()
        val vendorFeatures = features.filter { name ->
            VENDOR_KEYWORDS.any { name.contains(it, ignoreCase = true) }
        }
        Log.i(TAG, "Q4/Q6 系统 feature 总数=${features.size} 厂商相关=${vendorFeatures.size}")
        vendorFeatures.forEach { Log.i(TAG, "Q4/Q6 feature: $it") }

        val libraries = runCatching { pm.systemSharedLibraryNames?.toList() }.getOrNull().orEmpty()
        val vendorLibraries = libraries.filter { name ->
            VENDOR_KEYWORDS.any { name.contains(it, ignoreCase = true) }
        }
        Log.i(TAG, "Q4/Q6 共享库总数=${libraries.size} 厂商相关=${vendorLibraries.size}")
        vendorLibraries.forEach { Log.i(TAG, "Q4/Q6 library: $it") }

        var hits = 0
        for (candidate in CANDIDATE_SDK_CLASSES) {
            val found = runCatching { Class.forName(candidate) }.isSuccess
            if (found) {
                hits++
                Log.i(TAG, "Q4/Q6 反射命中: $candidate")
            }
        }
        Log.i(
            TAG,
            "Q4/Q6 反射候选 ${CANDIDATE_SDK_CLASSES.size} 个，命中 $hits 个" +
                if (hits == 0) "（未命中不等于不存在，以官方 SDK 文档为准）" else ""
        )

        val vendorPackages = runCatching {
            pm.getInstalledPackages(0).map { it.packageName }
        }.getOrDefault(emptyList()).filter { name ->
            VENDOR_KEYWORDS.any { name.contains(it, ignoreCase = true) }
        }
        Log.i(TAG, "Q4/Q6 厂商相关包=${vendorPackages.size}")
        vendorPackages.take(40).forEach { Log.i(TAG, "Q4/Q6 package: $it") }
    }

    /** Q5：TTS 引擎、中文语音可用性与合成延迟。 */
    @Test
    fun probeTextToSpeechChineseAvailability() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        Log.i(TAG, "=== Q5 TTS 中文能力 ===")

        val initLatch = CountDownLatch(1)
        var initStatus = TextToSpeech.ERROR
        val tts = TextToSpeech(context) { status ->
            initStatus = status
            initLatch.countDown()
        }

        try {
            if (!initLatch.await(TTS_INIT_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
                Log.i(TAG, "Q5 TTS 初始化超时（${TTS_INIT_TIMEOUT_SECONDS}s）—— 眼镜端可能没有 TTS 服务")
                return
            }
            if (initStatus != TextToSpeech.SUCCESS) {
                Log.i(TAG, "Q5 TTS 初始化失败 status=$initStatus —— 眼镜端播报需要另找方案")
                return
            }

            Log.i(TAG, "Q5 默认引擎=${tts.defaultEngine}")
            tts.engines.forEach { Log.i(TAG, "Q5 已安装引擎: ${it.name} (${it.label})") }

            val availability = tts.isLanguageAvailable(Locale.SIMPLIFIED_CHINESE)
            Log.i(TAG, "Q5 zh-CN 可用性=${describeLanguageAvailability(availability)} (raw=$availability)")

            val chineseVoices = runCatching {
                tts.voices.orEmpty().filter { it.locale.language.lowercase().startsWith("zh") }
            }.getOrDefault(emptyList())
            Log.i(TAG, "Q5 中文 voice 数量=${chineseVoices.size}")
            chineseVoices.take(10).forEach {
                Log.i(
                    TAG,
                    "Q5 voice: ${it.name} locale=${it.locale} quality=${it.quality} " +
                        "latency=${it.latency} networkRequired=${it.isNetworkConnectionRequired}"
                )
            }

            if (availability < TextToSpeech.LANG_AVAILABLE) {
                Log.i(TAG, "Q5 结论: 中文语音数据缺失，眼镜端 TTS 需预装语音包或改用预置音频")
                return
            }
            tts.language = Locale.SIMPLIFIED_CHINESE
            measureSynthesisLatency(tts, context)
        } finally {
            runCatching { tts.stop() }
            runCatching { tts.shutdown() }
        }
    }

    private fun measureSynthesisLatency(tts: TextToSpeech, context: Context) {
        val outputFile = File(context.cacheDir, "m0_tts_probe.wav")
        val samples = mutableListOf<Double>()

        for (attempt in 0 until TTS_SAMPLES) {
            val utteranceId = "m0-probe-$attempt"
            val doneLatch = CountDownLatch(1)
            var failed = false
            tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(id: String?) = Unit

                override fun onDone(id: String?) {
                    if (id == utteranceId) doneLatch.countDown()
                }

                @Deprecated("Required by the abstract base class")
                override fun onError(id: String?) {
                    if (id == utteranceId) {
                        failed = true
                        doneLatch.countDown()
                    }
                }

                override fun onError(id: String?, errorCode: Int) {
                    if (id == utteranceId) {
                        failed = true
                        doneLatch.countDown()
                    }
                }
            })

            val params = Bundle().apply {
                putString(TextToSpeech.Engine.KEY_PARAM_UTTERANCE_ID, utteranceId)
            }
            val startNs = SystemClock.elapsedRealtimeNanos()
            val queued = tts.synthesizeToFile(PROBE_PHRASE, params, outputFile, utteranceId)
            if (queued != TextToSpeech.SUCCESS) {
                Log.i(TAG, "Q5 synthesizeToFile 入队失败 result=$queued")
                break
            }
            if (!doneLatch.await(TTS_SYNTHESIS_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
                Log.i(TAG, "Q5 合成超时（第 $attempt 次）")
                break
            }
            if (failed) {
                Log.i(TAG, "Q5 合成报错（第 $attempt 次）")
                break
            }
            samples.add((SystemClock.elapsedRealtimeNanos() - startNs) / 1_000_000.0)
        }

        if (samples.isEmpty()) {
            Log.i(TAG, "Q5 未取得有效合成样本")
            return
        }
        val sorted = samples.sorted()
        Log.i(
            TAG,
            "Q5 合成延迟: 文本=\"$PROBE_PHRASE\"(${PROBE_PHRASE.length}字) 样本=${sorted.size} " +
                "p50=%.1fms max=%.1fms 输出=%dB 全部=%s".format(
                    sorted[sorted.size / 2],
                    sorted.last(),
                    outputFile.length(),
                    sorted.joinToString { "%.1f".format(it) }
                )
        )
        Log.i(
            TAG,
            "Q5 判定: 该延迟是 synthesizeToFile 的**合成**耗时，不含播放；" +
                "PRD 给眼镜端播报的预算是下发+起播 ≤200ms，需与此对照"
        )
        outputFile.delete()
    }

    /** Q7：后台运行与省电限制。眼镜端采集服务必须能长时间后台存活。 */
    @Test
    fun reportBackgroundExecutionConstraints() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        Log.i(TAG, "=== Q7 后台运行限制 ===")

        val powerManager = context.getSystemService(PowerManager::class.java)
        val ignoringOptimizations = runCatching {
            powerManager.isIgnoringBatteryOptimizations(context.packageName)
        }.getOrDefault(false)
        Log.i(TAG, "Q7 已加入电池优化白名单=$ignoringOptimizations（未加入则后台采集可能被 Doze 掐断）")
        Log.i(TAG, "Q7 处于省电模式=${powerManager.isPowerSaveMode}")

        val activityManager = context.getSystemService(ActivityManager::class.java)
        Log.i(TAG, "Q7 应用被后台限制=${activityManager.isBackgroundRestricted}")

        val permissions = listOf(
            android.Manifest.permission.CAMERA,
            android.Manifest.permission.RECORD_AUDIO,
            android.Manifest.permission.INTERNET
        )
        for (permission in permissions) {
            val granted = context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
            Log.i(TAG, "Q7 权限 $permission granted=$granted")
        }
        Log.i(
            TAG,
            "Q7 说明: 眼镜端采集服务需前台服务 + 白名单；本探测只报当前状态，" +
                "厂商 ROM 的额外限制需在 M1 用真实长跑验证"
        )
    }

    private fun describeLanguageAvailability(code: Int): String = when (code) {
        TextToSpeech.LANG_AVAILABLE -> "LANG_AVAILABLE"
        TextToSpeech.LANG_COUNTRY_AVAILABLE -> "LANG_COUNTRY_AVAILABLE"
        TextToSpeech.LANG_COUNTRY_VAR_AVAILABLE -> "LANG_COUNTRY_VAR_AVAILABLE"
        TextToSpeech.LANG_MISSING_DATA -> "LANG_MISSING_DATA"
        TextToSpeech.LANG_NOT_SUPPORTED -> "LANG_NOT_SUPPORTED"
        else -> "UNKNOWN($code)"
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
        private const val TAG = "M0Platform"
        private const val TTS_INIT_TIMEOUT_SECONDS = 15L
        private const val TTS_SYNTHESIS_TIMEOUT_SECONDS = 15L
        private const val TTS_SAMPLES = 5
        private const val PROBE_PHRASE = "前方一米有障碍，请停下确认"

        private val VENDOR_KEYWORDS = listOf(
            "rayneo", "tcl", "ffalcon", "thunderbird", "xreal", "glass", "xr", "temple", "wearing"
        )

        // 按常见命名习惯猜测的候选入口类；命中即存在，未命中不代表不存在。
        private val CANDIDATE_SDK_CLASSES = listOf(
            "com.rayneo.arsdk.android.core.RayNeoSDK",
            "com.rayneo.arsdk.android.touch.TempleAction",
            "com.rayneo.arsdk.android.MobileState",
            "com.rayneo.xr.sdk.XrManager",
            "com.tcl.xr.sdk.XrManager",
            "com.tcl.xrmanager.XrManager",
            "com.ffalcon.xr.sdk.XrSdk",
            "com.rayneo.ipc.IpcManager"
        )
    }
}
