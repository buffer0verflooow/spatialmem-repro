#!/usr/bin/env python3
"""M1 链路的假手机端 —— 用于眼镜端（:glasses）单独验收，不依赖 :app。

用途：在没有手机端实现之前，验证眼镜端确实按 LinkProtocol 吐出合法的包序列。
它做三件事：
  1. 监听 TCP，按 20 字节头重组 LinkPacket；
  2. 收到 HELLO 回 HELLO_ACK（否则眼镜端握手超时、不会开始发视频）；
  3. 收到 PING 回 PONG，让时钟对齐能跑起来。

线格式与 link/src/main/java/com/example/blindassist/link/LinkProtocol.kt 一致：
    0-1 magic 'B''A' | 2 version | 3 channel | 4 flags
    5-7 payload len(3B) | 8-11 sequence(u32) | 12-19 senderTimestampNs(i64)

用法：
    python3 scripts/m1_mock_phone.py [--port 47810] [--seconds 30]

眼镜端走 USB 隧道时先建反向端口（可绕开「未佩戴→Wi-Fi 断」）：
    adb -s <serial> reverse tcp:47810 tcp:47810
    adb -s <serial> shell am start -n com.example.blindassist.glasses/.GlassLinkActivity --es host 127.0.0.1
"""

import argparse
import socket
import struct
import sys
import time
from collections import Counter

HEADER = 20
MAGIC = b"BA"
VERSION = 1

CHANNELS = {
    0x01: "VIDEO", 0x02: "AUDIO", 0x03: "IMU", 0x04: "POSE",
    0x05: "INPUT", 0x10: "CONTROL", 0x11: "SPEAK", 0x12: "SPEAK_STATUS",
}
FLAG_KEYFRAME = 0x01
FLAG_CODEC_CONFIG = 0x02

CTRL_HELLO, CTRL_HELLO_ACK, CTRL_HELLO_REJECT = 0x01, 0x02, 0x03
CTRL_PING, CTRL_PONG, CTRL_BYE = 0x04, 0x05, 0x06


def encode_packet(channel, flags, seq, ts_ns, payload):
    n = len(payload)
    return (
        MAGIC
        + bytes([VERSION, channel, flags, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])
        + struct.pack(">I", seq & 0xFFFFFFFF)
        + struct.pack(">q", ts_ns)
        + payload
    )


def parse_header(buf):
    if buf[0:2] != MAGIC:
        raise ValueError(f"magic 不匹配: {buf[0:2]!r}，流已错位")
    version = buf[2]
    if version != VERSION:
        raise ValueError(f"协议版本不符: {version}")
    channel = buf[3]
    flags = buf[4]
    n = (buf[5] << 16) | (buf[6] << 8) | buf[7]
    seq = struct.unpack(">I", buf[8:12])[0]
    ts = struct.unpack(">q", buf[12:20])[0]
    return channel, flags, n, seq, ts


def encode_pose_batch(samples):
    """把 POSE 采样批编码为 0x04 通道负载（工单 M1-05 §3.2，全大端）。

    samples: [{timestamp_ns, qx, qy, qz, qw, accuracy}, ...]，至少一个采样。
    """
    if not samples:
        raise ValueError("空采样批不允许（工单 M1-05：空批拒绝）")
    out = bytearray()
    out += struct.pack(">H", len(samples))
    for s in samples:
        out += struct.pack(">q", s["timestamp_ns"])
        out += struct.pack(">ffff", s["qx"], s["qy"], s["qz"], s["qw"])
        out += bytes([s["accuracy"] & 0xFF])
    return bytes(out)


def decode_pose_batch(payload):
    """解码 0x04 通道负载，返回 [{"timestamp_ns", "qx", "qy", "qz", "qw", "accuracy"}, ...]。

    长度与 sampleCount 不符（截断/多余）或空批一律抛 ValueError，
    与 :link PosePayloadCodec 的失败语义一致。
    """
    if len(payload) < 2:
        raise ValueError(f"POSE 负载过短：至少需要 2 字节的 sampleCount，实际 {len(payload)}")
    count = struct.unpack(">H", payload[:2])[0]
    if count == 0:
        raise ValueError("空采样批不允许（工单 M1-05：空批拒绝）")
    sample_bytes = 8 + 4 * 4 + 1
    expected = 2 + count * sample_bytes
    if len(payload) != expected:
        raise ValueError(
            f"POSE 负载长度与 sampleCount={count} 不符：应为 {expected} 字节，实际 {len(payload)}"
        )
    samples = []
    for i in range(count):
        off = 2 + i * sample_bytes
        ts, qx, qy, qz, qw = struct.unpack(">qffff", payload[off:off + 24])
        samples.append({
            "timestamp_ns": ts,
            "qx": qx, "qy": qy, "qz": qz, "qw": qw,
            "accuracy": payload[off + 24],
        })
    return samples


class Reader:
    """按字段读 CONTROL 报文体；长度不足即报错（与 ControlCodec 的失败语义一致）。"""

    def __init__(self, data):
        self.d, self.i = data, 0

    def need(self, n, what):
        if len(self.d) - self.i < n:
            raise ValueError(f"CONTROL 报文被截断：{what} 需要 {n} 字节，剩余 {len(self.d)-self.i}")

    def u8(self, what="u8"):
        self.need(1, what); v = self.d[self.i]; self.i += 1; return v

    def u16(self, what="u16"):
        self.need(2, what); v = struct.unpack(">H", self.d[self.i:self.i+2])[0]; self.i += 2; return v

    def u32(self, what="u32"):
        self.need(4, what); v = struct.unpack(">I", self.d[self.i:self.i+4])[0]; self.i += 4; return v

    def i64(self, what="i64"):
        self.need(8, what); v = struct.unpack(">q", self.d[self.i:self.i+8])[0]; self.i += 8; return v

    def s(self, what="str"):
        n = self.u16(what + " 长度"); self.need(n, what)
        v = self.d[self.i:self.i+n].decode("utf-8"); self.i += n; return v


def decode_hello(body):
    r = Reader(body)
    caps = {
        "protocolVersion": r.u32("protocolVersion"),
        "deviceModel": r.s("deviceModel"),
    }
    n = r.u16("videoModes 个数")
    caps["videoModes"] = [(r.u32(), r.u32(), r.u32()) for _ in range(n)]
    for k in ("hasHardwareAvcEncoder", "hasLocalChineseTts", "hasRotationVector",
              "hasSixDof", "hasTempleTouch", "hasWearDetection"):
        caps[k] = bool(r.u8(k))
    caps["sensorTimestampSource"] = ["UNKNOWN", "REALTIME"][r.u8("sensorTimestampSource")]
    caps["sensorOrientationDegrees"] = r.u32("sensorOrientationDegrees")
    return caps


def encode_hello_ack(w, h, fps, bitrate, channels, speak_path, hb_ms, sync_ms):
    b = bytearray([CTRL_HELLO_ACK])
    b += struct.pack(">IIII", w, h, fps, bitrate)
    b += struct.pack(">H", len(channels))
    b += bytes(channels)
    b += bytes([speak_path])
    b += struct.pack(">II", hb_ms, sync_ms)
    return bytes(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--dump", help="把 VIDEO 负载按顺序写到该文件（裸 H.264 Annex-B）")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    srv.settimeout(args.seconds)
    print(f"[mock-phone] 监听 0.0.0.0:{args.port}，等待眼镜端连接…", flush=True)

    try:
        conn, addr = srv.accept()
    except socket.timeout:
        print("[mock-phone] 超时：没有任何连接进来", flush=True)
        return 1
    print(f"[mock-phone] 已连接 {addr}", flush=True)

    dump = open(args.dump, "wb") if args.dump else None
    conn.settimeout(1.0)
    buf = bytearray()
    stats = Counter()
    first_video_ts = last_video_ts = None
    saw = {"hello": False, "codec_config": False, "keyframe": False}
    ctrl_seq = 0
    deadline = time.time() + args.seconds
    # 眼镜端有读超时：握手后若对端长时间无任何下行，会判定失联并重连。
    # 真手机端靠周期 PING 维持，这里照做，同时顺带把时钟对齐链路跑起来。
    next_ping = time.time() + 1.0
    rtts = []

    try:
        while time.time() < deadline:
            if time.time() >= next_ping:
                ctrl_seq += 1
                t1 = time.monotonic_ns()
                conn.sendall(encode_packet(0x10, 0, ctrl_seq, t1,
                                           bytes([CTRL_PING]) + struct.pack(">q", t1)))
                next_ping = time.time() + 1.0
            try:
                chunk = conn.recv(65536)
                if not chunk:
                    print("[mock-phone] 对端关闭连接", flush=True)
                    break
                buf += chunk
            except socket.timeout:
                continue

            while len(buf) >= HEADER:
                channel, flags, n, seq, ts = parse_header(buf)
                if len(buf) < HEADER + n:
                    break
                payload = bytes(buf[HEADER:HEADER + n])
                del buf[:HEADER + n]

                name = CHANNELS.get(channel, f"0x{channel:02x}")
                stats[name] += 1

                if channel == 0x10:  # CONTROL
                    t = payload[0] if payload else -1
                    if t == CTRL_HELLO:
                        saw["hello"] = True
                        caps = decode_hello(payload[1:])
                        print(f"[mock-phone] ← HELLO  device={caps['deviceModel']} "
                              f"timestampSource={caps['sensorTimestampSource']} "
                              f"orientation={caps['sensorOrientationDegrees']} "
                              f"videoModes={len(caps['videoModes'])} "
                              f"tts={caps['hasLocalChineseTts']}", flush=True)
                        ack = encode_hello_ack(640, 360, 15, 1_200_000,
                                               [0x01, 0x10, 0x11, 0x12], 1, 1000, 30000)
                        ctrl_seq += 1
                        conn.sendall(encode_packet(0x10, 0, ctrl_seq,
                                                   time.monotonic_ns(), ack))
                        print("[mock-phone] → HELLO_ACK 640x360@15", flush=True)
                    elif t == CTRL_PONG:
                        r = Reader(payload[1:])
                        t1, t2, t3 = r.i64(), r.i64(), r.i64()
                        t4 = time.monotonic_ns()
                        rtt = (t4 - t1) - (t3 - t2)
                        offset = ((t2 - t1) + (t3 - t4)) // 2
                        rtts.append(rtt)
                        print(f"[mock-phone] ← PONG rtt={rtt/1e6:.2f}ms "
                              f"offset={offset/1e6:.1f}ms 上界={rtt/2e6:.2f}ms", flush=True)
                    elif t == CTRL_BYE:
                        print(f"[mock-phone] ← BYE {Reader(payload[1:]).s()}", flush=True)
                elif channel == 0x01:  # VIDEO
                    if flags & FLAG_CODEC_CONFIG:
                        saw["codec_config"] = True
                        print(f"[mock-phone] ← CODEC_CONFIG {n} 字节 "
                              f"(SPS/PPS: {payload[:8].hex()}…)", flush=True)
                    if flags & FLAG_KEYFRAME:
                        saw["keyframe"] = True
                        stats["_keyframe"] += 1
                    if first_video_ts is None:
                        first_video_ts = ts
                    last_video_ts = ts
                    if dump:
                        dump.write(payload)
    finally:
        conn.close()
        srv.close()
        if dump:
            dump.close()

    print("\n===== 判读 =====", flush=True)
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    if first_video_ts and last_video_ts and last_video_ts > first_video_ts:
        span = (last_video_ts - first_video_ts) / 1e9
        print(f"  视频时间跨度 {span:.2f}s，实测帧率 {stats['VIDEO']/span:.2f} FPS")
    if rtts:
        best = min(rtts)
        print(f"  时钟对齐样本 {len(rtts)} 个，最优 rtt {best/1e6:.2f}ms，"
              f"误差上界 {best/2e6:.2f}ms（F6-9 要求 ≤50ms）")
    ok = saw["hello"] and saw["codec_config"] and saw["keyframe"] and stats["VIDEO"] > 0
    print(f"  HELLO={saw['hello']} CODEC_CONFIG={saw['codec_config']} "
          f"KEYFRAME={saw['keyframe']} VIDEO={stats['VIDEO']}")
    print("  结论:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
