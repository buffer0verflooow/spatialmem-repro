package com.example.blindassist.link.transport

import android.util.Log
import com.example.blindassist.link.ControlCodec
import com.example.blindassist.link.ControlMessage
import com.example.blindassist.link.GlassCapabilities
import com.example.blindassist.link.LinkChannel
import com.example.blindassist.link.LinkFlags
import com.example.blindassist.link.LinkFrameReader
import com.example.blindassist.link.LinkPacket
import com.example.blindassist.link.LinkProtocol
import com.example.blindassist.link.LinkProtocolException
import com.example.blindassist.link.SessionConfig
import com.example.blindassist.util.MonotonicClock
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * 手机端链路服务端：`ServerSocket` 监听固定端口 47810，**单连接**。
 *
 * 职责（工单 M1-04 第 3 节）：
 * - `LinkFrameReader` 把 TCP 字节流重组为 [LinkPacket]，按 [LinkChannel] 分发；
 * - CONTROL：收 HELLO → [X3SessionNegotiator] 组 [SessionConfig] → 回 HELLO_ACK →
 *   **握手成功后立即启动周期 PING**（约束 1：漏发会让眼镜端读超时断链）；
 *   PONG 把四个时间戳交给监听器喂 `ClockSyncEstimator`；
 * - VIDEO：原样转交监听器（解码器由 X3ProVideoSource 按 CODEC_CONFIG 重建，约束 6）。
 *
 * 行为与 `scripts/m1_mock_phone.py` 逐行对照（握手与 CONTROL 部分以它为准）：
 * 每 1 秒发一个 PING（`[CTRL_PING] + i64 t1`），PONG 回显 t1/t2/t3。
 *
 * 单连接语义：活动会话期间到达的新连接直接关闭；会话结束后继续 accept，
 * 因此眼镜端重连（每次握手重发 CODEC_CONFIG）天然得到支撑。
 *
 * 线程模型：accept 线程一个；每个会话一个读线程（同时负责 CONTROL 写）；
 * 周期 PING 一个守护线程。
 */
class GlassLinkServer(
    private val port: Int = DEFAULT_PORT,
    private val clock: MonotonicClock = MonotonicClock.SYSTEM,
    private val listener: Listener,
    /** 高画质录制提供者：每次 HELLO 握手时读取，协商 720p@10/1.8Mbps（默认关）。 */
    private val highQualityRecording: () -> Boolean = { false }
) {

    interface Listener {
        /** 非致命状态消息（监听就绪、会话建立、会话结束等）。 */
        fun onState(message: String) = Unit

        /** 监听/绑定等致命错误；调用方应停止数据源。 */
        fun onServerError(error: Throwable) = Unit

        /** 握手完成：携带眼镜能力与协商出的会话配置。 */
        fun onHandshake(capabilities: GlassCapabilities, config: SessionConfig) = Unit

        /** CONTROL 消息（PONG 等），[localRecvNs] 是本机收到时刻。 */
        fun onControl(message: ControlMessage, localRecvNs: Long) = Unit

        /** VIDEO 包，[arrivalNs] 是本机收到时刻（时钟未收敛时的年龄下界基准）。 */
        fun onVideoPacket(packet: LinkPacket, arrivalNs: Long) = Unit

        /** AUDIO (0x02) 包（眼镜麦克风 PCM16 上行），[arrivalNs] 是本机收到时刻。 */
        fun onAudioPacket(packet: LinkPacket, arrivalNs: Long) = Unit

        /** 会话结束（正常关闭/协议错误/服务停止），调用方可释放会话资源。 */
        fun onSessionEnded(reason: String) = Unit
    }

    @Volatile private var running = false
    private var serverSocket: ServerSocket? = null
    private var acceptThread: Thread? = null
    @Volatile private var session: Session? = null

    fun start() {
        if (running) return
        running = true
        acceptThread = Thread({ acceptLoop() }, "glass-link-server").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        if (!running) return
        running = false
        val active = session
        session = null
        active?.close()
        runCatching { serverSocket?.close() }
        acceptThread?.interrupt()
    }

    private fun acceptLoop() {
        val serverSocket = ServerSocket()
        try {
            serverSocket.reuseAddress = true
            serverSocket.bind(InetSocketAddress(port))
            this.serverSocket = serverSocket
            listener.onState("已监听 0.0.0.0:$port，等待眼镜连接")
            while (running) {
                val connection = serverSocket.accept()
                if (session != null) {
                    // 单连接：活动会话期间拒绝新连接。
                    Log.d(TAG, "已有活动会话，拒绝新连接")
                    runCatching { connection.close() }
                    continue
                }
                val newSession = Session(connection)
                session = newSession
                Thread({ newSession.runSession() }, "glass-link-session").apply {
                    isDaemon = true
                    start()
                }
            }
        } catch (error: Exception) {
            if (running) {
                listener.onState("监听失败：${error.message}")
                listener.onServerError(error)
            }
        } finally {
            runCatching { serverSocket.close() }
            this.serverSocket = null
        }
    }

    /** 一个眼镜连接会话：读循环 + 握手 + 周期 PING。 */
    private inner class Session(private val socket: Socket) {
        private val reader = LinkFrameReader()
        private val writeLock = Any()
        private var controlSequence = 0L
        private var heartbeatIntervalMs = X3SessionNegotiator.DEFAULT_HEARTBEAT_INTERVAL_MS
        @Volatile private var alive = true
        @Volatile private var handshakeDone = false
        private var pingThread: Thread? = null
        private var endReason = "会话结束"

        fun runSession() {
            var reason = "会话结束"
            try {
                socket.tcpNoDelay = true
                // 眼镜端 15fps 推流 + 每秒 PONG，10s 无任何数据必然是死链。
                socket.soTimeout = READ_TIMEOUT_MS
                val input = socket.getInputStream()
                val buffer = ByteArray(READ_BUFFER_BYTES)
                while (alive && running) {
                    val n = try {
                        input.read(buffer)
                    } catch (error: SocketTimeoutException) {
                        reason = "接收超时（${READ_TIMEOUT_MS}ms 无任何数据）"
                        break
                    }
                    if (n < 0) {
                        reason = "眼镜端关闭连接"
                        break
                    }
                    reader.append(buffer, 0, n)
                    var protocolError: LinkProtocolException? = null
                    while (alive && running) {
                        val packet = try {
                            reader.next()
                        } catch (error: LinkProtocolException) {
                            protocolError = error
                            break
                        } ?: break
                        handlePacket(packet)
                    }
                    if (protocolError != null) {
                        reason = "链路协议错误：${protocolError.message}"
                        break
                    }
                }
            } catch (error: Exception) {
                reason = when {
                    alive && running -> "接收异常：${error.message}"
                    endReason != "会话结束" -> endReason
                    else -> "服务停止"
                }
            } finally {
                close()
                if (this@GlassLinkServer.session === this) {
                    this@GlassLinkServer.session = null
                }
                listener.onSessionEnded(reason)
            }
        }

        private fun handlePacket(packet: LinkPacket) {
            val arrivalNs = clock.nowNs()
            when (packet.channel) {
                LinkChannel.CONTROL -> handleControl(packet, arrivalNs)
                LinkChannel.VIDEO -> listener.onVideoPacket(packet, arrivalNs)
                LinkChannel.AUDIO -> listener.onAudioPacket(packet, arrivalNs)
                else -> Log.d(TAG, "忽略通道 ${packet.channel}（${packet.payload.size} 字节）")
            }
        }

        private fun handleControl(packet: LinkPacket, arrivalNs: Long) {
            // 解码失败抛 LinkProtocolException → 上层按协议错误断开会话。
            val message = ControlCodec.decode(packet.payload)
            when (message) {
                is ControlMessage.Hello -> {
                    if (handshakeDone) {
                        Log.d(TAG, "收到重复 HELLO，忽略")
                        return
                    }
                    val capabilities = message.capabilities
                    val config = X3SessionNegotiator.negotiate(
                        capabilities,
                        highQuality = highQualityRecording()
                    )
                    sendControl(ControlMessage.HelloAck(config))
                    handshakeDone = true
                    heartbeatIntervalMs = config.heartbeatIntervalMs
                    listener.onHandshake(capabilities, config)
                    // 约束 1：握手完成后立即启动周期 PING（保活 + 时钟对齐）。
                    startPingLoop()
                }
                is ControlMessage.Pong -> listener.onControl(message, arrivalNs)
                is ControlMessage.Bye -> {
                    endReason = "收到 BYE：${message.reason}"
                    listener.onState(endReason)
                    alive = false
                    runCatching { socket.close() }
                }
                is ControlMessage.HelloAck,
                is ControlMessage.HelloReject,
                is ControlMessage.Ping -> {
                    Log.d(TAG, "收到意外 CONTROL 报文 ${message::class.simpleName}，忽略")
                }
            }
        }

        private fun sendControl(message: ControlMessage) {
            val payload = ControlCodec.encode(message)
            val packet = LinkPacket(
                channel = LinkChannel.CONTROL,
                flags = LinkFlags.NONE,
                sequence = nextControlSequence(),
                senderTimestampNs = clock.nowNs(),
                payload = payload
            )
            sendPacket(packet)
        }

        private fun sendPacket(packet: LinkPacket) {
            val bytes = LinkProtocol.encode(packet)
            // HELLO_ACK（读线程）与 PING（ping 线程）可能同时写 socket。
            synchronized(writeLock) {
                val output = socket.getOutputStream()
                output.write(bytes)
                output.flush()
            }
        }

        private fun nextControlSequence(): Long {
            val current = controlSequence
            controlSequence = (controlSequence + 1) and 0xFFFF_FFFFL
            return current
        }

        /**
         * 约束 1 的落点：周期 PING 同时承担保活与时钟对齐两个职责。
         * 间隔取协商的 heartbeatIntervalMs（默认 1000ms，与 mock 一致）；
         * 500ms 下限防止配置错误导致眼镜端 5s 读超时。
         */
        private fun startPingLoop() {
            val intervalMs = heartbeatIntervalMs.coerceAtLeast(MIN_PING_INTERVAL_MS)
            pingThread = Thread({
                while (alive && running) {
                    try {
                        Thread.sleep(intervalMs)
                    } catch (error: InterruptedException) {
                        Thread.currentThread().interrupt()
                        break
                    }
                    if (!alive || !running) break
                    try {
                        sendControl(ControlMessage.Ping(clock.nowNs()))
                    } catch (error: Exception) {
                        Log.w(TAG, "发送 PING 失败：${error.message}")
                        break
                    }
                }
            }, "glass-link-ping").apply {
                isDaemon = true
                start()
            }
        }

        fun close() {
            alive = false
            pingThread?.interrupt()
            runCatching { socket.close() }
        }
    }

    companion object {
        private const val TAG = "GlassLinkServer"
        const val DEFAULT_PORT = 47810
        private const val READ_TIMEOUT_MS = 10_000
        private const val READ_BUFFER_BYTES = 64 * 1024
        private const val MIN_PING_INTERVAL_MS = 500L
    }
}
