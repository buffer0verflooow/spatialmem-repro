package com.example.blindassist.link

/**
 * 线格式编解码。
 *
 * [LinkFrameReader] 是关键部分：TCP 是字节流，一个包可能被切成任意块到达，
 * 也可能一次读到好几个包。这里把「任意切分的字节流」还原为完整包，
 * 并对损坏流做出**明确失败**而不是静默解析出垃圾。
 */
object LinkProtocol {

    /** 把一个包编码成可直接写 socket 的字节数组。 */
    fun encode(packet: LinkPacket, maxPayloadBytes: Int = LinkWire.DEFAULT_MAX_PAYLOAD): ByteArray {
        val size = packet.payload.size
        if (size > maxPayloadBytes) {
            throw LinkProtocolException("payload $size 超过上限 $maxPayloadBytes")
        }
        if (size > LinkWire.ABSOLUTE_MAX_PAYLOAD) {
            throw LinkProtocolException("payload $size 超过线格式上限 ${LinkWire.ABSOLUTE_MAX_PAYLOAD}")
        }
        val out = ByteArray(LinkWire.HEADER_SIZE + size)
        out[0] = LinkWire.MAGIC_0
        out[1] = LinkWire.MAGIC_1
        out[2] = LinkWire.VERSION.toByte()
        out[3] = packet.channel.code.toByte()
        out[4] = packet.flags.toByte()
        out[5] = ((size ushr 16) and 0xFF).toByte()
        out[6] = ((size ushr 8) and 0xFF).toByte()
        out[7] = (size and 0xFF).toByte()
        writeUInt32(out, 8, packet.sequence)
        writeInt64(out, 12, packet.senderTimestampNs)
        packet.payload.copyInto(out, LinkWire.HEADER_SIZE)
        return out
    }

    internal fun writeUInt32(target: ByteArray, offset: Int, value: Long) {
        target[offset] = ((value ushr 24) and 0xFF).toByte()
        target[offset + 1] = ((value ushr 16) and 0xFF).toByte()
        target[offset + 2] = ((value ushr 8) and 0xFF).toByte()
        target[offset + 3] = (value and 0xFF).toByte()
    }

    internal fun readUInt32(source: ByteArray, offset: Int): Long {
        return ((source[offset].toLong() and 0xFF) shl 24) or
            ((source[offset + 1].toLong() and 0xFF) shl 16) or
            ((source[offset + 2].toLong() and 0xFF) shl 8) or
            (source[offset + 3].toLong() and 0xFF)
    }

    internal fun writeInt64(target: ByteArray, offset: Int, value: Long) {
        for (i in 0 until 8) {
            target[offset + i] = ((value ushr (56 - 8 * i)) and 0xFF).toByte()
        }
    }

    internal fun readInt64(source: ByteArray, offset: Int): Long {
        var result = 0L
        for (i in 0 until 8) {
            result = (result shl 8) or (source[offset + i].toLong() and 0xFF)
        }
        return result
    }
}

/**
 * 把 TCP 字节流还原成包。
 *
 * 用法：
 * ```
 * reader.append(buffer, 0, bytesRead)
 * while (true) {
 *     val packet = reader.next() ?: break
 *     handle(packet)
 * }
 * ```
 *
 * 失败语义：magic 错、版本不符、长度超限一律抛 [LinkProtocolException]，
 * 调用方应**断开连接并走重连**。不尝试在流里重新寻找 magic —— 一旦 TCP 流
 * 错位，重同步得到的很可能是「看起来合法但实际是负载中段」的伪包头，
 * 那比直接断开危险得多。
 */
class LinkFrameReader(
    private val maxPayloadBytes: Int = LinkWire.DEFAULT_MAX_PAYLOAD,
    initialCapacity: Int = 64 * 1024
) {
    private var buffer = ByteArray(initialCapacity.coerceAtLeast(LinkWire.HEADER_SIZE))
    private var readPos = 0
    private var writePos = 0

    /** 尚未消费的字节数。持续增长说明下游处理不过来，调用方应据此报警。 */
    fun pendingBytes(): Int = writePos - readPos

    fun append(data: ByteArray, offset: Int = 0, length: Int = data.size - offset) {
        require(offset >= 0 && length >= 0 && offset + length <= data.size) {
            "append 越界: offset=$offset length=$length size=${data.size}"
        }
        if (length == 0) return
        ensureCapacity(length)
        data.copyInto(buffer, writePos, offset, offset + length)
        writePos += length
    }

    /** 取下一个完整包；不足一个完整包时返回 null。 */
    fun next(): LinkPacket? {
        if (pendingBytes() < LinkWire.HEADER_SIZE) return null

        if (buffer[readPos] != LinkWire.MAGIC_0 || buffer[readPos + 1] != LinkWire.MAGIC_1) {
            throw LinkProtocolException(
                "magic 不匹配（0x%02x 0x%02x），流已错位，应断开重连".format(
                    buffer[readPos], buffer[readPos + 1]
                )
            )
        }
        val version = buffer[readPos + 2].toInt() and 0xFF
        if (version != LinkWire.VERSION) {
            throw LinkProtocolException("协议版本不符: 收到 $version，本端 ${LinkWire.VERSION}")
        }
        val channelCode = buffer[readPos + 3].toInt() and 0xFF
        val channel = LinkChannel.fromCode(channelCode)
            ?: throw LinkProtocolException("未知通道 0x%02x".format(channelCode))
        val flags = buffer[readPos + 4].toInt() and 0xFF
        val payloadSize = ((buffer[readPos + 5].toInt() and 0xFF) shl 16) or
            ((buffer[readPos + 6].toInt() and 0xFF) shl 8) or
            (buffer[readPos + 7].toInt() and 0xFF)
        if (payloadSize > maxPayloadBytes) {
            throw LinkProtocolException("payload 长度 $payloadSize 超过上限 $maxPayloadBytes")
        }

        // 头齐了但负载还没到齐，等下一次 append。
        if (pendingBytes() < LinkWire.HEADER_SIZE + payloadSize) return null

        val sequence = LinkProtocol.readUInt32(buffer, readPos + 8)
        val timestamp = LinkProtocol.readInt64(buffer, readPos + 12)
        val payloadStart = readPos + LinkWire.HEADER_SIZE
        val payload = buffer.copyOfRange(payloadStart, payloadStart + payloadSize)
        readPos = payloadStart + payloadSize
        compactIfNeeded()

        return LinkPacket(
            channel = channel,
            flags = flags,
            sequence = sequence,
            senderTimestampNs = timestamp,
            payload = payload
        )
    }

    private fun ensureCapacity(additional: Int) {
        if (writePos + additional <= buffer.size) return
        // 先尝试搬移已消费部分，多数情况下不用扩容。
        compact()
        if (writePos + additional <= buffer.size) return
        var newSize = buffer.size
        while (newSize < writePos + additional) {
            newSize = if (newSize > Int.MAX_VALUE / 2) Int.MAX_VALUE else newSize * 2
        }
        buffer = buffer.copyOf(newSize)
    }

    private fun compactIfNeeded() {
        // 读位置过半就搬移，避免长连接下 buffer 无限增长。
        if (readPos > buffer.size / 2) compact()
    }

    private fun compact() {
        if (readPos == 0) return
        val remaining = pendingBytes()
        if (remaining > 0) {
            buffer.copyInto(buffer, 0, readPos, writePos)
        }
        readPos = 0
        writePos = remaining
    }
}
