package com.example.blindassist.link.transport

import android.util.Log
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.FileWriter
import java.util.concurrent.ArrayBlockingQueue
import com.example.blindassist.link.ClockSyncEstimator
import com.example.blindassist.link.ControlMessage
import com.example.blindassist.link.GlassCapabilities
import com.example.blindassist.link.LinkPacket
import com.example.blindassist.link.LinkFlags
import com.example.blindassist.link.SessionConfig
import com.example.blindassist.link.StaleFrameGate
import com.example.blindassist.source.SourceState
import com.example.blindassist.source.VideoFrame
import com.example.blindassist.source.VideoSource
import com.example.blindassist.source.VideoSourceRequest
import com.example.blindassist.util.MonotonicClock
import java.util.LinkedHashMap
import java.util.concurrent.atomic.AtomicLong

/**
 * 手机端 X3Pro 视频源（工单 M1-04 交付物），实现现有 [VideoSource] 契约。
 *
 * 管线：GlassLinkServer（TCP 接收 + 握手 + 周期 PING）→ H264Decoder（MediaCodec）
 * → YuvToRgba → StaleFrameGate → [VideoFrame]（onFrame）。
 *
 * 六条硬约束的落点：
 * 1. 周期 PING：GlassLinkServer 握手成功后启动（每 heartbeatIntervalMs 一次）；
 * 2. YUV 布局分派 + rowStride：YuvToRgba（I420 / NV12 / NV21 + 行填充）；
 * 3. captureTimestampNs 一律经 ClockSyncEstimator 换算；未收敛时 [StaleFrameGate]
 *    走 ARRIVAL_LOWER_BOUND（到达时间下界），帧时间戳用到达时间，**绝不用
 *    眼镜时间戳当本机时间**（否则 captureToEventLatencyMs 会等于两机开机时间差）；
 * 4. [StaleFrameGate.evaluate] 在 onFrame 之前调用，过期帧不进下游；
 * 5. rotationDegrees 取自 HELLO（GlassCapabilities.sensorOrientationDegrees），不写死 0；
 * 6. 每次连接收到新 CODEC_CONFIG 时重建解码器（不缓存 CSD、不复用旧解码器）。
 *
 * 本单不接进 CaptureCoordinator（工单 6 的事）；[VideoSourceRequest.videoOutputFile]
 * 暂不使用，只做可独立启停、独立验证的数据源。onFrame 在解码线程回调。
 */
class X3ProVideoSource(
    private val port: Int = GlassLinkServer.DEFAULT_PORT,
    private val clock: MonotonicClock = MonotonicClock.SYSTEM,
    /** 高画质录制提供者：每次 HELLO 握手时读取（默认关，保持实时链路稳定档）。 */
    private val highQualityRecording: () -> Boolean = { false }
) : VideoSource, GlassLinkServer.Listener {

    private val stateLock = Any()

    /**
     * ClockSyncEstimator 非线程安全，而本类里样本由会话读线程写入（PONG）、
     * 查询由解码线程执行（toReceiverNs），所以一切访问都在这把锁下串行化。
     * 锁序约定：stateLock → estimatorLock，全类一致。
     */
    private val estimatorLock = Any()
    private val estimator = ClockSyncEstimator()
    private val staleGate = StaleFrameGate()
    private val frameIndex = AtomicLong(0)

    @Volatile private var running = false
    private var server: GlassLinkServer? = null

    private var onFrameCallback: ((VideoFrame) -> Unit)? = null
    private var onStateCallback: ((SourceState, String) -> Unit)? = null
    private var onErrorCallback: ((Throwable) -> Unit)? = null
    @Volatile private var onAudioPacketCallback: ((LinkPacket, Long) -> Unit)? = null

    /** 供 CaptureCoordinator 把眼镜音频路由给 [GlassesAudioSource]。 */
    fun setOnAudioPacketCallback(callback: (LinkPacket, Long) -> Unit) {
        onAudioPacketCallback = callback
    }

    // ---- 会话状态（stateLock 保护；会话读线程写、解码线程读）----
    private var decoder: H264Decoder? = null
    private var rotationDegrees = 0
    private var negotiatedWidth = X3SessionNegotiator.DEFAULT_VIDEO_WIDTH
    private var negotiatedHeight = X3SessionNegotiator.DEFAULT_VIDEO_HEIGHT

    /** ptsUs → arrivalNs：把解码输出的帧与它到达本机的时刻关联起来。 */
    private val arrivalByPts = LinkedHashMap<Long, Long>()

    @Volatile private var convergedReported = false

    // ---- H.264 tee（SpatialMem 复现临时取数，零转码落盘）----
    private data class TeeItem(
        val flags: Int,
        val senderNs: Long,
        val arrivalNs: Long,
        val payload: ByteArray,
    )

    @Volatile private var teeEnabled = false
    @Volatile private var teeH264File: File? = null
    @Volatile private var teeTimelineFile: File? = null
    private val teeQueue = ArrayBlockingQueue<TeeItem>(TEE_QUEUE_CAPACITY)
    private var h264Out: BufferedOutputStream? = null
    private var timelineOut: FileWriter? = null
    private var teeFrameIndex = 0L
    private var teeFlushCounter = 0

    private val teeThread = Thread(::teeLoop, "h264-tee").apply { isDaemon = true; start() }

    private fun teeLoop() {
        while (true) {
            val item = try {
                teeQueue.take()
            } catch (e: InterruptedException) {
                return
            }
            if (item.flags == TEE_CLOSE_FLAG) {
                closeTeeWriters()
                continue
            }
            try {
                writeTeeItem(item)
            } catch (t: Throwable) {
                Log.w(TAG, "H.264 tee 写盘失败", t)
            }
        }
    }

    private fun openTeeWriters(): BufferedOutputStream? {
        val h264File = teeH264File ?: return null
        return try {
            h264File.parentFile?.mkdirs()
            val out = BufferedOutputStream(FileOutputStream(h264File), 64 * 1024)
            h264Out = out
            teeTimelineFile?.let { tl ->
                tl.parentFile?.mkdirs()
                timelineOut = FileWriter(tl).apply {
                    write("frame_index,sender_ts_ns,arrival_ns,flags,bytes\n")
                }
            }
            out
        } catch (t: Throwable) {
            Log.w(TAG, "打开 H.264 tee 文件失败", t)
            null
        }
    }

    private fun writeTeeItem(item: TeeItem) {
        val h264 = h264Out ?: openTeeWriters() ?: return
        h264.write(item.payload)
        timelineOut?.write(
            "${teeFrameIndex++},${item.senderNs},${item.arrivalNs},${item.flags},${item.payload.size}\n"
        )
        if (++teeFlushCounter >= TEE_FLUSH_EVERY || LinkFlags.has(item.flags, LinkFlags.KEYFRAME)) {
            h264.flush()
            timelineOut?.flush()
            teeFlushCounter = 0
        }
    }

    private fun closeTeeWriters() {
        try {
            h264Out?.flush()
            timelineOut?.flush()
        } catch (_: Throwable) {
        }
        try {
            h264Out?.close()
        } catch (_: Throwable) {
        }
        try {
            timelineOut?.close()
        } catch (_: Throwable) {
        }
        h264Out = null
        timelineOut = null
    }

    private fun teeEnqueue(packet: LinkPacket, arrivalNs: Long) {
        if (!teeEnabled) return
        val essential = packet.isCodecConfig || packet.isKeyframe
        val item = TeeItem(packet.flags, packet.senderTimestampNs, arrivalNs, packet.payload)
        if (essential) {
            teeQueue.put(item)
        } else if (!teeQueue.offer(item)) {
            Log.d(TAG, "H.264 tee 队列满，丢弃非关键帧")
        }
    }

    override fun start(
        request: VideoSourceRequest,
        onFrame: (VideoFrame) -> Unit,
        onState: (SourceState, String) -> Unit,
        onError: (Throwable) -> Unit
    ) {
        synchronized(stateLock) {
            if (running) return
            running = true
            teeH264File = request.h264TeeFile
            teeTimelineFile = request.videoTimelineFile
            teeEnabled = request.h264TeeFile != null
            onFrameCallback = onFrame
            onStateCallback = onState
            onErrorCallback = onError
        }
        teeFrameIndex = 0
        teeFlushCounter = 0
        staleGate.reset()
        frameIndex.set(0)
        emitState(SourceState.STARTING, "X3Pro 视频源启动中")
        val newServer = GlassLinkServer(port, clock, this, highQualityRecording)
        server = newServer
        newServer.start()
    }

    override fun stop() {
        val wasRunning: Boolean
        synchronized(stateLock) {
            wasRunning = running
            running = false
        }
        if (!wasRunning) return
        teeEnabled = false
        try {
            teeQueue.put(TeeItem(TEE_CLOSE_FLAG, 0L, 0L, ByteArray(0)))
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
        val active = server
        server = null
        active?.stop()
        releaseDecoderLocked()
        emitState(SourceState.IDLE, "X3Pro 视频源已停止")
    }

    // ------------------------------------------------------------------
    // GlassLinkServer.Listener
    // ------------------------------------------------------------------

    override fun onAudioPacket(packet: LinkPacket, arrivalNs: Long) {
        onAudioPacketCallback?.invoke(packet, arrivalNs)
    }

    override fun onState(message: String) {
        if (running) emitState(SourceState.RUNNING, message)
    }

    override fun onServerError(error: Throwable) {
        if (!running) return
        emitState(SourceState.ERROR, "链路服务失败：${error.message ?: error.javaClass.simpleName}")
        onErrorCallback?.invoke(error)
        // 监听失败无法自愈，停止源（幂等）。
        synchronized(stateLock) {
            running = false
            server = null
        }
    }

    override fun onHandshake(capabilities: GlassCapabilities, config: SessionConfig) {
        synchronized(stateLock) {
            rotationDegrees = capabilities.sensorOrientationDegrees
            negotiatedWidth = config.videoWidth
            negotiatedHeight = config.videoHeight
            // 每次重连时钟域可能跳变：旧样本作废（ClockSyncEstimator 文档要求）。
            synchronized(estimatorLock) { estimator.reset() }
            convergedReported = false
            releaseDecoderLocked()
            arrivalByPts.clear()
        }
        val speakPathName = if (capabilities.hasLocalChineseTts) "本地 TTS" else "预置音频"
        emitState(
            SourceState.RUNNING,
            "眼镜已连接（${capabilities.deviceModel}），协商 ${config.videoWidth}x${config.videoHeight}" +
                "@${config.videoFps}，旋转 ${rotationDegrees}°，播报路径：$speakPathName"
        )
    }

    override fun onControl(message: ControlMessage, localRecvNs: Long) {
        if (message !is ControlMessage.Pong) return
        val accepted = synchronized(estimatorLock) {
            estimator.addSample(message.t1, message.t2, message.t3, localRecvNs)
        }
        if (!accepted) return
        val converged = synchronized(estimatorLock) { estimator.isConverged() }
        if (converged && !convergedReported) {
            convergedReported = true
            val uncertaintyMs = synchronized(estimatorLock) {
                estimator.uncertaintyNs()?.let { it / 1_000_000.0 }
            }
            emitState(
                SourceState.RUNNING,
                "时钟对齐已收敛" + (uncertaintyMs?.let { "（误差上界 %.1f ms）".format(it) } ?: "")
            )
        }
    }

    override fun onVideoPacket(packet: LinkPacket, arrivalNs: Long) {
        if (!running) return
        teeEnqueue(packet, arrivalNs)
        if (packet.isCodecConfig) {
            // 约束 6：每次连接用本次收到的 CSD 重建解码器，绝不跨连接复用/缓存。
            synchronized(stateLock) {
                if (!running) return
                releaseDecoderLocked()
                decoder = createDecoderLocked(packet.payload)
            }
            return
        }
        val target = synchronized(stateLock) {
            if (!running) return
            decoder
        }
        if (target == null) {
            // CODEC_CONFIG 之前的数据帧：还没有可配置的解码器，丢弃。
            Log.d(TAG, "收到 CODEC_CONFIG 之前的 VIDEO 包，丢弃")
            return
        }
        synchronized(stateLock) {
            arrivalByPts[packet.senderTimestampNs / 1000L] = arrivalNs
            pruneArrivalMapLocked()
        }
        target.queueAccessUnit(packet.payload, packet.senderTimestampNs / 1000L)
    }

    override fun onSessionEnded(reason: String) {
        synchronized(stateLock) {
            releaseDecoderLocked()
            arrivalByPts.clear()
        }
        if (running) {
            emitState(SourceState.RUNNING, "会话结束：$reason（继续监听，等待眼镜重连）")
        }
    }

    // ------------------------------------------------------------------
    // 解码线程回调
    // ------------------------------------------------------------------

    private fun onDecodedFrame(frame: DecodedVideoFrame) {
        if (!running) return
        val arrivalNs: Long
        val rotation: Int
        synchronized(stateLock) {
            arrivalNs = arrivalByPts.remove(frame.presentationTimeUs) ?: clock.nowNs()
            rotation = rotationDegrees
            pruneArrivalMapLocked()
        }
        val nowNs = clock.nowNs()
        // 约束 3：未收敛时 toReceiverNs 返回 null —— 此时年龄只能用到达时间下界，
        // 绝对时间换算绝不在本类任何路径上发生。
        val capturePhoneNs = synchronized(estimatorLock) {
            estimator.toReceiverNs(frame.presentationTimeUs * 1000L)
        }
        // 约束 4：StaleFrameGate 接在 onFrame 之前 —— 过期帧不进下游。
        val decision = staleGate.evaluate(capturePhoneNs, arrivalNs, nowNs)
        if (!decision.accepted) {
            if (decision.basis == StaleFrameGate.AgeBasis.SYNCED_CAPTURE &&
                staleGate.droppedCount % DROP_LOG_INTERVAL == 1L
            ) {
                Log.w(TAG, "丢弃过期帧：age=%.1f ms".format(decision.ageMs))
            }
            return
        }
        val callback = onFrameCallback ?: return
        callback(
            VideoFrame(
                frameIndex = frameIndex.getAndIncrement(),
                // 未收敛时用到达时间：合法的本机单调时钟值（略滞后），
                // 而绝不会是两台设备开机时间差量级的无意义值。
                captureTimestampNs = capturePhoneNs ?: arrivalNs,
                width = frame.width,
                height = frame.height,
                rotationDegrees = rotation,
                rgba8888 = frame.rgba8888
            )
        )
    }

    private fun onDecoderError(error: Throwable) {
        if (!running) return
        emitState(SourceState.RUNNING, "解码错误：${error.message ?: error.javaClass.simpleName}")
        onErrorCallback?.invoke(error)
        // 源保持运行：下一次连接（CODEC_CONFIG）会重建解码器。
    }

    // ------------------------------------------------------------------
    // 内部
    // ------------------------------------------------------------------

    private fun createDecoderLocked(csd: ByteArray): H264Decoder {
        return H264Decoder(
            codecConfig = csd,
            widthHint = negotiatedWidth,
            heightHint = negotiatedHeight,
            onFrame = ::onDecodedFrame,
            onError = ::onDecoderError
        )
    }

    private fun releaseDecoderLocked() {
        val old = decoder
        decoder = null
        old?.release()
    }

    /** 按插入顺序淘汰最旧关联，防止 PTS 关联表在断流场景下无界增长。 */
    private fun pruneArrivalMapLocked() {
        while (arrivalByPts.size > MAX_PENDING_ARRIVALS) {
            val iterator = arrivalByPts.entries.iterator()
            iterator.next()
            iterator.remove()
        }
    }

    private fun emitState(state: SourceState, message: String) {
        onStateCallback?.invoke(state, message)
    }

    companion object {
        private const val TAG = "X3ProVideoSource"
        private const val MAX_PENDING_ARRIVALS = 512
        private const val DROP_LOG_INTERVAL = 60L
        private const val TEE_CLOSE_FLAG = -1
        private const val TEE_QUEUE_CAPACITY = 128
        private const val TEE_FLUSH_EVERY = 30
    }
}
