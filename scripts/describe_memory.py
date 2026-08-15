#!/usr/bin/env python3
"""M3 描述层：为已装配记忆生成属性/关系文本与双层合并（真实录制回放）。

对每个物体实例，从其证据帧（detections.jsonl bbox + frames/ 裁剪）取颜色、
从度量 3D 框取尺寸，按帧位姿生成 Layer 1 视图描述；多视角一致后合并出
Layer 2 场景级描述；再附上活动关系文本。

Usage:
    python scripts/describe_memory.py --data-dir data/cup_walk
    python scripts/describe_memory.py --data-dir data/cup_walk --out memory_descriptions.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from spatialmem.builder import build_memory_from_artifacts
from spatialmem.descriptions import (
    DescriptionAccumulator,
    box_dims_cm,
    dominant_color_name,
    pose_from_quat,
    relation_text,
    view_description,
    zh_direction,
    zh_label,
)
from spatialmem.relations import egocentric_direction
from spatialmem.quality import evaluate as evaluate_quality


def load_artifacts(data_dir: Path, poses_file: str = "poses_model0.jsonl") -> dict:
    def read_jsonl(name: str) -> list[dict]:
        path = data_dir / name
        if not path.exists():
            return []
        return [json.loads(line) for line in open(path)]

    return {
        "anchors": read_jsonl("anchors.jsonl"),
        "supports": read_jsonl("supports.jsonl"),
        "instances": read_jsonl("instances_focus.jsonl"),
        "detections": read_jsonl("detections.jsonl"),
        "poses": {
            row["frame_file"]: row for row in read_jsonl(poses_file)
        },
        "points_metric": np.load(data_dir / "metric_cloud.npz")["points_metric"],
    }


def crop_from_frame(
    frames_dir: Path, frame: str, bbox: list[float]
) -> np.ndarray | None:
    """按 bbox 裁剪帧，返回 RGB ndarray；越界/过小时返回 None。"""
    path = frames_dir / frame
    if not path.exists():
        return None
    img = Image.open(path).convert("RGB")
    x1, y1, x2, y2 = (int(v) for v in bbox)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img.width, x2), min(img.height, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return np.asarray(img.crop((x1, y1, x2, y2)))


def pick_detection(detections: list[dict], cls: str) -> dict | None:
    """同帧内同类检测取最高置信度。"""
    best = [d for d in detections if d["class"] == cls]
    return max(best, key=lambda d: d["conf"]) if best else None


def evidence_frames(inst: dict, max_views: int = 3) -> list[str]:
    first = inst.get("first_frame")
    last = inst.get("last_frame")
    if not first:
        return []
    if not last or last == first or max_views <= 1:
        return [first]
    try:
        n_first = int(Path(first).stem.split("_")[-1])
        n_last = int(Path(last).stem.split("_")[-1])
    except ValueError:
        return [first, last]
    mid = f"frame_{n_first + (n_last - n_first) // 2:06d}.jpg"
    return [first, mid, last][:max_views]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/cup_walk")
    ap.add_argument("--poses", default="poses_model0.jsonl",
                    help="位姿文件（COLMAP 多模型时指定，如 poses_model3.jsonl）")
    ap.add_argument("--out", default=None, help="输出 JSON（默认只打印样本）")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    frames_dir = data_dir / "frames"
    arts = load_artifacts(data_dir, args.poses)

    mem = build_memory_from_artifacts(
        anchors=arts["anchors"],
        supports=arts["supports"],
        instances=arts["instances"],
        points_metric=arts["points_metric"],
    )
    det_by_frame: dict[str, list[dict]] = {}
    for d in arts["detections"]:
        det_by_frame.setdefault(d["frame"], []).append(d)

    accum = DescriptionAccumulator()
    described: list[dict] = []
    for inst in arts["instances"]:
        node = mem.get_node(inst["instance_id"])
        if node is None or node.node_type != "object":
            continue
        size = box_dims_cm(node.box)
        layer1_last = ""
        views_total = 0
        views_accepted = 0
        rejected_reasons: list[str] = []
        for frame in evidence_frames(inst):
            views_total += 1
            frame_bytes = (frames_dir / frame).read_bytes()
            quality = evaluate_quality(frame_bytes)
            ok, reasons = quality.acceptable()
            if not ok:
                # 画质门控：低质量帧不进 VLM 描述（2026-08-11 实验结论，
                # 低画质诱发幻觉，宁可拒答）。
                rejected_reasons.extend(reasons)
                continue
            views_accepted += 1
            det = pick_detection(det_by_frame.get(frame, []), inst["class"])
            pose_row = arts["poses"].get(frame)
            color = None
            ego = None
            if det is not None and pose_row is not None:
                crop = crop_from_frame(frames_dir, frame, det["bbox"])
                if crop is not None:
                    color = dominant_color_name(crop)
                pose = pose_from_quat(
                    pose_row["qx"], pose_row["qy"], pose_row["qz"], pose_row["qw"],
                    pose_row["tx"], pose_row["ty"], pose_row["tz"],
                )
                ego = egocentric_direction(node.position(), pose)
            accum.observe(node.node_id, color, size)
            layer1_last = view_description(
                zh_label(node.label),
                color=color,
                box=node.box,
                direction_tag=zh_direction(ego["tag"] if ego else None),
                distance_m=ego["distance"] if ego else None,
            )
        node.layer1_text = layer1_last
        node.layer2_text = accum.layer2(node.node_id, zh_label(node.label))
        node.attributes["color"] = accum._colors.get(node.node_id, [None])[-1]
        node.attributes["size_cm"] = list(size)
        described.append(
            {
                "instance_id": node.node_id,
                "label": node.label,
                "label_zh": zh_label(node.label),
                "layer1_text": node.layer1_text,
                "layer2_text": node.layer2_text,
                "relations": relation_text(mem, node.node_id),
                "color": node.attributes.get("color"),
                "size_cm": node.attributes.get("size_cm"),
                "confidence": node.confidence,
                "n_observations": node.attributes.get("n_observations"),
                "quality": {
                    "views": views_total,
                    "accepted": views_accepted,
                    "rejected_reasons": sorted(set(rejected_reasons)),
                },
            }
        )

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(
                {
                    "scene": data_dir.name,
                    "objects": described,
                    "layers": {
                        "rule": "layer1=最新视角视图描述；layer2=多视角一致后写入（保守）",
                        "min_confirmations": accum._min_conf,
                        "window": accum._window,
                    },
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"descriptions -> {out_path}")

    print(f"\n== M3 描述样本（{data_dir.name}）==")
    for row in described:
        print(
            f"- {row['instance_id']} [{row['label_zh']}] conf={row['confidence']:.2f}"
        )
        q = row["quality"]
        print(f"    画质: 通过 {q['accepted']}/{q['views']} 视图"
              + (f"（拒绝: {q['rejected_reasons']}）" if q["rejected_reasons"] else ""))
        print(f"    L1: {row['layer1_text']}")
        print(f"    L2: {row['layer2_text'] or '（未达成多视角一致）'}")
        print(f"    关系: {row['relations'] or '（无活动关系）'}")


if __name__ == "__main__":
    main()
