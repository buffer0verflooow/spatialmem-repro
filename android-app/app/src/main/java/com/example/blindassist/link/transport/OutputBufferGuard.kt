package com.example.blindassist.link.transport

/**
 * 输出缓冲槽处理门（工单 M1-04 打回 2 第 3.1/3.3 节、审查项 2/3、第 4 节第 2 条）。
 *
 * 保证「取 Image → 用 → 还槽」这条纪律在**所有**分支下都不会漏掉
 * `releaseOutputBuffer(index, false)`：
 * - `getOutputImage()` 返回 null（编解码器不支持 flexible、或该缓冲是配置数据）
 *   → 直接把输出槽还回去。漏还一个就少一个槽，几帧后解码器无槽可用而停住，
 *   表现为「不崩但没画面」；
 * - `getOutputImage()` 抛异常 → 按 null 处理，同样还槽；
 * - 转换/回调抛异常 → 记 [onFailure] 后，Image 先关闭再还槽；
 * - 正常路径 → Image 先关闭再还槽。
 *
 * 独立成零 Android 依赖的小类：H264Decoder 在 `onOutputBufferAvailable` 里注入真实的
 * obtain/close/release，JVM 单测注入假实现即可验证「null 时 buffer 仍被归还」。
 */
internal object OutputBufferGuard {

    fun <T> process(
        obtainImage: () -> T?,
        closeImage: (T) -> Unit,
        releaseBuffer: () -> Unit,
        consumeImage: (T) -> Unit,
        onFailure: (Throwable) -> Unit
    ) {
        val image = runCatching { obtainImage() }.getOrNull()
        if (image == null) {
            // 判空分支（含 obtain 抛异常被吞成 null）：只还槽，没有 Image 可关。
            releaseBuffer()
            return
        }
        try {
            consumeImage(image)
        } catch (error: Throwable) {
            onFailure(error)
        } finally {
            // 审查项 3：Image 必须在 releaseOutputBuffer **之前**关闭；异常路径也必须
            // 还槽（审查项 2），所以两个调用都收敛到 finally，顺序固定为 close → release。
            closeImage(image)
            releaseBuffer()
        }
    }
}
