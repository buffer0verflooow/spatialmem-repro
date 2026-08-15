#!/usr/bin/env python3
"""M5.4 视觉再识别 + 遗忘/纠正（离线验证）。

用例：
  1. 再识别（同物跨帧/跨视角）：
     - 白风扇 619（VLM 叫「马桶」）vs 628（叫「电风扇」）→ 应弱匹配为同一物体；
     - 黑椅子 chair_6 vs chair_8（语义一致）→ 强匹配；
     - 跨场景负例：new_scene 风扇 vs cup_walk 杯子 → 不匹配；
  2. 纠正：用户说「不是电风扇，是暖风机」→ 标签更新 + 置信降档 + 日志；
  3. 移动：风扇位置 A→B → 旧位置带时间戳归档；
  4. 陈旧：长时间未再见的节点标记 stale。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from spatialmem.candidates import crop_frame, crop_feature
from spatialmem.memory_lifecycle import apply_correction, move_node, stale_status
from spatialmem.reid import ReidEntry, reidentify


def crop_semantic_feature(data_dir: Path, frame: str, bbox):
    crop = crop_frame(data_dir / "frames", frame, bbox)
    if crop is None:
        return None, None
    return crop_feature(crop), crop


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/new_scene")
    ap.add_argument("--out", default="data/new_scene/m5_reid.json")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    cache_path = data_dir / "vlm_semantic_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    confirmed = json.loads(
        (data_dir / "confirmed_nodes.json").read_text(encoding="utf-8")
    )["confirmed_nodes"]

    def node_entry(c: dict) -> ReidEntry:
        feat, _ = crop_semantic_feature(
            data_dir, c["appearances"][0]["frame"], c["appearances"][0]["bbox"]
        ) if c.get("appearances") else (None, None)
        sem = (c.get("name"), c.get("color"))
        return ReidEntry(c["candidate_id"], feat, sem if sem[0] else None)

    entries = [node_entry(c) for c in confirmed]

    report: dict = {"cases": [], "corrections": [], "moves": [], "stale": {}}

    # ---- 1. 再识别 ----
    fan619_feat, _ = crop_semantic_feature(
        data_dir, "frame_000619.jpg", [483, 362, 755, 462]
    )
    fan628 = next((e for e in entries if e.node_id == "cand_35"), None)
    r_fan = reidentify(
        fan619_feat, ("马桶", "白色"), [fan628] if fan628 else []
    )
    report["cases"].append(
        {"name": "风扇619 vs 风扇628", "match": r_fan.match_id, "tier": r_fan.tier,
         "sim": round(r_fan.similarity, 4), "expect": "cand_35/weak"}
    )

    chair8_feat, _ = crop_semantic_feature(
        data_dir, "frame_000595.jpg", [395, 27, 646, 272]
    )
    chair6 = next(
        (ReidEntry("chair_6", None, ("椅子", "黑色")) for _ in [0]), None
    )
    r_chair = reidentify(chair8_feat, ("办公椅", "黑色"), [chair6])
    report["cases"].append(
        {"name": "chair8 vs chair6", "match": r_chair.match_id, "tier": r_chair.tier,
         "sim": round(r_chair.similarity, 4), "expect": "chair_6/strong"}
    )

    cup_path = Path("data/cup_walk/frames/frame_000352.jpg")
    if cup_path.exists():
        cup_feat = crop_feature(crop_frame(cup_path.parent, cup_path.name, [313, 296, 354, 334]))
        r_cross = reidentify(cup_feat, ("杯子", "白色"), [fan628] if fan628 else [])
        report["cases"].append(
            {"name": "cup_walk 杯子 vs new_scene 风扇", "match": r_cross.match_id,
             "tier": r_cross.tier, "sim": round(r_cross.similarity, 4), "expect": "none"}
        )

    # ---- 2. 纠正 ----
    fan_node = next((c for c in confirmed if c["candidate_id"] == "cand_35"), None)
    if fan_node:
        corrected, log = apply_correction(
            fan_node, name="暖风机", color="白色", source="interactive", t_s=1786000000.0
        )
        report["corrections"].append(
            {
                "old_name": log.old_name, "new_name": log.new_name,
                "old_conf": fan_node.get("confidence"),
                "new_conf": corrected.get("confidence"),
                "sources": corrected.get("sources"),
            }
        )

    # ---- 3. 移动 ----
    if fan_node:
        moved, archived = move_node(fan_node, [3.0, 4.0, 0.2], t_s=1786000100.0)
        report["moves"].append(
            {
                "old_center": fan_node.get("center"),
                "new_center": moved.get("center"),
                "archived": archived.old_center if archived else None,
                "archived_at_s": archived.archived_at_s if archived else None,
            }
        )

    # ---- 4. 陈旧 ----
    nodes = [
        {"node_id": "cand_35", "last_seen_s": 100.0},
        {"node_id": "cand_01", "last_seen_s": 1786000000.0},
    ]
    report["stale"] = stale_status(nodes, now_s=1786005000.0, max_age_s=3600.0)

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[M5.4] -> {args.out}")

    print("== M5.4 再识别 / 遗忘 / 纠正 ==")
    for case in report["cases"]:
        ok = (
            (case["tier"] == "weak" and case["match"] == "cand_35")
            if case["expect"].startswith("cand_35")
            else (case["tier"] == "strong" and case["match"] == "chair_6")
            if case["expect"].startswith("chair_6")
            else case["tier"] == "none"
        )
        print(f"  {'✓' if ok else '✗'} {case['name']}: {case['tier']}/{case['match']} sim={case['sim']}")
    print("  纠正:", report["corrections"])
    print("  移动:", report["moves"])
    print("  陈旧:", report["stale"])


if __name__ == "__main__":
    main()
