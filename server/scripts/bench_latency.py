#!/usr/bin/env python3
"""延迟压测（CLAUDE.md §12）：N 台设备并发，输出 P50/P95 与逐节点耗时。

直接在进程内跑管线，不经过 HTTP——目的是量化管线本身的延迟分解，
排除网络噪声。带网络的端到端数字用 fake_glasses.py 测。

用法：
    python scripts/bench_latency.py --devices 20 --frames-per-device 50
    MOCK_LATENCY_MS=1200 python scripts/bench_latency.py   # 模拟真实模型耗时
"""

from __future__ import annotations

import argparse
import asyncio
import io
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.observability import setup_logging  # noqa: E402
from app.observability.metrics import node_latency  # noqa: E402
from app.runtime import AppContext  # noqa: E402

# §6 延迟预算目标
TARGET_P50_S = 1.5
TARGET_P95_S = 3.0


def make_frame(seed: int) -> bytes:
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    img = Image.new("RGB", (640, 480), (12, 12, 16))
    draw = ImageDraw.Draw(img)
    for _ in range(18):
        x0, y0 = rng.randrange(0, 600), rng.randrange(0, 440)
        x1, y1 = x0 + rng.randrange(30, 160), y0 + rng.randrange(30, 160)
        color = (rng.randrange(80, 256), rng.randrange(80, 256), rng.randrange(80, 256))
        (draw.rectangle if rng.random() < 0.5 else draw.ellipse)([x0, y0, x1, y1], fill=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


async def device_loop(ctx: AppContext, device_id: str, frames: int, base_seed: int) -> list:
    out = []
    for i in range(frames):
        state, elapsed = await ctx.process_frame(
            device_id=device_id, frame_jpeg=make_frame(base_seed + i), seq=i
        )
        out.append((state, elapsed))
    return out


def dump_node_histograms() -> None:
    """从 Prometheus 直方图里反推每个节点的平均耗时。"""
    print("\n逐节点耗时（均值，来自 linksee_node_latency_seconds）")
    print("-" * 52)
    rows = []
    for metric in node_latency.collect():
        totals: dict[str, float] = {}
        counts: dict[str, float] = {}
        for sample in metric.samples:
            node = sample.labels.get("node", "?")
            if sample.name.endswith("_sum"):
                totals[node] = sample.value
            elif sample.name.endswith("_count"):
                counts[node] = sample.value
        for node, total in totals.items():
            n = counts.get(node, 0)
            if n:
                rows.append((total / n, node, int(n)))

    for mean_s, node, n in sorted(rows, reverse=True):
        share = ""
        print(f"  {node:<12} {mean_s * 1000:>8.2f} ms   (n={n}){share}")
    print("-" * 52)


async def main_async(args) -> None:
    # 压测输出只要统计结果，把逐帧日志压掉
    setup_logging("WARNING")
    settings = get_settings().model_copy(
        update={
            "kv_backend": args.kv,
            "db_backend": "null",
            "kb_backend": "null",
            # 压测要让帧真的进模型，否则测的是闸门而不是管线
            "gate_rate_limit_per_sec": 10_000.0,
            "gate_min_interval_s": 0.0,
            "gate_phash_dup_distance": 0,
        }
    )
    ctx = AppContext(settings)
    await ctx.startup()

    total = args.devices * args.frames_per_device
    print(
        f"压测：{args.devices} 台设备 x {args.frames_per_device} 帧 = {total} 次，"
        f"后端={settings.inference_backend} kv={settings.kv_backend}"
    )

    start = time.perf_counter()
    try:
        results = await asyncio.gather(
            *[
                device_loop(ctx, f"bench-{d}", args.frames_per_device, d * 10_000)
                for d in range(args.devices)
            ]
        )
        await ctx.background.drain(timeout_s=10.0)
    finally:
        await ctx.shutdown()
    wall = time.perf_counter() - start

    flat = [item for sub in results for item in sub]
    latencies = [elapsed for _, elapsed in flat]
    outcomes: dict[str, int] = {}
    for state, _ in flat:
        key = (
            "rejected" if state.get("rejected_by")
            else "error" if state.get("error")
            else "replied"
        )
        outcomes[key] = outcomes.get(key, 0) + 1

    p50 = statistics.median(latencies)
    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)

    print("\n" + "=" * 52)
    print(f"总耗时 {wall:.2f}s   吞吐 {total / wall:.1f} 帧/秒")
    print(f"结果分布 {outcomes}")
    print("-" * 52)
    print(f"  P50  {p50 * 1000:>8.1f} ms   目标 <= {TARGET_P50_S * 1000:.0f}   "
          f"{'PASS' if p50 <= TARGET_P50_S else 'FAIL'}")
    print(f"  P95  {p95 * 1000:>8.1f} ms   目标 <= {TARGET_P95_S * 1000:.0f}   "
          f"{'PASS' if p95 <= TARGET_P95_S else 'FAIL'}")
    print(f"  P99  {p99 * 1000:>8.1f} ms")
    print(f"  max  {max(latencies) * 1000:>8.1f} ms")
    print("=" * 52)

    dump_node_histograms()

    if settings.inference_backend == "mock" and settings.mock_latency_ms == 0:
        print(
            "\n注意：mock 后端零延迟，上面的数字**不能**作为 §12 验收依据。"
            "\n真实验收需 INFERENCE_BACKEND=dashscope，或至少 MOCK_LATENCY_MS=1200"
            "\n来复现模型调用占 P50 94% 的结构。"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="管线延迟压测")
    parser.add_argument("--devices", type=int, default=20, help="并发设备数（§1 首期 20）")
    parser.add_argument("--frames-per-device", type=int, default=50)
    parser.add_argument("--kv", default="memory", choices=("memory", "redis"))
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
