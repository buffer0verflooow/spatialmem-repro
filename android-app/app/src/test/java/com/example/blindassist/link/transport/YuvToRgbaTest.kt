package com.example.blindassist.link.transport

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * YuvToRgba 的 JVM 单测（工单 M1-04 第 4 节 1–6；打回 2 第 3.3 节 U/V 独立性回归）。
 *
 * 布局分派在打回 2 已删除（工单第 3.3 节）：半平面（NV12/NV21）下 U/V 是两个
 * position()==0 的独立 slice()，调用方各自拷成独立 buffer。因此 NV12/NV21 用例
 * 改为「U 视图 / V 视图各自独立」的构造，断言三种 pixelStride 组合都能转对
 * （这条断言从打回 1 保留至今）。
 *
 * 期望值全部用手算：BT.601 有限范围整数公式（298/409/516/100/208，+128 后 >>8，clamp 0..255）
 * 下，以下 YUV 三元组给出精确的原色：
 * - 白 (235,128,128) → (255,255,255)
 * - 红 (81,90,240)   → (255,0,0)
 * - 蓝 (41,240,110)  → (0,0,255)
 * - 灰 (128,128,128) → (130,130,130)
 */
class YuvToRgbaTest {

    // 4x4 图像，四个 2x2 色块：白 / 红 / 蓝 / 灰。
    private val y4 = b(
        235, 235, 81, 81,
        235, 235, 81, 81,
        41, 41, 128, 128,
        41, 41, 128, 128
    )

    /** 2x2 色度网格：U 行主序（白块 U=128、红块 U=90、蓝块 U=240、灰块 U=128）。 */
    private val u4 = b(128, 90, 240, 128)

    /** 2x2 色度网格：V 行主序（白块 V=128、红块 V=240、蓝块 V=110、灰块 V=128）。 */
    private val v4 = b(128, 240, 110, 128)

    @Test
    fun i420_pixelStride1_convertsToKnownRgba() {
        val rgba = YuvToRgba.convertToRgba(
            width = 4,
            height = 4,
            y = YuvPlane(y4, rowStride = 4, pixelStride = 1),
            u = YuvPlane(u4, rowStride = 2, pixelStride = 1),
            v = YuvPlane(v4, rowStride = 2, pixelStride = 1)
        )

        assertArrayEquals(expectedRgba4x4(), rgba)
    }

    @Test
    fun nv12_pixelStride2_uFirst_convertsToKnownRgba() {
        // getOutputImage() 的半平面实况（工单 M1-04 打回 2 第 3.3 节）：plane[1] 是
        // 交织数据从 U 开始的视图，plane[2] 是同一段数据从 V 开始的视图（错 1 字节），
        // 两者 position() 都是 0。各自拷成独立 buffer 后，U/V 各按自己的 pixelStride
        // 隔一个字节取样，不得再靠 position 之差推断共享基址。
        // 交织行 0 = (U0,V0)(U1,V1)，行 1 = (U2,V2)(U3,V3)。
        val uView = b(
            128, 128, 90, 240,
            240, 110, 128, 128
        )
        val vView = b(
            128, 90, 240, 240,
            110, 128, 128, 128
        )
        val rgba = YuvToRgba.convertToRgba(
            width = 4,
            height = 4,
            y = YuvPlane(y4, 4, 1),
            u = YuvPlane(uView, rowStride = 4, pixelStride = 2, baseOffset = 0),
            v = YuvPlane(vView, rowStride = 4, pixelStride = 2, baseOffset = 0)
        )

        assertArrayEquals(expectedRgba4x4(), rgba)
    }

    @Test
    fun nv21_pixelStride2_vFirst_convertsToKnownRgba() {
        // 同上，但交织顺序是 V 在前：plane[1]（U 视图）从 U0 开始，
        // plane[2]（V 视图）从 V0 开始（即原始交织数据本身）。
        // 交织行 0 = (V0,U0)(V1,U1)，行 1 = (V2,U2)(V3,U3)。
        val uView = b(
            128, 240, 90, 110,
            240, 128, 128, 128
        )
        val vView = b(
            128, 128, 240, 90,
            110, 240, 128, 128
        )
        val rgba = YuvToRgba.convertToRgba(
            width = 4,
            height = 4,
            y = YuvPlane(y4, 4, 1),
            u = YuvPlane(uView, rowStride = 4, pixelStride = 2, baseOffset = 0),
            v = YuvPlane(vView, rowStride = 4, pixelStride = 2, baseOffset = 0)
        )

        assertArrayEquals(expectedRgba4x4(), rgba)
    }

    @Test
    fun semiPlanarIndependentViews_positionZero_uvStayDistinct() {
        // 工单 M1-04 打回 2 第 3.3 节 / 第 4 节第 1 条的回归：造一份 NV12 布局的假平面
        // —— U/V 是同一段交织数据的两个视图、position() 都为 0、V 视图相对 U 视图偏移
        // 1 字节，U 全填 0x40、V 全填 0xC0。
        //
        // 旧实现按 position 之差推断共享基址（uPos==vPos==0 → sharedStart=0），U/V
        // 会读到**同一份** 0x40，静默错色。验证（与实现逐位一致的模拟，见提交说明）：
        //   旧实现：像素 = (R=153, G=255, B=126)   ← U/V 都是 0x40
        //   新实现：像素 = (R=255, G=228, B=126)   ← U=0x40, V=0xC0
        // 因此下面精确到通道的断言（尤其 R=255 ≠ 153）在旧实现上先失败，能复现旧错色。
        // 另附判据：正确输出下纯色区域 R ≠ B（255 ≠ 126）——U/V 被区分开。
        val width = 4
        val height = 4
        val y = b(
            235, 235, 235, 235,
            235, 235, 235, 235,
            235, 235, 235, 235,
            235, 235, 235, 235
        )
        val uView = b(
            0x40, 0xC0, 0x40, 0xC0,
            0x40, 0xC0, 0x40, 0xC0
        )
        val vView = b(
            0xC0, 0x40, 0xC0, 0x40,
            0xC0, 0x40, 0xC0, 0x40
        )

        val rgba = YuvToRgba.convertToRgba(
            width = width,
            height = height,
            y = YuvPlane(y, rowStride = 4, pixelStride = 1),
            u = YuvPlane(uView, rowStride = 4, pixelStride = 2, baseOffset = 0),
            v = YuvPlane(vView, rowStride = 4, pixelStride = 2, baseOffset = 0)
        )

        // 手算（Y=235, U=0x40=64, V=0xC0=192）：
        // R = (298·219 + 409·64 + 128) >> 8 = 357 → clamp 255
        // G = (298·219 − 100·(−64) − 208·64 + 128) >> 8 = 228
        // B = (298·219 + 516·(−64) + 128) >> 8 = 126
        for (pixel in 0 until width * height) {
            assertPixel(rgba, pixel, 255, 228, 126, 255)
            val offset = pixel * 4
            val r = rgba[offset].toInt() and 0xFF
            val bVal = rgba[offset + 2].toInt() and 0xFF
            assertTrue("纯色区域 U/V 必须被区分开（R=$r 不应等于 B=$bVal）", r != bVal)
        }
    }

    @Test
    fun rowStridePadding_isSkippedNotReadAsPixels() {
        // 同一张 4x4 图像，但三个平面的 rowStride 都大于宽度，填充字节塞满 0x00。
        // 若实现按 width 连续读，填充会被当成像素，得到一帧颜色全错的斜画面。
        val yPadded = b(
            235, 235, 81, 81, 0, 0, 0, 0,
            235, 235, 81, 81, 0, 0, 0, 0,
            41, 41, 128, 128, 0, 0, 0, 0,
            41, 41, 128, 128, 0, 0, 0, 0
        )
        val uPadded = b(
            128, 90, 0, 0, 0, 0, 0, 0,
            240, 128, 0, 0, 0, 0, 0, 0
        )
        val vPadded = b(
            128, 240, 0, 0, 0, 0, 0, 0,
            110, 128, 0, 0, 0, 0, 0, 0
        )

        val rgba = YuvToRgba.convertToRgba(
            width = 4,
            height = 4,
            y = YuvPlane(yPadded, rowStride = 8, pixelStride = 1),
            u = YuvPlane(uPadded, rowStride = 8, pixelStride = 1),
            v = YuvPlane(vPadded, rowStride = 8, pixelStride = 1)
        )

        // 输出与无填充的 4x4 完全一致，即填充字节从未进入像素。
        assertArrayEquals(expectedRgba4x4(), rgba)
    }

    @Test
    fun oddAndMinimalSizes_doNotReadOutOfBounds() {
        // 最小尺寸 2x2：全红。
        val y2 = b(81, 81, 81, 81)
        val rgba2 = YuvToRgba.convertToRgba(
            width = 2,
            height = 2,
            y = YuvPlane(y2, rowStride = 2, pixelStride = 1),
            u = YuvPlane(b(90), rowStride = 1, pixelStride = 1),
            v = YuvPlane(b(240), rowStride = 1, pixelStride = 1)
        )
        assertEquals(2 * 2 * 4, rgba2.size)
        for (pixel in 0 until 4) {
            assertPixel(rgba2, pixel, 255, 0, 0, 255)
        }

        // 奇数尺寸 3x3：色度网格仍是 2x2，最后一行/列复用第二组色度。
        val y3 = b(
            235, 235, 81,
            235, 235, 81,
            41, 41, 128
        )
        val u3 = b(128, 90, 240, 128)
        val v3 = b(128, 240, 110, 128)
        val rgba3 = YuvToRgba.convertToRgba(
            width = 3,
            height = 3,
            y = YuvPlane(y3, rowStride = 3, pixelStride = 1),
            u = YuvPlane(u3, rowStride = 2, pixelStride = 1),
            v = YuvPlane(v3, rowStride = 2, pixelStride = 1)
        )
        assertEquals(3 * 3 * 4, rgba3.size)
        // 行 0-1：白白红；行 2：蓝蓝灰。
        val expected3 = listOf(
            intArrayOf(255, 255, 255), intArrayOf(255, 255, 255), intArrayOf(255, 0, 0),
            intArrayOf(255, 255, 255), intArrayOf(255, 255, 255), intArrayOf(255, 0, 0),
            intArrayOf(0, 0, 255), intArrayOf(0, 0, 255), intArrayOf(130, 130, 130)
        )
        expected3.forEachIndexed { index, rgb ->
            assertPixel(rgba3, index, rgb[0], rgb[1], rgb[2], 255)
        }
    }

    @Test
    fun outputSizeAndAlpha_areExactForEveryLayout() {
        val width = 2
        val height = 2
        val y = b(235, 235, 235, 235)
        val u = b(128)
        val v = b(128)
        val i420 = YuvToRgba.convertToRgba(
            width, height,
            YuvPlane(y, 2, 1), YuvPlane(u, 1, 1), YuvPlane(v, 1, 1)
        )
        assertSizeAndAlpha(i420, width, height)

        // 半平面下 U/V 各自独立视图（打回 2 第 3.3 节），数据全 128（灰度）。
        val nv12uView = b(128, 128, 128, 128)
        val nv12vView = b(128, 128, 128, 128)
        val nv12 = YuvToRgba.convertToRgba(
            width, height,
            YuvPlane(y, 2, 1),
            YuvPlane(nv12uView, 2, 2, 0),
            YuvPlane(nv12vView, 2, 2, 0)
        )
        assertSizeAndAlpha(nv12, width, height)

        val nv21uView = b(128, 128, 128, 128)
        val nv21vView = b(128, 128, 128, 128)
        val nv21 = YuvToRgba.convertToRgba(
            width, height,
            YuvPlane(y, 2, 1),
            YuvPlane(nv21uView, 2, 2, 0),
            YuvPlane(nv21vView, 2, 2, 0)
        )
        assertSizeAndAlpha(nv21, width, height)
    }

    @Test
    fun frontValidation_acceptsValidFrameWithRowPadding() {
        // 与 rowStridePadding_isSkippedNotReadAsPixels 同构的带填充数据：前置校验必须放行，
        // 数据覆盖度以 rowStride 为准而不是连续宽度。
        val yPadded = b(
            235, 235, 81, 81, 0, 0, 0, 0,
            235, 235, 81, 81, 0, 0, 0, 0,
            41, 41, 128, 128, 0, 0, 0, 0,
            41, 41, 128, 128, 0, 0, 0, 0
        )
        val uPadded = b(128, 90, 0, 0, 0, 0, 0, 0, 240, 128, 0, 0, 0, 0, 0, 0)
        val vPadded = b(128, 240, 0, 0, 0, 0, 0, 0, 110, 128, 0, 0, 0, 0, 0, 0)

        assertNull(
            YuvToRgba.validateFrame(
                width = 4,
                height = 4,
                rgbaByteCount = 4 * 4 * 4,
                y = YuvPlane(yPadded, rowStride = 8, pixelStride = 1),
                u = YuvPlane(uPadded, rowStride = 8, pixelStride = 1),
                v = YuvPlane(vPadded, rowStride = 8, pixelStride = 1)
            )
        )
    }

    @Test
    fun frontValidation_rejectsPlaneTooShortToCoverCropRect() {
        // 工单 M1-04 打回 1 第 4 节的 JVM 单测：构造「平面数据长度不足以覆盖 cropRect」
        // 的输入 —— Y 平面少最后一行最后一个样本。必须被前置校验拒绝（返回原因），
        // 而不是在转换里越界读；直接转换同样必须抛 IllegalArgumentException 而不是
        // ArrayIndexOutOfBoundsException。
        val shortY = b(
            235, 235, 81, 81,
            235, 235, 81, 81,
            41, 41, 128, 128,
            41, 41, 128
        )
        val issue = YuvToRgba.validateFrame(
            width = 4,
            height = 4,
            rgbaByteCount = 4 * 4 * 4,
            y = YuvPlane(shortY, rowStride = 4, pixelStride = 1),
            u = YuvPlane(u4, rowStride = 2, pixelStride = 1),
            v = YuvPlane(v4, rowStride = 2, pixelStride = 1)
        )
        assertNotNull("短 Y 平面必须被拒绝", issue)
        assertTrue("拒绝原因应包含平面与字节数：$issue", issue!!.contains("Y 平面数据不足"))

        assertThrows(IllegalArgumentException::class.java) {
            YuvToRgba.convertToRgba(
                width = 4,
                height = 4,
                y = YuvPlane(shortY, 4, 1),
                u = YuvPlane(u4, 2, 1),
                v = YuvPlane(v4, 2, 1)
            )
        }
    }

    @Test
    fun frontValidation_rejectsCropBiggerThanAllocatedPlaneData() {
        // 真机崩溃的同构场景：数据按 640×360 分配，但 cropRect 覆盖到 640×368
        // （含 16 对齐补齐）。JVM 上无法复现 nativeCreatePlanes 的 JNI abort，但前置
        // 校验必须拒绝这种输入，而不是带着不足的数据进转换。
        val y = ByteArray(640 * 360)
        val u = ByteArray(320 * 180)
        val v = ByteArray(320 * 180)
        val issue = YuvToRgba.validateFrame(
            width = 640,
            height = 368,
            rgbaByteCount = 640 * 368 * 4,
            y = YuvPlane(y, rowStride = 640, pixelStride = 1),
            u = YuvPlane(u, rowStride = 320, pixelStride = 1),
            v = YuvPlane(v, rowStride = 320, pixelStride = 1)
        )
        assertNotNull("cropRect 超过分配数据时必须被拒绝", issue)
        assertTrue("拒绝原因应指向数据不足：$issue", issue!!.contains("数据不足"))
    }

    @Test
    fun frontValidation_rejectsRgbaLengthMismatchWithCrop() {
        // 「cropRect 尺寸 × 4 == 目标 RGBA 缓冲长度」：目标缓冲长度不对直接拒绝。
        val issue = YuvToRgba.validateFrame(
            width = 4,
            height = 4,
            rgbaByteCount = 4 * 4 * 3,
            y = YuvPlane(y4, 4, 1),
            u = YuvPlane(u4, 2, 1),
            v = YuvPlane(v4, 2, 1)
        )
        assertNotNull(issue)
        assertTrue(issue!!.contains("RGBA 缓冲长度"))
    }

    @Test
    fun cropRectOffset_samplingProducesOnlyVisiblePixels() {
        // 3.2 节的取样数学（JVM 侧）：crop 起点 (2,0)（偶数，4:2:0 无歧义），
        // Y 基址 = top*rowStride + left*pixelStride，色度基址按减半算。
        // 只取 2x2 可见区（红块），输出必须是纯红，且不含右侧填充字节。
        val yPadded = b(
            235, 235, 81, 81, 0, 0, 0, 0,
            235, 235, 81, 81, 0, 0, 0, 0,
            41, 41, 128, 128, 0, 0, 0, 0,
            41, 41, 128, 128, 0, 0, 0, 0
        )
        val uPadded = b(128, 90, 0, 0, 0, 0, 0, 0, 240, 128, 0, 0, 0, 0, 0, 0)
        val vPadded = b(128, 240, 0, 0, 0, 0, 0, 0, 110, 128, 0, 0, 0, 0, 0, 0)
        val yBase = 0 * 8 + 2 * 1
        val uvBase = (0 / 2) * 8 + (2 / 2) * 1

        val rgba = YuvToRgba.convertToRgba(
            width = 2,
            height = 2,
            y = YuvPlane(yPadded, 8, 1, yBase),
            u = YuvPlane(uPadded, 8, 1, uvBase),
            v = YuvPlane(vPadded, 8, 1, uvBase)
        )

        assertEquals(2 * 2 * 4, rgba.size)
        for (pixel in 0 until 4) {
            assertPixel(rgba, pixel, 255, 0, 0, 255)
        }
    }

    // ------------------------------------------------------------------
    // 辅助
    // ------------------------------------------------------------------

    /** 把无符号字节字面量转成 ByteArray（避免 128..255 逐个 .toByte()）。 */
    private fun b(vararg values: Int): ByteArray =
        ByteArray(values.size) { values[it].toByte() }

    private fun assertSizeAndAlpha(rgba: ByteArray, width: Int, height: Int) {
        assertEquals(width.toLong() * height * 4, rgba.size.toLong())
        var offset = 3
        while (offset < rgba.size) {
            assertEquals("第 ${offset / 4} 个像素的 alpha", 255, rgba[offset].toInt() and 0xFF)
            offset += 4
        }
    }

    private fun assertPixel(rgba: ByteArray, pixel: Int, r: Int, g: Int, b: Int, a: Int) {
        val offset = pixel * 4
        assertEquals(r, rgba[offset].toInt() and 0xFF)
        assertEquals(g, rgba[offset + 1].toInt() and 0xFF)
        assertEquals(b, rgba[offset + 2].toInt() and 0xFF)
        assertEquals(a, rgba[offset + 3].toInt() and 0xFF)
    }

    private fun expectedRgba4x4(): ByteArray {
        // 白块 (0,0)-(1,1)；红块 (2,0)-(3,1)；蓝块 (0,2)-(1,3)；灰块 (2,2)-(3,3)。
        val colors = intArrayOf(
            0xFFFFFFFF.toInt(), 0xFFFFFFFF.toInt(), 0xFFFF0000.toInt(), 0xFFFF0000.toInt(),
            0xFFFFFFFF.toInt(), 0xFFFFFFFF.toInt(), 0xFFFF0000.toInt(), 0xFFFF0000.toInt(),
            0xFF0000FF.toInt(), 0xFF0000FF.toInt(), 0xFF828282.toInt(), 0xFF828282.toInt(),
            0xFF0000FF.toInt(), 0xFF0000FF.toInt(), 0xFF828282.toInt(), 0xFF828282.toInt()
        )
        return ByteArray(colors.size * 4) { index ->
            val pixel = index / 4
            val channel = index % 4
            when (channel) {
                0 -> ((colors[pixel] shr 16) and 0xFF).toByte()
                1 -> ((colors[pixel] shr 8) and 0xFF).toByte()
                2 -> (colors[pixel] and 0xFF).toByte()
                else -> ((colors[pixel] ushr 24) and 0xFF).toByte()
            }
        }
    }
}
