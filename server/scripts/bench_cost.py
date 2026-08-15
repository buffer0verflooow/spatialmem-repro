#!/usr/bin/env python3
"""成本实测（CLAUDE.md §7）——W1 必须跑完并把结果填回 CLAUDE.md。

做两件事：
  1. 用真实场景图跑 N 次真实调用，实测平均 token 数与延迟
  2. 按 §7 公式算三档设备规模的月成本

单价必须从 DashScope 官方价格页取当前费率再传进来，脚本不内置价格——
硬编码的价格过期后会给出错误结论，比没有更危险。

用法：
    # 先实测 token（需要 DASHSCOPE_API_KEY）
    python scripts/bench_cost.py measure --images tests/fixtures/frames --n 20

    # 再算成本（单价单位：元 / 1000 token）
    python scripts/bench_cost.py project \
        --prompt-tokens 1180 --completion-tokens 42 \
        --price-in 0.008 --price-out 0.008
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.inference.backend import build_backend  # noqa: E402
from app.inference.image import normalize  # noqa: E402
from app.inference.parser import parse  # noqa: E402

# 三档规模（§7）
SCENARIOS = ((20, "首期"), (100, "中期"), (500, "规模化"))


def load_images(image_dir: Path) -> list[bytes]:
    """同步读盘，别在 async 函数里做阻塞 IO。"""
    paths = sorted(image_dir.glob("*.jpg")) + sorted(image_dir.glob("*.jpeg"))
    return [p.read_bytes() for p in paths]


async def measure(args) -> None:
    settings = get_settings()
    if settings.inference_backend != "dashscope":
        print("警告：INFERENCE_BACKEND 不是 dashscope，测的是 mock 的假数字")

    images = load_images(Path(args.images))
    if not images:
        sys.exit(f"{args.images} 下没有 jpg，请放入真实场景样例帧")

    backend = build_backend(settings)
    prompt_tokens, completion_tokens, latencies = [], [], []
    sizes = []

    print(f"用 {len(images)} 张图跑 {args.n} 次调用 ...\n")
    try:
        for i in range(args.n):
            raw = images[i % len(images)]
            image, _ = normalize(
                raw,
                max_edge=settings.image_max_edge,
                quality=settings.image_jpeg_quality,
            )
            sizes.append(len(image))
            resp = await backend.infer(image, [])
            result, degraded = parse(resp.raw_text)
            prompt_tokens.append(resp.prompt_tokens)
            completion_tokens.append(resp.completion_tokens)
            latencies.append(resp.latency_ms)
            flag = " [解析降级]" if degraded else ""
            print(
                f"  {i + 1:>3}/{args.n}  in={resp.prompt_tokens:>5} "
                f"out={resp.completion_tokens:>4} {resp.latency_ms:>5}ms  "
                f"risk={result.risk_level:<6}{flag}"
            )
    finally:
        await backend.close()

    print("\n" + "=" * 60)
    print(f"图像大小   均值 {statistics.mean(sizes) / 1024:.0f} KB")
    print(f"prompt     均值 {statistics.mean(prompt_tokens):.0f} token")
    print(f"completion 均值 {statistics.mean(completion_tokens):.0f} token")
    ordered = sorted(latencies)
    print(
        f"模型延迟   P50 {statistics.median(ordered):.0f}ms  "
        f"P95 {ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]:.0f}ms"
    )
    print("=" * 60)
    print("\n把上面的 token 均值填进下一步：")
    print(
        f"  python scripts/bench_cost.py project "
        f"--prompt-tokens {statistics.mean(prompt_tokens):.0f} "
        f"--completion-tokens {statistics.mean(completion_tokens):.0f} "
        f"--price-in <元/1k> --price-out <元/1k>"
    )


def project(args) -> None:
    unit = (
        args.prompt_tokens / 1000 * args.price_in
        + args.completion_tokens / 1000 * args.price_out
    )
    print("=" * 68)
    print(f"单次调用成本 = {unit:.6f} 元")
    print(
        f"  (in {args.prompt_tokens:.0f} tok x {args.price_in}/1k + "
        f"out {args.completion_tokens:.0f} tok x {args.price_out}/1k)"
    )
    print(f"假设：在线 {args.hours} 小时/天，{args.calls_per_hour} 次调用/小时")
    print("=" * 68)
    print(f"{'设备数':>8} {'档位':>8} {'调用/月':>14} {'月成本(元)':>14}")
    print("-" * 68)
    for devices, label in SCENARIOS:
        calls = devices * args.hours * args.calls_per_hour * 30
        print(f"{devices:>8} {label:>8} {calls:>14,.0f} {calls * unit:>14,.0f}")
    print("-" * 68)

    print("\n对比：不装闸门、按 2 帧/秒全量处理（§5.1）")
    for devices, label in SCENARIOS:
        calls = devices * args.hours * 3600 * 2 * 30
        print(f"{devices:>8} {label:>8} {calls:>14,.0f} {calls * unit:>14,.0f}")
    print(
        f"\n倍数关系: {3600 * 2 / args.calls_per_hour:.0f}x"
        "  —— 闸门是唯一能把成本压到可上线的手段"
    )
    print("\n超预算时的调节杠杆（§7 优先级）：")
    print("  1. 提高 GATE_MIN_INTERVAL_S / GATE_PHASH_DUP_DISTANCE")
    print("  2. IMAGE_MAX_EDGE 再降一档（1024 -> 768），prompt token 近似平方下降")
    print("  3. 评估更低价模型档位并做质量对比")
    print("  4. 高频简单场景用本地小模型分流")


def main() -> None:
    parser = argparse.ArgumentParser(description="成本实测与推算")
    sub = parser.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("measure", help="实测真实调用的 token 与延迟")
    m.add_argument("--images", default="tests/fixtures/frames")
    m.add_argument("--n", type=int, default=20)

    p = sub.add_parser("project", help="按公式推算月成本")
    p.add_argument("--prompt-tokens", type=float, required=True)
    p.add_argument("--completion-tokens", type=float, required=True)
    p.add_argument("--price-in", type=float, required=True, help="元 / 1000 输入 token")
    p.add_argument("--price-out", type=float, required=True, help="元 / 1000 输出 token")
    p.add_argument("--hours", type=float, default=4.0, help="单设备日均在线小时")
    p.add_argument("--calls-per-hour", type=float, default=360.0, help="过闸门后的调用频次")

    args = parser.parse_args()
    if args.cmd == "measure":
        asyncio.run(measure(args))
    else:
        project(args)


if __name__ == "__main__":
    main()
