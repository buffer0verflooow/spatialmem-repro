package com.example.blindassist.link.transport

import android.graphics.Rect
import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 解码器输出的一帧：已是 RGBA8888。
 *
 * 宽高取 **`Image.cropRect` 的可见区域**（如 640×360），不是含 16 对齐补齐的
 * 解码器分配尺寸（640×368）。下游的走廊判断/地面几何都建立在这个比例上。
 */
data class DecodedVideoFrame(
    val width: Int,
    val height: Int,
    /** 解码器回显的输入 PTS（微秒），即眼镜端发送时戳 ÷ 1000，仍处于眼镜时钟域。 */
    val presentationTimeUs: Long,
    val rgba8888: ByteArray
)

/**
 * H.264 解码器：`MediaCodec`（video/avc，异步回调模式，**ByteBuffer 输出模式**）
 * → `getOutputImage()`（YUV_420_888）→ [YuvToRgba] → [DecodedVideoFrame]。
 *
 * CSD 来自对端每次握手后重发的 `CODEC_CONFIG` 包（约束 6：**每次连接新建解码器**，
 * 用当次收到的 CSD 配置，绝不跨连接缓存/复用）。
 *
 * 异步回调模式的两条纪律（工单 M1-03 在编码侧踩过同类坑）：
 * 1. `setCallback()` 必须**先于** `configure()`；
 * 2. 输入侧用 `onInputBufferAvailable` 喂数据（async 模式下不应调用
 *    `dequeueInputBuffer`）：本类维护一个待喂队列 + 空闲输入槽，链路线程只入队，
 *    解码线程（HandlerThread）统一喂。
 *
 * **ByteBuffer 模式**（工单 M1-04 打回 2 第 3.1 节，硬约束）：
 * - `configure(format, null, null, 0)` 不绑 surface，并在 configure 之前设
 *   `KEY_COLOR_FORMAT = COLOR_FormatYUV420Flexible`。surface 模式下厂商（高通）给的是
 *   UBWC 压缩缓冲（`HAL_PIXEL_FORMAT_YCbCr_420_SP_VENUS_UBWC`），`ImageReader`
 *   的 `getPlanes()` 拿不到可映射平面，native 侧指针为 null，CheckJNI 直接 SIGABRT；
 *   ByteBuffer 模式下同一厂商输出线性 Venus NV12（0x7fa30c04），可正常读平面。
 * - 输出侧在 `onOutputBufferAvailable` 里用 `codec.getOutputImage(index)` 取帧：
 *   可能返回 null（编解码器不支持 flexible、或该缓冲是配置数据），必须判空并
 *   把输出槽还回去，漏一个就少一个槽，几帧后解码器停住（不崩但没画面）。
 *   Image 用 try/finally 保证在任何分支（判空、异常、正常）都先关闭再
 *   `releaseOutputBuffer(index, false)`（render 参数恒为 false，没有 surface 可渲染）。
 * - 没有预绑定的 surface，分辨率变化由 `getOutputImage()` 每帧自带的 cropRect /
 *   stride 自然承载，**不需要**重建解码器；`onOutputFormatChanged` 只留日志。
 *
 * 尺寸纪律（工单 M1-04 打回 1 的成果，保留）：
 * 1. 可见区域一律取 `Image.cropRect`，输出宽高用它，不用 `image.width/height`
 *    （后者含 16 对齐补齐）；
 * 2. 逐行取样起点 `cropRect.top * rowStride + cropRect.left * pixelStride`
 *    （色度平面按 4:2:0 减半）；
 * 3. 前置校验 [YuvToRgba.validateFrame] 保留：失效表现为「丢帧 + 日志」。
 *
 * U/V 平面（工单 M1-04 打回 2 第 3.3 节）：三个平面**各自独立拷贝、各自定址**。
 * 半平面（NV12/NV21）下 `getOutputImage()` 给的 plane[1]/plane[2] 是两个
 * `position()==0` 的独立 slice()，共享基址推断（position 之差）会让 U/V 读到
 * 同一份数据——不崩、帧数正常、PNG 也出得来，只是颜色静默错掉。
 */
class H264Decoder(
    /** 本次连接收到的 CODEC_CONFIG 负载（Annex-B SPS+PPS），作为 csd-0。 */
    private val codecConfig: ByteArray,
    /** 协商尺寸（HELLO_ACK），仅用于 configure 的 width/height hint，可见区域由 cropRect 界定。 */
    private val widthHint: Int,
    private val heightHint: Int,
    private val onFrame: (DecodedVideoFrame) -> Unit,
    private val onError: (Throwable) -> Unit
) {

    private data class AccessUnit(val payload: ByteArray, val presentationTimeUs: Long)

    private val thread = HandlerThread("x3-h264-decoder").apply { start() }
    private val handler = Handler(thread.looper)

    /** 待喂输入槽（仅解码线程读写，但用锁与链路线程的入队互斥）。 */
    private val auLock = Any()
    private val pending = ArrayDeque<AccessUnit>()
    private var idleInputIndex: Int? = null

    @Volatile private var codec: MediaCodec? = null
    private val released = AtomicBoolean(false)

    init {
        handler.post { createCodec() }
    }

    /**
     * 链路线程调用：把一帧 H.264 访问单元交给解码器。
     * [presentationTimeUs] 是眼镜时钟域的值（MediaCodec 原样回显到输出 BufferInfo）。
     */
    fun queueAccessUnit(payload: ByteArray, presentationTimeUs: Long) {
        if (released.get()) return
        synchronized(auLock) {
            // 解码线程跟不上时（YUV→RGBA 是逐像素软转换），待喂队列会无限增长，
            // 帧龄随之变大、手机端新鲜度门限丢帧。超限丢最旧帧，保新鲜度。
            while (pending.size >= MAX_PENDING_AUS) {
                pending.removeFirst()
            }
            pending.addLast(AccessUnit(payload, presentationTimeUs))
        }
        handler.post { drainPendingInputs() }
    }

    /** 释放解码器，退出解码线程。可重复调用，幂等。 */
    fun release() {
        if (!released.compareAndSet(false, true)) return
        handler.post {
            runCatching { codec?.stop() }
            runCatching { codec?.release() }
            codec = null
            thread.quitSafely()
        }
    }

    // ------------------------------------------------------------------
    // 创建（解码线程）
    // ------------------------------------------------------------------

    private fun createCodec() {
        if (released.get()) return
        try {
            require(codecConfig.isNotEmpty()) { "CODEC_CONFIG 负载为空，无法配置解码器" }
            val format = MediaFormat.createVideoFormat(MIME_TYPE_AVC, widthHint, heightHint)
            // Annex-B SPS+PPS 直接作为 csd-0（40 字节的 CODEC_CONFIG 包）。
            format.setByteBuffer("csd-0", ByteBuffer.wrap(codecConfig))
            // 硬约束（工单 M1-04 打回 2 审查项 1）：KEY_COLOR_FORMAT 必须在 configure
            // 之前设成 flexible，ByteBuffer 模式下解码器才承诺输出 getOutputImage()
            // 可读的线性 YUV_420_888，而不是厂商默认的 UBWC 压缩缓冲。
            format.setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible
            )

            val created = MediaCodec.createDecoderByType(MIME_TYPE_AVC)
            // 先登记再配置：任何后续失败都能由 release() 收尾（否则会泄漏 codec）。
            codec = created
            // 硬约束（工单 M1-04 审查项 9）：setCallback 必须先于 configure。
            created.setCallback(decoderCallback, handler)
            // surface 传 null：ByteBuffer 模式，输出槽由 getOutputImage() 读取。
            created.configure(format, null, null, 0)
            created.start()
            Log.i(TAG, "解码器已创建并启动：${widthHint}x$heightHint，CSD ${codecConfig.size} 字节")
            drainPendingInputs()
        } catch (error: Throwable) {
            Log.e(TAG, "创建解码器失败：${error.message}", error)
            onError(error)
            // 创建失败也要退出解码线程，避免泄漏；下次连接会新建实例。
            release()
        }
    }

    // ------------------------------------------------------------------
    // 输入（解码线程：onInputBufferAvailable / drainPendingInputs）
    // ------------------------------------------------------------------

    private fun drainPendingInputs() {
        val created = codec ?: return
        while (true) {
            val index: Int
            val unit: AccessUnit
            synchronized(auLock) {
                val idle = idleInputIndex ?: return
                unit = pending.removeFirstOrNull() ?: return
                index = idle
                idleInputIndex = null
            }
            feedInput(created, index, unit)
        }
    }

    private fun feedInput(codec: MediaCodec, index: Int, unit: AccessUnit) {
        try {
            val buffer = codec.getInputBuffer(index) ?: return
            if (unit.payload.size > buffer.capacity()) {
                throw IllegalArgumentException(
                    "访问单元 ${unit.payload.size} 字节超过输入缓冲 ${buffer.capacity()} 字节"
                )
            }
            buffer.clear()
            buffer.put(unit.payload)
            codec.queueInputBuffer(index, 0, unit.payload.size, unit.presentationTimeUs, 0)
        } catch (error: Exception) {
            Log.e(TAG, "喂输入缓冲失败：${error.message}", error)
            onError(error)
        }
    }

    private val decoderCallback = object : MediaCodec.Callback() {
        override fun onInputBufferAvailable(codec: MediaCodec, index: Int) {
            if (released.get()) return
            // 被取代/已释放的旧实例的滞留回调：输入槽与当前 pending 无关，忽略。
            if (codec !== this@H264Decoder.codec) return
            var unit: AccessUnit? = null
            synchronized(auLock) {
                unit = pending.removeFirstOrNull()
                if (unit == null) idleInputIndex = index
            }
            if (unit != null) feedInput(codec, index, unit)
        }

        override fun onOutputBufferAvailable(
            codec: MediaCodec,
            index: Int,
            info: MediaCodec.BufferInfo
        ) {
            if (released.get()) return
            // 用回调自带的 codec 而不是字段：旧实例的滞留回调必须忽略，否则会把旧
            // 索引 release 到新实例上。
            if (codec !== this@H264Decoder.codec) return
            if (info.size <= 0) {
                // 配置数据等无内容缓冲：不取图，直接还槽。
                codec.releaseOutputBuffer(index, false)
                return
            }
            // 输出槽处理收敛到一个可注入的入口（工单 M1-04 打回 2 第 4 节第 2 条）：
            // getOutputImage() 判空、Image 先关闭再还槽、所有分支（含判空、含异常）
            // 都走到 releaseOutputBuffer(index, false)。
            OutputBufferGuard.process(
                obtainImage = { codec.getOutputImage(index) },
                closeImage = { it.close() },
                releaseBuffer = { codec.releaseOutputBuffer(index, false) },
                consumeImage = { image -> handleOutputImage(image, info.presentationTimeUs) },
                onFailure = { error ->
                    Log.e(TAG, "YUV→RGBA 失败：${error.message}", error)
                    onError(error)
                }
            )
        }

        override fun onError(codec: MediaCodec, error: MediaCodec.CodecException) {
            if (codec !== this@H264Decoder.codec) return
            Log.e(TAG, "解码器错误：${error.message}", error)
            onError(error)
        }

        override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
            try {
                val outputWidth = format.getInteger(MediaFormat.KEY_WIDTH)
                val outputHeight = format.getInteger(MediaFormat.KEY_HEIGHT)
                Log.i(TAG, "解码器输出格式：${outputWidth}x$outputHeight")
                // ByteBuffer 模式下没有预绑定的 surface：分辨率变化由 getOutputImage()
                // 每帧自带的 cropRect / stride 自然承载，不需要重建解码器。
            } catch (error: Throwable) {
                Log.w(TAG, "读取输出格式失败：${error.message}")
            }
        }
    }

    // ------------------------------------------------------------------
    // YUV → RGBA（解码线程）
    // ------------------------------------------------------------------

    private fun handleOutputImage(image: Image, presentationTimeUs: Long) {
        // 工单 M1-04 打回 1 第 3.2 节：可见区域一律以 cropRect 为准。
        // image.width/height 是含 16 对齐补齐的分配尺寸（640×368），按它取样会
        // 多出 8 行垃圾且几何比例全错；cropRect 才是真实可见区域（0,0,640,360）。
        val crop = image.cropRect
        val width = crop.width()
        val height = crop.height()
        if (width <= 0 || height <= 0) {
            Log.e(TAG, "丢弃无效帧：cropRect ${width}x$height")
            return
        }
        val planes = image.planes
        // 工单 M1-04 打回 2 第 3.3 节：三个平面一律各自 copyPlane() + 各自的
        // rowStride / pixelStride / baseOffset，U/V 不共享 buffer、不做任何基址推断。
        // 半平面下多拷一份色度（本例 188 KB/帧），换来 I420 和 NV12/NV21 一视同仁，
        // 这个代价必须付。
        val y = YuvPlane(
            data = copyPlane(planes[0]),
            rowStride = planes[0].rowStride,
            pixelStride = planes[0].pixelStride,
            baseOffset = cropOffset(crop, planes[0].rowStride, planes[0].pixelStride, isChroma = false)
        )
        val u = YuvPlane(
            data = copyPlane(planes[1]),
            rowStride = planes[1].rowStride,
            pixelStride = planes[1].pixelStride,
            baseOffset = cropOffset(crop, planes[1].rowStride, planes[1].pixelStride, isChroma = true)
        )
        val v = YuvPlane(
            data = copyPlane(planes[2]),
            rowStride = planes[2].rowStride,
            pixelStride = planes[2].pixelStride,
            baseOffset = cropOffset(crop, planes[2].rowStride, planes[2].pixelStride, isChroma = true)
        )

        // 工单 M1-04 打回 1 第 4 节：前置校验前移到取 Image 之后立即执行。
        // ByteBuffer 模式下越界是普通 IndexOutOfBoundsException（可捕获），但校验
        // 仍然让失效表现为「丢帧 + 日志」而不是异常穿到链路线程。
        val rgbaByteCount = YuvToRgba.rgbaByteCountFor(width, height)
        val issue = YuvToRgba.validateFrame(width, height, rgbaByteCount, y, u, v)
        if (issue != null) {
            Log.e(TAG, "丢弃无效帧（cropRect ${width}x$height）：$issue")
            return
        }
        val rgba = ByteArray(rgbaByteCount)
        YuvToRgba.convertIntoRgba(width, height, y, u, v, rgba)
        // PTS 取本帧回调自带的 info.presentationTimeUs，不再用「渲染顺序 ==
        // onImageAvailable 顺序」的队列推断（工单 M1-04 打回 2 第 3.1 节：丢一帧就
        // 永久错位的隐患已随 renderedPts 一并删除）。
        onFrame(
            DecodedVideoFrame(
                width = width,
                height = height,
                presentationTimeUs = presentationTimeUs,
                rgba8888 = rgba
            )
        )
    }

    /** 拷贝整个平面（含行填充）。三个平面各自独立调用（工单 M1-04 打回 2 第 3.3 节）。 */
    private fun copyPlane(plane: Image.Plane): ByteArray {
        val buffer: ByteBuffer = plane.buffer
        val bytes = ByteArray(buffer.remaining())
        buffer.get(bytes)
        return bytes
    }

    /**
     * cropRect 起点在该平面 buffer 里的字节偏移（`top * rowStride + left * pixelStride`）。
     * U/V 是 4:2:0 色度，横纵各减半；YUV_420_888 的 cropRect left/top 保证为偶数，
     * 整除无歧义。Y 平面用完整分辨率。
     */
    private fun cropOffset(crop: Rect, rowStride: Int, pixelStride: Int, isChroma: Boolean): Int {
        val left = if (isChroma) crop.left / 2 else crop.left
        val top = if (isChroma) crop.top / 2 else crop.top
        return top * rowStride + left * pixelStride
    }

    companion object {
        private const val TAG = "H264Decoder"
        private const val MIME_TYPE_AVC = "video/avc"

        /** 待喂访问单元上限（45 ≈ 3 秒 @15fps），超过则丢最旧帧。 */
        private const val MAX_PENDING_AUS = 45
    }
}
