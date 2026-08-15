#!/usr/bin/env python3
"""M5.2 机会式确认：候选 → 确认节点（离线验证）。

对 M5.1 候选池跑两级确认：
  1. 多帧一致：候选各观察帧的 VLM 语义同 (名称族, 颜色) ≥ 2 帧 → 自动升级；
  2. 交互确认：未自动升级的候选，模拟用户提问（对裁剪图重新调 VLM，
     标注 source=interactive）→ 升级（白风扇「口红式」流程）。

产物：confirmed_nodes.json（可喂给 M5.3 检索优先问答）。
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from PIL import Image

from spatialmem.candidates import crop_frame
from spatialmem.confirmation import Confirmation, decide_upgrade
from spatialmem.confirmation import SOURCE_WEIGHT


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/new_scene")
    ap.add_argument("--out", default="data/new_scene/confirmed_nodes.json")
    ap.add_argument("--min-views", type=int, default=2)
    ap.add_argument("--interactive-max", type=int, default=40,
                    help="交互模拟的最大候选数（控制 VLM 调用量）")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    frames_dir = data_dir / "frames"
    m5 = json.loads((data_dir / "m5_candidates.json").read_text(encoding="utf-8"))
    cache_path = data_dir / "vlm_semantic_cache.json"
    cache: dict = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    # 复用 run_m5_candidates 的语义描述函数（含缓存）
    import importlib.util
    import sys

    runner_path = Path(__file__).resolve().parent / "run_m5_candidates.py"
    spec = importlib.util.spec_from_file_location("m5runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    key = runner.load_key()

    def view_semantics(c: dict) -> list:
        out = []
        for ap_ in c["appearances"]:
            key_name = f"det:{ap_['frame']}:{c['label_hint']}"
            sem = cache.get(key_name)
            out.append(tuple(sem) if sem else None)
        return out

    def interactive_semantic(c: dict):
        """模拟用户提问：重新对候选裁剪图调 VLM，答案标注 source=interactive。"""
        if not c["appearances"]:
            return None
        first = c["appearances"][0]
        crop = crop_frame(frames_dir, first["frame"], first["bbox"])
        if crop is None:
            return None
        buf = io.BytesIO()
        Image.fromarray(crop).save(buf, format="JPEG", quality=85)
        sem = runner.vlm_semantic(key, buf.getvalue())
        if sem:
            # 交互答案写回缓存，避免重复调用
            cache[f"inter:{c['candidate_id']}"] = list(sem)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        return sem

    confirmed: list[dict] = []
    remaining: list[dict] = []
    stats = {"multi_view": 0, "interactive": 0, "remaining": 0}

    for c in m5["candidates"]:
        views = view_semantics(c)
        d = decide_upgrade(view_semantics=views, min_views=args.min_views)
        if d.upgrade:
            confirmed.append(
                {
                    "candidate_id": c["candidate_id"],
                    "label_hint": c["label_hint"],
                    "name": d.name,
                    "color": d.color,
                    "sources": d.sources,
                    "confidence": d.confidence,
                    "center": c["center"],
                    "appearances": c["appearances"],
                }
            )
            stats["multi_view"] += 1
            print(f"  [多帧] {c['candidate_id']} -> {d.name}/{d.color}（{d.sources}）")
            continue

        if stats["interactive"] + stats["multi_view"] >= 0 and (
            stats["interactive"] < args.interactive_max
        ):
            sem = interactive_semantic(c)
            if sem is not None:
                d2 = decide_upgrade(
                    view_semantics=views,
                    confirmations=[
                        Confirmation(source="interactive", name=sem[0], color=sem[1])
                    ],
                    min_views=args.min_views,
                )
                if d2.upgrade:
                    confirmed.append(
                        {
                            "candidate_id": c["candidate_id"],
                            "label_hint": c["label_hint"],
                            "name": d2.name,
                            "color": d2.color,
                            "sources": d2.sources,
                            "confidence": d2.confidence,
                            "center": c["center"],
                            "appearances": c["appearances"],
                        }
                    )
                    stats["interactive"] += 1
                    print(f"  [交互] {c['candidate_id']} -> {d2.name}/{d2.color}（{d2.sources}）")
                    continue
        remaining.append(
            {
                "candidate_id": c["candidate_id"],
                "label_hint": c["label_hint"],
                "semantic": c["semantic"],
                "n_observations": c["n_observations"],
                "first_seen": c["first_seen"],
                "last_seen": c["last_seen"],
            }
        )
        stats["remaining"] += 1

    out = {
        "data_dir": str(data_dir),
        "confirmed_nodes": confirmed,
        "remaining_candidates": remaining,
        "stats": stats,
        "rule": "确认源权重: multi_view=0.7, ocr=0.9, interactive=1.0；同(名称族,颜色)权重和≥1.0 升级",
    }
    if args.out:
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[M5.2] -> {args.out}")
    print(f"[M5.2] 统计: {stats}")


if __name__ == "__main__":
    main()
