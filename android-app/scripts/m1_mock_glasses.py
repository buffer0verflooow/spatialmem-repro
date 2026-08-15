#!/usr/bin/env python3
"""M1 链路的假眼镜端 —— 用于验收手机端（:app 的 X3ProVideoSource），不依赖真眼镜。

为什么要它：真眼镜的 Wi-Fi 只在佩戴时才起来（未佩戴时 ConnectivityService
里一个网络都没有），而手机端解码逻辑的验收不该被"必须戴着眼镜"卡住。
这个脚本回放**真实录制的眼镜 H.264 码流**，按 LinkProtocol 打包推给手机端，
输入确定、可重复，比用真眼镜更适合做回归。

它按真眼镜的实测行为工作：
  1. 连上后立刻发 HELLO，能力值与真机一致
     （REALTIME / orientation=90 / 640x360 / hasLocalChineseTts=false）；
  2. 等 HELLO_ACK；
  3. 先发一个 CODEC_CONFIG 包（SPS+PPS），再按 15 FPS 逐帧发 VIDEO 包，
     IDR 帧打 KEYFRAME 标志；
  4. 收到 PING 就回 PONG（否则手机端的时钟对齐跑不起来）。

用法（手机经 adb forward 暴露到本机 47810）：
    adb -s <phone> forward tcp:47810 tcp:47810
    python3 scripts/m1_mock_glasses.py --stream /tmp/glass2.h264 --seconds 60

线格式与 link/.../LinkProtocol.kt 一致，详见 m1_mock_phone.py 的注释。
"""

import argparse
import socket
import struct
import sys
import time

HEADER = 20
MAGIC = b"BA"
VERSION = 1

CH_VIDEO, CH_AUDIO, CH_CONTROL = 0x01, 0x02, 0x10
FLAG_KEYFRAME, FLAG_CODEC_CONFIG = 0x01, 0x02
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


def put_str(b, s):
    e = s.encode("utf-8")
    b += struct.pack(">H", len(e)) + e


def encode_hello():
    """与真眼镜 HELLO 逐字段对齐（实测值见 docs/工单-M1-04）。"""
    b = bytearray([CTRL_HELLO])
    b += struct.pack(">I", 1)                 # protocolVersion
    put_str(b, "ARGF20")                      # deviceModel
    modes = [(640, 360, 30), (1280, 720, 30), (1920, 1080, 30)]
    b += struct.pack(">H", len(modes))
    for w, h, f in modes:
        b += struct.pack(">III", w, h, f)
    for flag in (True, False, True, False, True, True):
        # hasHardwareAvcEncoder, hasLocalChineseTts, hasRotationVector,
        # hasSixDof, hasTempleTouch, hasWearDetection
        b += bytes([1 if flag else 0])
    b += bytes([1])                           # sensorTimestampSource = REALTIME
    b += struct.pack(">I", 90)                # sensorOrientationDegrees
    return bytes(b)


def split_access_units(data):
    """把 Annex-B 码流切成访问单元；SPS/PPS 单独返回。

    返回 (codec_config_bytes, [(is_keyframe, au_bytes), ...])
    """
    starts = []
    i = 0
    while True:
        j = data.find(b"\x00\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j)
        i = j + 4
    starts.append(len(data))

    nals = []
    for k in range(len(starts) - 1):
        s, e = starts[k], starts[k + 1]
        nals.append((data[s + 4] & 0x1F, data[s:e]))

    codec_config = b""
    aus = []
    pending = b""
    for nal_type, raw in nals:
        if nal_type in (7, 8):            # SPS / PPS
            codec_config += raw
            continue
        if nal_type in (1, 5):            # 片
            if pending:
                aus.append(pending)
            pending = raw
        else:                             # SEI/AUD 等，并入下一个 AU
            pending += raw
    if pending:
        aus.append(pending)

    out = []
    for au in aus:
        j = au.find(b"\x00\x00\x00\x01")
        is_key = (au[j + 4] & 0x1F) == 5 if j >= 0 else False
        out.append((is_key, au))
    return codec_config, out


def load_wav_pcm(path):
    """读取 16kHz 单声道 16bit PCM WAV，返回裸 PCM 字节。"""
    raw = open(path, "rb").read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise SystemExit(f"--audio 需要 WAV 文件：{path}")
    pos = 12
    fmt = None
    data = None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], int.from_bytes(raw[pos + 4:pos + 8], "little")
        pos += 8
        if cid == b"fmt ":
            fmt = raw[pos:pos + min(size, 16)]
        elif cid == b"data":
            data = raw[pos:pos + size]
            break
        pos += size + (size & 1)
    if fmt is None or data is None:
        raise SystemExit(f"WAV 缺少 fmt/data 块：{path}")
    channels = int.from_bytes(fmt[2:4], "little")
    rate = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if channels != 1 or rate != 16000 or bits != 16:
        raise SystemExit(
            f"--audio 需 16kHz 单声道 16bit WAV，实际 {rate}Hz/{channels}ch/{bits}bit"
        )
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--stream", required=True, help="录制的裸 H.264（Annex-B）")
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--loop", action="store_true", help="码流放完后循环，用于长跑")
    ap.add_argument("--audio", default=None,
                    help="16kHz 单声道 16bit WAV，按 20ms/包随视频一起上行（AUDIO 0x02）")
    ap.add_argument("--audio-loop", action="store_true", help="音频放完后循环")
    ap.add_argument("--connect-timeout", type=float, default=60.0,
                    help="等待手机端起监听的最长时间（秒）")
    args = ap.parse_args()

    data = open(args.stream, "rb").read()
    codec_config, aus = split_access_units(data)
    keys = sum(1 for k, _ in aus if k)
    print(f"[mock-glasses] 码流 {len(data)} 字节 → {len(aus)} 个访问单元"
          f"（关键帧 {keys}），SPS/PPS {len(codec_config)} 字节", flush=True)
    if not codec_config or not aus:
        print("[mock-glasses] 码流里没找到 SPS/PPS 或访问单元", flush=True)
        return 1

    audio_pcm = load_wav_pcm(args.audio) if args.audio else None
    if args.audio:
        print(f"[mock-glasses] 音频 {len(audio_pcm)} 字节"
              f"（约 {len(audio_pcm) / 32000:.1f}s，16kHz mono）", flush=True)

    # 手机端可能还没起来（instrumented 测试启动要几秒），重试直到连上。
    conn = None
    connect_deadline = time.time() + args.connect_timeout
    while time.time() < connect_deadline:
        try:
            conn = socket.create_connection((args.host, args.port), timeout=5)
            break
        except OSError:
            time.sleep(0.5)
    if conn is None:
        print(f"[mock-glasses] {args.connect_timeout}s 内没能连上 "
              f"{args.host}:{args.port}", flush=True)
        return 1
    conn.settimeout(0.05)
    print(f"[mock-glasses] 已连接 {args.host}:{args.port}", flush=True)

    def now_ns():
        return time.monotonic_ns()

    ctrl_seq = video_seq = audio_seq = 0
    ctrl_seq += 1
    conn.sendall(encode_packet(CH_CONTROL, 0, ctrl_seq, now_ns(), encode_hello()))
    print("[mock-glasses] → HELLO", flush=True)

    buf = bytearray()
    acked = False
    sent_config = False
    idx = 0
    frames = 0
    deadline = time.time() + args.seconds
    next_frame = time.time()
    interval = 1.0 / args.fps
    audio_interval = 0.02
    next_audio = time.time()
    audio_pos = 0
    audio_packets = 0
    PACKET_BYTES = 640

    try:
        while time.time() < deadline:
            # 收下行：HELLO_ACK / PING
            try:
                chunk = conn.recv(65536)
                if not chunk:
                    print("[mock-glasses] 对端关闭连接", flush=True)
                    break
                buf += chunk
            except socket.timeout:
                pass

            while len(buf) >= HEADER:
                if buf[0:2] != MAGIC:
                    print("[mock-glasses] magic 错位，断开", flush=True)
                    return 1
                n = (buf[5] << 16) | (buf[6] << 8) | buf[7]
                if len(buf) < HEADER + n:
                    break
                ch, payload = buf[3], bytes(buf[HEADER:HEADER + n])
                del buf[:HEADER + n]
                if ch == CH_CONTROL and payload:
                    t = payload[0]
                    if t == CTRL_HELLO_ACK and not acked:
                        acked = True
                        w, h, fps_, br = struct.unpack(">IIII", payload[1:17])
                        print(f"[mock-glasses] ← HELLO_ACK {w}x{h}@{fps_} {br}bps", flush=True)
                    elif t == CTRL_HELLO_REJECT:
                        print("[mock-glasses] ← HELLO_REJECT，退出", flush=True)
                        return 1
                    elif t == CTRL_PING:
                        t1 = struct.unpack(">q", payload[1:9])[0]
                        t2 = now_ns()
                        ctrl_seq += 1
                        body = bytes([CTRL_PONG]) + struct.pack(">qqq", t1, t2, now_ns())
                        conn.sendall(encode_packet(CH_CONTROL, 0, ctrl_seq, now_ns(), body))

            if not acked:
                continue

            if not sent_config:
                video_seq += 1
                conn.sendall(encode_packet(CH_VIDEO, FLAG_CODEC_CONFIG, video_seq,
                                           now_ns(), codec_config))
                sent_config = True
                print(f"[mock-glasses] → CODEC_CONFIG {len(codec_config)} 字节", flush=True)

            # AUDIO (0x02)：每 20ms 一包 640 字节（16kHz × 20ms × 2B）
            if audio_pcm and time.time() >= next_audio:
                next_audio += audio_interval
                if audio_pos >= len(audio_pcm):
                    if not args.audio_loop:
                        audio_pcm = None
                    else:
                        audio_pos = 0
                if audio_pcm:
                    packet = audio_pcm[audio_pos:audio_pos + PACKET_BYTES]
                    if len(packet) < PACKET_BYTES:
                        # 流末不足一包：补零到 640 或直接发尾包（协议允许不足仅流末）
                        packet = packet.ljust(PACKET_BYTES, b"\x00")
                    audio_pos += PACKET_BYTES
                    audio_seq += 1
                    audio_packets += 1
                    conn.sendall(encode_packet(CH_AUDIO, 0, audio_seq, now_ns(), packet))

            now = time.time()
            if now < next_frame:
                continue
            next_frame += interval

            if idx >= len(aus):
                if not args.loop:
                    print("[mock-glasses] 码流放完", flush=True)
                    break
                idx = 0
            is_key, au = aus[idx]
            idx += 1
            video_seq += 1
            frames += 1
            conn.sendall(encode_packet(CH_VIDEO, FLAG_KEYFRAME if is_key else 0,
                                       video_seq, now_ns(), au))
    finally:
        try:
            ctrl_seq += 1
            conn.sendall(encode_packet(CH_CONTROL, 0, ctrl_seq, now_ns(),
                                       bytes([CTRL_BYE]) + struct.pack(">H", 0)))
        except Exception:
            pass
        conn.close()

    print(f"\n===== 判读 =====\n  已发送 {frames} 个视频包、{audio_packets} 个音频包，"
          f"握手={'成功' if acked else '失败'}",
          flush=True)
    return 0 if (acked and frames > 0 and (audio_packets > 0 or not args.audio)) else 1


if __name__ == "__main__":
    sys.exit(main())
