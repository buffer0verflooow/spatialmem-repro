#!/usr/bin/env python3
"""m1_record.py —— 真机 M1 链路录制器（雷鸟 X3 Pro → PC/Mac）。

在 PC/Mac 上监听 TCP（经 `adb reverse` 接眼镜），把眼镜端 :glasses 推来的流
落盘为自包含会话目录：

    session_<ts>_<label>/
        video.h264              裸 H.264（Annex-B，每包一个访问单元）
        video.mp4               ffmpeg 转封装（--no-ffmpeg 可跳过）
        video_timeline.csv      每帧：frame_index,sender_ts_ns,host_mono_ns,flags,bytes
        audio.pcm               AUDIO 通道原始负载（PCM16 或 Opus，握手协商）
        imu.jsonl / input.jsonl                原始负载 base64 + 双时间戳
        pose.jsonl                             解码后的 rotation vector 采样批 + 原始 base64
        events.jsonl            握手 / 重连 / 场景标记 / 结束
        session.json            元数据 + 判读统计

用法：
    adb -s <serial> reverse tcp:47810 tcp:47810
    python3 scripts/m1_record.py --label indoor_walk --seconds 60
    python3 scripts/m1_record.py --label low_light --seconds 30 --mark 关灯 --note "约 30 lux"

高画质录制（720p@10，HELLO_ACK 协商，眼镜端按配置重建相机）：
    adb -s <serial> shell am start -n com.example.blindassist.glasses/.GlassLinkActivity \
        --es host 127.0.0.1
    python3 scripts/m1_record.py --label new_scene --seconds 120 \
        --video-mode 1280x720@10 --bitrate 1800000

依赖：m1_mock_phone.py（同目录，提供线格式编解码）；ffmpeg 可选（转 mp4）。
"""

import argparse
import base64
import csv
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m1_mock_phone import (  # noqa: E402
    HEADER,
    CHANNELS,
    FLAG_KEYFRAME,
    FLAG_CODEC_CONFIG,
    CTRL_HELLO,
    CTRL_PING,
    CTRL_PONG,
    CTRL_BYE,
    decode_pose_batch,
    encode_packet,
    parse_header,
    Reader,
    decode_hello,
    encode_hello_ack,
)

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "glasses-recordings"
# 请求全部可上行通道（眼镜端目前只回 VIDEO+CONTROL；M1-05 落地后自动补全）
REQUEST_CHANNELS = [0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11, 0x12]


class Recorder:
    def __init__(self, session_dir, label, note, marks,
                 video_w=640, video_h=360, video_fps=15, video_bitrate=1_200_000):
        self.dir = session_dir
        self.label = label
        self.note = note
        self.video_w = video_w
        self.video_h = video_h
        self.video_fps = video_fps
        self.video_bitrate = video_bitrate
        self.stats = Counter()
        self.saw = {"hello": False, "codec_config": False, "keyframe": False}
        self.rtts = []
        self.first_video_ts = None
        self.last_video_ts = None
        self.video_count = 0
        self.keyframes = 0
        self.audio_bytes = 0
        self.pose_packets = 0
        self.pose_samples = 0
        self.pose_decode_errors = 0
        self.codec_ready = False     # 本连接是否已收到 SPS/PPS
        self.writing = False         # 是否已见过参数集后的首个 IDR（从此才开始落盘）
        self.pending_config = None   # 待写到文件头的 SPS/PPS
        self.config_ts = None
        self.ctrl_seq = 0
        self.acks_sent = 0
        self.connections = 0
        self.started_wall = datetime.now().astimezone().isoformat(timespec="seconds")
        self.started_mono = time.monotonic_ns()
        self.device = {}

        self.h264 = (self.dir / "video.h264").open("wb")
        self.audio = (self.dir / "audio.pcm").open("wb")
        self.timeline = (self.dir / "video_timeline.csv").open("w", newline="")
        self.tw = csv.writer(self.timeline)
        self.tw.writerow(["frame_index", "sender_ts_ns", "host_mono_ns", "flags", "bytes"])
        self.imu_f = (self.dir / "imu.jsonl").open("w")
        self.pose_f = (self.dir / "pose.jsonl").open("w")
        self.input_f = (self.dir / "input.jsonl").open("w")
        self.events_f = (self.dir / "events.jsonl").open("w")

        self.event("recorder_start", label=label, note=note,
                   requested_video=f"{video_w}x{video_h}@{video_fps}",
                   requested_bitrate=video_bitrate)
        for m in marks:
            self.event("marker", text=m)

    def now_mono(self):
        return time.monotonic_ns()

    def event(self, kind, **kw):
        row = {
            "t_wall": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "t_host_mono_ns": self.now_mono(),
            "event": kind,
        }
        row.update(kw)
        self.events_f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.events_f.flush()

    def close(self):
        for f in (self.h264, self.audio, self.timeline, self.imu_f,
                  self.pose_f, self.input_f, self.events_f):
            f.close()


def stream_channel(rec, channel, flags, seq, ts, payload, host_mono):
    """非 CONTROL 通道落盘。"""
    if channel == 0x01:  # VIDEO
        if flags & FLAG_CODEC_CONFIG:
            rec.saw["codec_config"] = True
            rec.codec_ready = True
            rec.event("codec_config", bytes=len(payload))
            if not rec.writing:
                # 保留到首个 IDR 时写到文件头（真机实测：重连后 SPS/PPS 会晚于旧帧）
                rec.pending_config = payload
                rec.config_ts = ts
            return
        if not rec.codec_ready:
            rec.stats["_dropped_pre_config"] += 1
            return
        if flags & FLAG_KEYFRAME:
            rec.saw["keyframe"] = True
            rec.keyframes += 1
        if not rec.writing:
            if not (flags & FLAG_KEYFRAME):
                rec.stats["_dropped_pre_keyframe"] += 1
                return
            if rec.pending_config:
                rec.h264.write(rec.pending_config)
                rec.tw.writerow([0, rec.config_ts, host_mono, 2, len(rec.pending_config)])
                rec.pending_config = None
            rec.writing = True
        if rec.first_video_ts is None:
            rec.first_video_ts = ts
        rec.last_video_ts = ts
        rec.video_count += 1
        rec.h264.write(payload)
        rec.tw.writerow([rec.video_count, ts, host_mono, flags, len(payload)])
    elif channel == 0x02:  # AUDIO（PCM16 或 Opus，原样落盘）
        rec.audio.write(payload)
        rec.audio_bytes += len(payload)
    elif channel in (0x03, 0x04, 0x05):  # IMU / POSE / INPUT（原始负载，格式见工单 M1-05）
        target = {0x03: rec.imu_f, 0x04: rec.pose_f, 0x05: rec.input_f}[channel]
        row = {
            "sender_ts_ns": ts,
            "host_mono_ns": host_mono,
            "seq": seq,
            "flags": flags,
            "bytes": len(payload),
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        }
        if channel == 0x04:
            rec.pose_packets += 1
            try:
                samples = decode_pose_batch(payload)
                row["sample_count"] = len(samples)
                row["samples"] = samples
                rec.pose_samples += len(samples)
            except ValueError as e:
                rec.pose_decode_errors += 1
                row["decode_error"] = str(e)
                rec.event("pose_decode_error", error=str(e), seq=seq, bytes=len(payload))
        target.write(json.dumps(row, ensure_ascii=False) + "\n")
        target.flush()


def handle_connection(rec, conn, deadline):
    rec.connections += 1
    # 新连接 = 新解码会话：参数集/起始状态全部重置
    rec.codec_ready = False
    rec.writing = False
    rec.pending_config = None
    rec.event("connected")
    print(f"[record] 已连接 {conn.getpeername()}（第 {rec.connections} 次）", flush=True)
    conn.settimeout(0.5)
    buf = bytearray()
    next_ping = time.monotonic() + 1.0
    last_progress = time.monotonic()

    while time.monotonic() < deadline:
        if time.monotonic() >= next_ping:
            rec.ctrl_seq += 1
            t1 = time.monotonic_ns()
            conn.sendall(encode_packet(0x10, 0, rec.ctrl_seq, t1,
                                       bytes([CTRL_PING]) + struct.pack(">q", t1)))
            next_ping = time.monotonic() + 1.0
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            rec.event("disconnected")
            print("[record] 对端关闭连接，等待重连…", flush=True)
            return
        buf += chunk

        while len(buf) >= HEADER:
            channel, flags, n, seq, ts = parse_header(buf)
            if len(buf) < HEADER + n:
                break
            payload = bytes(buf[HEADER:HEADER + n])
            del buf[:HEADER + n]
            rec.stats[CHANNELS.get(channel, f"0x{channel:02x}")] += 1
            host_mono = rec.now_mono()

            if channel == 0x10:  # CONTROL
                if not payload:
                    continue
                t = payload[0]
                if t == CTRL_HELLO:
                    rec.saw["hello"] = True
                    rec.device = decode_hello(payload[1:])
                    rec.event("hello", device=rec.device.get("deviceModel"),
                              modes=len(rec.device.get("videoModes", [])))
                    print(f"[record] ← HELLO {rec.device.get('deviceModel')} "
                          f"ts={rec.device.get('sensorTimestampSource')} "
                          f"orientation={rec.device.get('sensorOrientationDegrees')}", flush=True)
                    rec.ctrl_seq += 1
                    ack = encode_hello_ack(
                        rec.video_w, rec.video_h, rec.video_fps, rec.video_bitrate,
                        REQUEST_CHANNELS, 1, 1000, 30000,
                    )
                    conn.sendall(encode_packet(0x10, 0, rec.ctrl_seq, rec.now_mono(), ack))
                    rec.acks_sent += 1
                    print(
                        f"[record] → HELLO_ACK {rec.video_w}x{rec.video_h}@{rec.video_fps} "
                        f"{rec.video_bitrate}bps（请求 {len(REQUEST_CHANNELS)} 通道）",
                        flush=True,
                    )
                elif t == CTRL_PONG:
                    r = Reader(payload[1:])
                    t1, t2, t3 = r.i64(), r.i64(), r.i64()
                    t4 = time.monotonic_ns()
                    rec.rtts.append((t4 - t1) - (t3 - t2))
                elif t == CTRL_BYE:
                    rec.event("bye", reason=Reader(payload[1:]).s())
                    print("[record] ← BYE", flush=True)
            else:
                stream_channel(rec, channel, flags, seq, ts, payload, host_mono)

        if time.monotonic() - last_progress >= 10:
            last_progress = time.monotonic()
            print(f"[record] {(time.monotonic()-rec.started_mono/1e9):.0f}s "
                  f"VIDEO {rec.video_count} 帧 / AUDIO {rec.audio_bytes} B", flush=True)


def main():
    ap = argparse.ArgumentParser(description="真机 M1 链路录制器")
    ap.add_argument("--port", type=int, default=47810)
    ap.add_argument("--label", required=True, help="场景标签，如 indoor_walk / low_light")
    ap.add_argument("--seconds", type=float, default=60.0, help="录制时长（秒）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="录制根目录")
    ap.add_argument("--mark", action="append", default=[], help="开始时的场景标记（可重复）")
    ap.add_argument("--note", default="", help="备注，写入 session.json")
    ap.add_argument("--no-ffmpeg", action="store_true", help="不转 mp4")
    ap.add_argument("--video-mode", default="640x360@15",
                    help="HELLO_ACK 请求的采集模式 WxH@fps，如 1280x720@10（眼镜端按此重建相机）")
    ap.add_argument("--bitrate", type=int, default=1_200_000,
                    help="HELLO_ACK 请求的编码码率 bps（如 1800000）")
    args = ap.parse_args()

    m = re.fullmatch(r"(\d+)x(\d+)@(\d+)", args.video_mode)
    if not m:
        raise SystemExit(f"--video-mode 格式应为 WxH@fps，收到：{args.video_mode!r}")
    video_w, video_h, video_fps = int(m.group(1)), int(m.group(2)), int(m.group(3))

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = args.out / f"session_{stamp}_{args.label}"
    session_dir.mkdir()

    rec = Recorder(session_dir, args.label, args.note, args.mark,
                   video_w=video_w, video_h=video_h, video_fps=video_fps,
                   video_bitrate=args.bitrate)
    print(f"[record] 会话目录: {session_dir}", flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    srv.settimeout(0.5)
    print(f"[record] 监听 0.0.0.0:{args.port}，label={args.label}，{args.seconds}s …", flush=True)

    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            with conn:
                handle_connection(rec, conn, deadline)
    except KeyboardInterrupt:
        print("\n[record] Ctrl-C，落盘…", flush=True)
    finally:
        srv.close()

    rec.event("recorder_end")
    rec.close()

    # ---- 判读统计 ----
    span = None
    if rec.first_video_ts and rec.last_video_ts and rec.last_video_ts > rec.first_video_ts:
        span = (rec.last_video_ts - rec.first_video_ts) / 1e9
    fps = rec.video_count / span if span else None
    best_rtt = min(rec.rtts) / 1e6 if rec.rtts else None
    verdict = "PASS" if (rec.saw["hello"] and rec.saw["codec_config"]
                         and rec.saw["keyframe"] and rec.video_count > 0) else "FAIL"

    meta = {
        "session_dir": session_dir.name,
        "label": args.label,
        "note": args.note,
        "started_wall": rec.started_wall,
        "ended_wall": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_s": round((time.monotonic_ns() - rec.started_mono) / 1e9, 3),
        "device": rec.device,
        "stats": dict(rec.stats),
        "video": {
            "frames": rec.video_count,
            "span_s": round(span, 3) if span else None,
            "fps": round(fps, 2) if fps else None,
            "keyframes": rec.keyframes,
            "raw_bytes": (session_dir / "video.h264").stat().st_size,
        },
        "audio_bytes": rec.audio_bytes,
        "pose": {
            "packets": rec.pose_packets,
            "samples": rec.pose_samples,
            "decode_errors": rec.pose_decode_errors,
        },
        "clock_sync": {
            "samples": len(rec.rtts),
            "best_rtt_ms": round(best_rtt, 2) if best_rtt is not None else None,
            "error_bound_ms": round(best_rtt / 2, 2) if best_rtt is not None else None,
        },
        "connections": rec.connections,
        "verdict": verdict,
    }

    mp4_path = session_dir / "video.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and not args.no_ffmpeg:
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(session_dir / "video.h264"),
             "-c", "copy", str(mp4_path)],
            capture_output=True, text=True,
        )
        meta["ffmpeg_ok"] = r.returncode == 0
        if r.returncode != 0:
            meta["ffmpeg_stderr"] = r.stderr[-500:]
    else:
        meta["ffmpeg_ok"] = False

    (session_dir / "session.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 判读 =====", flush=True)
    for k in sorted(rec.stats):
        print(f"  {k}: {rec.stats[k]}")
    if span:
        print(f"  视频时间跨度 {span:.2f}s，实测帧率 {fps:.2f} FPS")
    if best_rtt is not None:
        print(f"  时钟对齐样本 {len(rec.rtts)} 个，最优 rtt {best_rtt:.2f}ms，"
              f"误差上界 {best_rtt/2:.2f}ms（要求 ≤50ms）")
    print(f"  HELLO={rec.saw['hello']} CODEC_CONFIG={rec.saw['codec_config']} "
          f"KEYFRAME={rec.saw['keyframe']} VIDEO={rec.video_count}")
    print(f"  POSE: {rec.pose_packets} 包 / {rec.pose_samples} 采样（解码错误 {rec.pose_decode_errors}）")
    print(f"  结论: {verdict}")
    print(f"  产物: {session_dir}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
