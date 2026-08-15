#!/usr/bin/env python3
"""假眼镜：模拟设备推流，验证端到端闭环（CLAUDE.md §11 W1 出口条件）。

也是 W6 硬件延期时的协议模拟器（§13 风险 2 的应对手段）。

用法：
    # 先起服务：make run
    python scripts/fake_glasses.py --frames 20 --fps 2
    python scripts/fake_glasses.py --frames 5 --scene-change-every 1   # 每帧都换场景
    python scripts/fake_glasses.py --http                              # 走 HTTP 备用接口
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.transport.auth import sign  # noqa: E402


def make_frame(seed: int, width: int = 640, height: int = 480) -> bytes:
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    img = Image.new("RGB", (width, height), (12, 12, 16))
    draw = ImageDraw.Draw(img)
    for _ in range(18):
        x0, y0 = rng.randrange(0, width - 40), rng.randrange(0, height - 40)
        x1, y1 = x0 + rng.randrange(30, 160), y0 + rng.randrange(30, 160)
        color = (rng.randrange(80, 256), rng.randrange(80, 256), rng.randrange(80, 256))
        (draw.rectangle if rng.random() < 0.5 else draw.ellipse)([x0, y0, x1, y1], fill=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


class Stats:
    def __init__(self) -> None:
        self.latencies: list[float] = []
        self.outcomes: dict[str, int] = {}
        self.segments = 0

    def record(self, reply_type: str, latency_ms: float, *, end: bool = True) -> None:
        """按**完整响应**统计，不是按消息条数。

        阅读模式一次请求会下发 N 片，它们共享同一次模型调用和同一个延迟数字。
        逐片累加会让 P50 被重复计数扭曲，也会让模型调用数算错——W1 核成本时
        正是靠这个数字。
        """
        self.segments += 1
        if end:
            self.latencies.append(latency_ms)
            self.outcomes[reply_type] = self.outcomes.get(reply_type, 0) + 1

    def report(self, sent: int, wall: float) -> None:
        print("\n" + "=" * 58)
        print(f"发送 {sent} 帧，耗时 {wall:.1f}s")
        print(f"回复分布: {self.outcomes}")
        if self.segments > sum(self.outcomes.values()):
            print(f"下发消息数: {self.segments}（阅读模式分片）")

        replied = (
            self.outcomes.get("text", 0)
            + self.outcomes.get("voice", 0)
            + self.outcomes.get("alert", 0)
            + self.outcomes.get("read", 0)
        )
        noop = self.outcomes.get("noop", 0)
        if sent:
            print(f"闸门驳回率: {noop / sent:.1%}   （§12 目标区间 90%-97%）")
            print(f"实际模型调用: {replied} 次 / {sent} 帧")

        if self.latencies:
            ordered = sorted(self.latencies)
            p50 = statistics.median(ordered)
            p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
            print(f"延迟 P50={p50:.0f}ms  P95={p95:.0f}ms  max={max(ordered):.0f}ms")
            print("（§6 目标 P50<=1500ms  P95<=3000ms；mock 后端下应远低于此）")
        print("=" * 58)


async def run_ws(args) -> None:
    import websockets

    token = sign(args.device_id, args.secret)
    url = f"{args.url}/ws/glass/{args.device_id}?token={token}"
    stats = Stats()
    interval = 1.0 / args.fps if args.fps > 0 else 0.0

    print(f"连接 {url}")
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        start = time.perf_counter()

        async def receiver() -> None:
            # 按「完整响应」计数而不是消息条数：阅读模式一次请求会连续下发多片，
            # 只有 end=true 才算这一次结束。固件也必须这么做（docs/api.md §2.6）。
            completed = 0
            while completed < args.frames:
                try:
                    import json

                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                except TimeoutError:
                    print("  [超时] 没等到回复")
                    return
                stats.record(
                    reply.get("type", "?"),
                    reply.get("latency_ms", 0),
                    end=reply.get("end", True),
                )
                marker = {
                    "alert": "!!",
                    "voice": " *",
                    "text": "  ",
                    "noop": " ·",
                    "read": " >",
                }.get(reply.get("type", ""), " ?")
                total = reply.get("total", 1)
                position = f"{reply.get('index', 1)}/{total}" if total > 1 else ""
                print(
                    f"  {marker} seq={reply.get('seq'):>3} "
                    f"{reply.get('type'):<6} {reply.get('latency_ms'):>5}ms "
                    f"{position:>6}  {reply.get('content', '')}"
                )
                if reply.get("end", True):
                    completed += 1

        recv_task = asyncio.create_task(receiver())
        for i in range(args.frames):
            seed = i // max(1, args.scene_change_every)
            await ws.send(
                _frame_json(seed, seq=i, trigger=_trigger_of(args))
            )
            if interval:
                await asyncio.sleep(interval)
        await recv_task
        stats.report(args.frames, time.perf_counter() - start)


def _trigger_of(args) -> str:
    if args.read:
        return "read"
    return "manual" if args.manual else "auto"


def _frame_json(seed: int, *, seq: int, trigger: str) -> str:
    import json

    return json.dumps(
        {
            "type": "frame",
            "seq": seq,
            "ts": time.time(),
            "trigger": trigger,
            "image": base64.b64encode(make_frame(seed)).decode(),
        }
    )


async def run_http(args) -> None:
    import httpx

    stats = Stats()
    token = sign(args.device_id, args.secret)
    base = args.url.replace("ws://", "http://").replace("wss://", "https://")
    interval = 1.0 / args.fps if args.fps > 0 else 0.0
    start = time.perf_counter()

    async with httpx.AsyncClient(base_url=base, timeout=20.0) as client:
        for i in range(args.frames):
            seed = i // max(1, args.scene_change_every)
            resp = await client.post(
                "/v1/frame",
                json={
                    "device_id": args.device_id,
                    "seq": i,
                    "trigger": _trigger_of(args),
                    "image": base64.b64encode(make_frame(seed)).decode(),
                },
                headers={"X-Device-Token": token},
            )
            if resp.status_code != 200:
                print(f"  [{resp.status_code}] {resp.text[:120]}")
                continue
            reply = resp.json()
            stats.record(reply["type"], reply["latency_ms"])
            print(
                f"   seq={reply['seq']:>3} {reply['type']:<6} "
                f"{reply['latency_ms']:>5}ms  {reply['content']}"
            )
            # HTTP 单响应：阅读模式把全部分片放在 segments 里一次给全
            for n, piece in enumerate(reply.get("segments") or [], start=1):
                print(f"        {n:>2}/{len(reply['segments'])}  {piece}")
            if interval:
                await asyncio.sleep(interval)
    stats.report(args.frames, time.perf_counter() - start)


def main() -> None:
    parser = argparse.ArgumentParser(description="假眼镜推流器")
    parser.add_argument("--url", default="ws://127.0.0.1:8000")
    parser.add_argument("--device-id", default="fake-glass-01")
    parser.add_argument("--secret", default="dev-secret-change-me")
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--fps", type=float, default=2.0, help="推帧频率，0 表示不限速")
    parser.add_argument(
        "--scene-change-every", type=int, default=3,
        help="每 N 帧换一次场景，用来观察闸门去重效果",
    )
    parser.add_argument("--manual", action="store_true", help="全部标记为用户主动触发")
    parser.add_argument(
        "--read",
        action="store_true",
        help="阅读模式（trigger=read）：走 OCR 档位，回传连续分片。"
        "注意默认限流 6 次/分，--frames 调大会撞限流",
    )
    parser.add_argument("--http", action="store_true", help="走 HTTP 备用接口")
    args = parser.parse_args()

    asyncio.run(run_http(args) if args.http else run_ws(args))


if __name__ == "__main__":
    main()
