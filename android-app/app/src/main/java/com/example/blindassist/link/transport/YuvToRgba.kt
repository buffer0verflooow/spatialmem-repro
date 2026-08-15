package com.example.blindassist.link.transport

/**
 * 一个 YUV_420_888 平面（字段与 `android.media.Image.Plane` 一一对应，零 Android 依赖）。
 *
 * [baseOffset] 是**本平面自己的数据**里第一个可见样本的字节偏移（工单 M1-04
 * 打回 1 第 3.2 节）：`crop.top * rowStride + crop.left * pixelStride`，色度平面
 * 按 4:2:0 横纵减半 —— 让转换只取可见区域，不含 16 对齐补齐的行/列。
 *
 * 工单 M1-04 打回 2 第 3.3 节：三个平面一律**各自独立拷贝、各自定址**，U/V 之间
 * 不存在共享 buffer 与相对偏移。半平面（NV12/NV21）下 plane[1]/plane[2] 是同一段
 * 交织数据的两个独立 `slice()`（position() 都为 0、数据本身错开一个样本），
 * 各自拷成独立 [data] 后，用各自的 pixelStride 取样自然得到正确的 U/V，
 * 不需要也不允许从 position 之差推断共享基址。
 */
data class YuvPlane(
    val data: ByteArray,
    val rowStride: Int,
    val pixelStride: Int,
    val baseOffset: Int = 0
)

/**
 * YUV_420_888 → RGBA8888 的纯 Kotlin 转换（工单 M1-04 约束 2）。零 Android 依赖，可 JVM 单测。
 *
 * **不做布局分派**（工单 M1-04 打回 2 第 3.3 节）：调用方（H264Decoder）已把每个
 * 平面各自拷贝、以各自 rowStride/pixelStride/baseOffset 传入。取样公式对三种布局
 * 一视同仁 —— I420（pixelStride=1）逐样本读独立平面；NV12/NV21（pixelStride=2）
 * 各自隔一个字节读自己的视图。U/V 是否被区分开由**数据本身**决定（plane[1] 的
 * 第一个字节是 U 还是 V），不依赖任何基址推断，因此 NV12 与 NV21 不可能再被
 * 读成同一份数据。
 *
 * 所有平面一律**逐行按 rowStride 取样本**（解码器/相机输出普遍有行填充，
 * 按 width 连续读会得到斜掉的画面）。
 *
 * 颜色空间：BT.601 有限范围（与 libyuv `I420ToARGB` 的默认口径一致），整数定点系数：
 * ```
 * R = (298·(Y−16) + 409·(V−128) + 128) >> 8
 * G = (298·(Y−16) − 100·(U−128) − 208·(V−128) + 128) >> 8
 * B = (298·(Y−16) + 516·(U−128) + 128) >> 8
 * ```
 * 结果 clamp 到 [0, 255]；alpha 恒为 255；输出顺序 R,G,B,A（与
 * [com.example.blindassist.source.VideoFrame.rgba8888] 口径一致）。
 *
 * 本实现是工单 5 原生加速（libyuv）的**逐像素比对基准**，所以正确性优先，不图快。
 */
object YuvToRgba {

    const val RGBA_BYTES_PER_PIXEL = 4

    /** 转换到新建的 RGBA8888 [ByteArray]。 */
    fun convertToRgba(
        width: Int,
        height: Int,
        y: YuvPlane,
        u: YuvPlane,
        v: YuvPlane
    ): ByteArray {
        val out = ByteArray(rgbaByteCountFor(width, height))
        convertIntoRgba(width, height, y, u, v, out)
        return out
    }

    /** 转换到调用方提供的输出数组；不满足大小时抛出 [IllegalArgumentException]。 */
    fun convertIntoRgba(
        width: Int,
        height: Int,
        y: YuvPlane,
        u: YuvPlane,
        v: YuvPlane,
        out: ByteArray
    ) {
        require(width > 0 && height > 0) { "图像尺寸必须为正：${width}x$height" }
        val needed = width.toLong() * height * RGBA_BYTES_PER_PIXEL
        require(needed <= out.size) { "输出数组 ${out.size} 字节，需要 $needed 字节" }

        val chromaWidth = (width + 1) shr 1
        val chromaHeight = (height + 1) shr 1
        validatePlane(y, height, width, "Y")
        validatePlane(u, chromaHeight, chromaWidth, "U")
        validatePlane(v, chromaHeight, chromaWidth, "V")
        convertPixels(width, height, y, u, v, out)
    }

    /**
     * RGBA8888 输出字节数（`width × height × 4`）。尺寸非正或溢出时抛
     * [IllegalArgumentException]，与 [convertIntoRgba] 的口径一致。
     */
    fun rgbaByteCountFor(width: Int, height: Int): Int {
        require(width > 0 && height > 0) { "图像尺寸必须为正：${width}x$height" }
        val byteCount = width.toLong() * height * RGBA_BYTES_PER_PIXEL
        require(byteCount <= Int.MAX_VALUE) { "图像过大：$byteCount 字节" }
        return byteCount.toInt()
    }

    /**
     * 前置校验（工单 M1-04 打回 1 第 4 节）：在取到 `Image` 之后、交给转换之前调用。
     *
     * 校验两点：
     * 1. cropRect 尺寸 × 4 必须等于目标 RGBA 缓冲长度 [rgbaByteCount] —— 只按可见区域
     *    取样，拒绝按含 16 对齐补齐的分配尺寸（`image.width/height`）取样的几何错配；
     * 2. 三个平面各自的数据长度都要覆盖可见区域最后一行最后一个样本
     *    `(rows-1)*rowStride + (cols-1)*pixelStride + 1`（含 [YuvPlane.baseOffset]），
     *    不满足就拒绝，而不是继续往下读（JVM 上可单测：短平面 → 被拒绝而非越界读）。
     *
     * 返回 `null` 表示帧有效；否则返回可读的拒绝原因，调用方应**记一条 error 并丢弃
     * 这一帧**，让失效表现为「丢帧 + 日志」而不是崩溃或静默读错数据。
     */
    fun validateFrame(
        width: Int,
        height: Int,
        rgbaByteCount: Int,
        y: YuvPlane,
        u: YuvPlane,
        v: YuvPlane
    ): String? {
        if (width <= 0 || height <= 0) {
            return "cropRect 尺寸必须为正：${width}x$height"
        }
        val needed = rgbaByteCountFor(width, height)
        if (rgbaByteCount != needed) {
            return "RGBA 缓冲长度 $rgbaByteCount 与 cropRect ${width}x$height×4=$needed 不一致"
        }
        val chromaWidth = (width + 1) shr 1
        val chromaHeight = (height + 1) shr 1
        return planeCoverageIssue(y, height, width, "Y")
            ?: planeCoverageIssue(u, chromaHeight, chromaWidth, "U")
            ?: planeCoverageIssue(v, chromaHeight, chromaWidth, "V")
    }

    private fun convertPixels(
        width: Int,
        height: Int,
        y: YuvPlane,
        u: YuvPlane,
        v: YuvPlane,
        out: ByteArray
    ) {
        val yData = y.data
        val uData = u.data
        val vData = v.data
        val yBase = y.baseOffset
        val uBase = u.baseOffset
        val vBase = v.baseOffset
        val yRowStride = y.rowStride
        val uRowStride = u.rowStride
        val vRowStride = v.rowStride
        val uPixelStride = u.pixelStride
        val vPixelStride = v.pixelStride

        for (row in 0 until height) {
            val yRowBase = yBase + row * yRowStride
            val uRowBase = (row shr 1) * uRowStride
            val vRowBase = (row shr 1) * vRowStride
            var outPos = row * width * RGBA_BYTES_PER_PIXEL
            for (col in 0 until width) {
                val yv = yData[yRowBase + col].toInt() and 0xFF
                // 4:2:0 色度：每个 2x2 亮度块共享一对 U/V。
                // U/V 各自用**自己的** rowStride / pixelStride / baseOffset 定址
                // （工单 M1-04 打回 2 第 3.3 节），不再假设两者同 stride 同基址。
                val uVal = uData[uBase + uRowBase + (col shr 1) * uPixelStride].toInt() and 0xFF
                val vVal = vData[vBase + vRowBase + (col shr 1) * vPixelStride].toInt() and 0xFF

                val yScaled = yv - 16
                val r = clampByte((298 * yScaled + 409 * (vVal - 128) + 128) shr 8)
                val g = clampByte(
                    (298 * yScaled - 100 * (uVal - 128) - 208 * (vVal - 128) + 128) shr 8
                )
                val b = clampByte((298 * yScaled + 516 * (uVal - 128) + 128) shr 8)

                out[outPos++] = r
                out[outPos++] = g
                out[outPos++] = b
                out[outPos++] = 0xFF.toByte()
            }
        }
    }

    private fun clampByte(value: Int): Byte = value.coerceIn(0, 255).toByte()

    /**
     * 校验单个平面：rowStride 不得小于「列数 × pixelStride」，且数据长度必须覆盖
     * 最后一行的最后一个样本（含行填充）。报错信息可读，而不是数组越界。
     */
    private fun validatePlane(plane: YuvPlane, rows: Int, columns: Int, what: String) {
        val issue = planeCoverageIssue(plane, rows, columns, what)
        require(issue == null) { issue!! }
    }

    /**
     * 与 [validatePlane] 同一套覆盖度判定，但返回原因而不是抛异常，供
     * [validateFrame] 在转换之前复用（工单 M1-04 打回 1 第 4 节）。
     */
    private fun planeCoverageIssue(
        plane: YuvPlane,
        rows: Int,
        columns: Int,
        what: String
    ): String? {
        if (plane.rowStride < columns * plane.pixelStride) {
            return "$what 平面 rowStride=${plane.rowStride} 小于 $columns × " +
                "pixelStride=${plane.pixelStride}"
        }
        // 最后一行的最后一个样本：base + (rows-1)*rowStride + (columns-1)*pixelStride，
        // 数据长度需要覆盖到它之后（+1）。
        val neededEnd = (rows - 1) * plane.rowStride + (columns - 1) * plane.pixelStride + 1
        if (plane.data.size < plane.baseOffset + neededEnd) {
            return "$what 平面数据不足：baseOffset=${plane.baseOffset}，需要 ≥ " +
                "${plane.baseOffset + neededEnd} 字节，实际 ${plane.data.size}"
        }
        return null
    }
}
