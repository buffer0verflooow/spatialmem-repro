#!/usr/bin/env python3
"""pose_check.py —— 按 HANDOFF.md「SpatialMem 位姿采集·验收建议」判读一段录制的 POSE 通道数据。

对 `m1_record.py --label pose_repro --seconds 60` 生成的会话目录做四项检查：

  1. pose.jsonl 存在且有解码出的采样（>0）；
  2. 采样率 ≥ 1Hz（采样跨度按采样自身时间戳，与帧率无关）；
  3. 轨迹平滑：相邻采样四元数夹角不超阈值（默认 90°，对应 100ms 批间隔下
     ~900°/s 的瞬时角速度，正常转头不会到）；
  4. 时间戳对齐：随机抽 10 个采样，其 sender 域时间戳能在容差内（默认 100ms）
     在 video_timeline.csv 的 sender_ts_ns 中找到最近帧。

COLMAP 轨迹误差对比不在本脚本范围（需 spatialmem-repro 评测脚本）。

用法：
    python3 scripts/pose_check.py glasses-recordings/session_20260803_125024_pose_repro
    python3 scripts/pose_check.py <session_dir> --min-rate 5 --max-jump-deg 60

退出码：全部通过 0，任一失败 1。
"""

import argparse
import bisect
import csv
import json
import math
import random
import sys
from pathlib import Path


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def quat_angle_deg(a, b):
    """两个单位四元数 (x,y,z,w) 之间的旋转角（度），自动处理 q/-q 等价。"""
    dot = a["qx"] * b["qx"] + a["qy"] * b["qy"] + a["qz"] * b["qz"] + a["qw"] * b["qw"]
    return 2.0 * math.degrees(math.acos(clamp(abs(dot), -1.0, 1.0)))


def load_pose_samples(pose_jsonl):
    samples = []
    decode_errors = 0
    packets = 0
    for line in pose_jsonl.open():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if "decode_error" in row:
            decode_errors += 1
            continue
        packets += 1
        samples.extend(row.get("samples") or [])
    return samples, packets, decode_errors


def load_video_timestamps(timeline_csv):
    stamps = []
    with timeline_csv.open(newline="") as f:
        for row in csv.DictReader(f):
            raw = (row.get("sender_ts_ns") or "").strip()
            if raw:
                stamps.append(int(raw))
    return sorted(stamps)


def check(session_dir: Path, min_rate, max_jump_deg, align_tolerance_ms, align_samples):
    ok = True

    def report(name, passed, detail):
        nonlocal ok
        ok = ok and passed
        print(f"[pose-check] {name}: {'PASS' if passed else 'FAIL'} ({detail})", flush=True)

    pose_jsonl = session_dir / "pose.jsonl"
    timeline_csv = session_dir / "video_timeline.csv"

    if not pose_jsonl.exists():
        report("pose.jsonl 存在", False, "文件不存在")
        print(f"[pose-check] 结论: FAIL", flush=True)
        return 1

    samples, packets, decode_errors = load_pose_samples(pose_jsonl)
    report(
        "采样非空",
        len(samples) > 0,
        f"{len(samples)} 采样 / {packets} 包 / 解码错误 {decode_errors}"
    )

    finite = [s for s in samples
              if all(math.isfinite(v) for v in (s["qx"], s["qy"], s["qz"], s["qw"]))]
    report(
        "数值有限",
        len(finite) == len(samples),
        f"非有限采样 {len(samples) - len(finite)}"
    )
    samples = finite

    if len(samples) >= 2:
        samples.sort(key=lambda s: s["timestamp_ns"])
        span_s = (samples[-1]["timestamp_ns"] - samples[0]["timestamp_ns"]) / 1e9
        rate = (len(samples) - 1) / span_s if span_s > 0 else float("inf")
        report(
            "采样率 ≥1Hz",
            rate >= min_rate,
            f"{rate:.2f} Hz（跨度 {span_s:.2f}s，要求 ≥{min_rate}Hz）"
        )

        jumps = []
        for i in range(1, len(samples)):
            dt_s = (samples[i]["timestamp_ns"] - samples[i - 1]["timestamp_ns"]) / 1e9
            if dt_s <= 0 or dt_s > 1.0:
                continue  # 批间隔异常不作为"跳变"判据，只计算连续正常间隔
            jumps.append(quat_angle_deg(samples[i - 1], samples[i]))
        max_jump = max(jumps) if jumps else 0.0
        report(
            "轨迹平滑",
            max_jump <= max_jump_deg,
            f"相邻采样最大夹角 {max_jump:.2f}°（阈值 {max_jump_deg}°）"
        )
    else:
        report("采样率 ≥1Hz", False, "采样不足 2 个，无法计算")
        report("轨迹平滑", False, "采样不足 2 个，无法计算")

    if timeline_csv.exists():
        stamps = load_video_timestamps(timeline_csv)
        if stamps and samples:
            rng = random.Random(20260803)
            chosen = rng.sample(samples, k=min(align_samples, len(samples)))
            tol_ns = int(align_tolerance_ms * 1_000_000)
            missed = 0
            for s in chosen:
                i = bisect.bisect_left(stamps, s["timestamp_ns"])
                window = stamps[max(0, i - 1):i + 2] or [stamps[0]]
                nearest = min(window, key=lambda t: abs(t - s["timestamp_ns"]))
                if nearest is None or abs(nearest - s["timestamp_ns"]) > tol_ns:
                    missed += 1
            hit = len(chosen) - missed
            report(
                "时间戳对齐",
                hit == len(chosen),
                f"{hit}/{len(chosen)} 采样在 ±{align_tolerance_ms}ms 内对齐到视频帧"
            )
        else:
            report("时间戳对齐", False, "视频时间线或位姿采样为空，无法比对")
    else:
        report("时间戳对齐", False, "缺少 video_timeline.csv")

    print(f"[pose-check] 结论: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="POSE 通道录制验收（交接说明 §7.4）")
    ap.add_argument("session_dir", type=Path, help="m1_record.py 生成的会话目录")
    ap.add_argument("--min-rate", type=float, default=1.0, help="最低采样率 Hz（默认 1.0）")
    ap.add_argument("--max-jump-deg", type=float, default=90.0,
                    help="相邻采样最大夹角（默认 90°）")
    ap.add_argument("--align-tolerance-ms", type=float, default=100.0,
                    help="与视频帧对齐的容差（默认 100ms）")
    ap.add_argument("--align-samples", type=int, default=10,
                    help="随机抽几个采样做对齐检查（默认 10）")
    args = ap.parse_args()

    if not args.session_dir.is_dir():
        print(f"[pose-check] 会话目录不存在: {args.session_dir}", file=sys.stderr)
        return 1
    return check(
        args.session_dir,
        args.min_rate,
        args.max_jump_deg,
        args.align_tolerance_ms,
        args.align_samples,
    )


if __name__ == "__main__":
    sys.exit(main())
