#!/usr/bin/env python3
"""M5.1 无感持续学习：候选池 + novelty 检测 + 同实例合并（离线验证）。

对已录制会话跑：
  1. 每个实例的证据裁剪图做 VLM 语义描述（名称+颜色，带缓存）；
  2. 同实例去重：语义一致 +（3D 邻近/时间窗重叠）→ 合并重复实例（chair 案例）；
  3. 候选池：未被实例覆盖的检测 → VLM 语义 → novelty 判定
     （白风扇应收进候选、黑椅子残留应跳过）。

Usage:
    python scripts/run_m5_candidates.py [--data-dir data/new_scene] [--out m5_candidates.json]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
from pathlib import Path
from urllib import error, request

import numpy as np
from PIL import Image

from spatialmem.candidates import (
    CandidatePool,
    crop_feature,
    crop_frame,
    merge_duplicate_instances,
)


def load_key() -> str:
    key = os.environ.get("LINKSEE_API_KEY")
    if not key:
        raise SystemExit("未找到 API key：请设置环境变量 LINKSEE_API_KEY")
    return key


def vlm_semantic(key: str, jpeg: bytes) -> tuple[str, str] | None:
    """裁剪图 → (名称, 颜色)；解析失败返回 None。"""
    body = {
        "model": "qwen-vl-max",
        "temperature": 0.1,
        "max_tokens": 80,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
                        },
                    },
                    {
                        "type": "text",
                        "text": '图中框内物体是什么？什么颜色？只输出 JSON {"name":"","color":""}',
                    },
                ],
            }
        ],
    }
    req = request.Request(
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            text = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(m.group(0)) if m else {}
        name = str(parsed.get("name", "")).strip()
        color = str(parsed.get("color", "")).strip()
        if not name or name == "不确定":
            return None
        return name, color
    except Exception:  # noqa: BLE001
        return None


def frame_n(frame: str) -> int:
    return int(Path(frame).stem.split("_")[-1])


def semantic_cache_path(data_dir: Path) -> Path:
    return data_dir / "vlm_semantic_cache.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/new_scene")
    ap.add_argument("--out", default="data/new_scene/m5_candidates.json")
    ap.add_argument("--conf", type=float, default=0.45, help="候选检测置信度下限")
    ap.add_argument("--skip-vlm", action="store_true", help="不调用 VLM（用缓存/无语义）")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    frames_dir = data_dir / "frames"
    key = None if args.skip_vlm else load_key()

    instances = [json.loads(l) for l in open(data_dir / "instances_focus.jsonl")]
    detections = [json.loads(l) for l in open(data_dir / "detections.jsonl")]
    det_by_frame: dict[str, list[dict]] = {}
    for d in detections:
        det_by_frame.setdefault(d["frame"], []).append(d)
    lifted = []
    lifted_path = data_dir / "objects3d_depth.jsonl"
    if lifted_path.exists():
        lifted = [json.loads(l) for l in open(lifted_path)]

    cache_path = semantic_cache_path(data_dir)
    cache: dict[str, list] = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    def describe(frame: str, bbox, crop_key: str) -> tuple[str, str] | None:
        if key is None:
            return tuple(cache.get(crop_key)) if crop_key in cache else None
        if crop_key in cache:
            return tuple(cache[crop_key]) if cache[crop_key] else None
        crop = crop_frame(frames_dir, frame, bbox)
        if crop is None:
            return None
        buf = io.BytesIO()
        Image.fromarray(crop).save(buf, format="JPEG", quality=85)
        sem = vlm_semantic(key, buf.getvalue())
        cache[crop_key] = list(sem) if sem else None
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        return sem

    # ---- 1. 实例语义 ----
    semantics: dict[str, tuple[str, str] | None] = {}
    for inst in instances:
        ev = inst.get("evidence") or {}
        frame, bbox = ev.get("frame", ""), ev.get("bbox2d") or [0, 0, 0, 0]
        if not frame or not bbox or bbox[2] - bbox[0] < 4:
            # 旧管线实例无 evidence：用 first_frame 内同类最高置信检测框兜底。
            cands = [
                d for d in det_by_frame.get(inst.get("first_frame", ""), [])
                if d["class"] == inst["class"]
            ]
            if cands:
                best = max(cands, key=lambda d: d["conf"])
                frame, bbox = best["frame"], best["bbox"]
                inst["evidence"] = {
                    "frame": frame, "class": inst["class"],
                    "conf": best["conf"], "bbox2d": bbox,
                }
        semantics[inst["instance_id"]] = describe(
            frame, bbox,
            f"inst:{inst['instance_id']}",
        )

    # ---- 2. 同实例去重 ----
    merged_instances, merge_report = merge_duplicate_instances(
        instances, frames_dir=frames_dir, semantics=semantics
    )
    print(f"[M5.1] 实例去重: {len(instances)} -> {len(merged_instances)}")
    for r in merge_report:
        print(f"  合并: {r['absorbed']} -> {r['keep']}（{r['class']}）")

    # ---- 3. 候选池 ----
    known_features: list[tuple[str, np.ndarray]] = []
    known_semantics: list[tuple[str, tuple[str, str] | None]] = []
    for inst in merged_instances:
        ev = inst.get("evidence") or {}
        crop = crop_frame(frames_dir, ev.get("frame", ""), ev.get("bbox2d") or [0, 0, 0, 0])
        if crop is not None:
            known_features.append((inst["instance_id"], crop_feature(crop)))
        known_semantics.append((inst["instance_id"], semantics.get(inst["instance_id"])))

    def covered(d: dict) -> bool:
        n = frame_n(d["frame"])
        cx = (d["bbox"][0] + d["bbox"][2]) / 2
        cy = (d["bbox"][1] + d["bbox"][3]) / 2
        for inst in merged_instances:
            if inst["class"] != d["class"]:
                continue
            if not (frame_n(inst["first_frame"]) <= n <= frame_n(inst["last_frame"])):
                continue
            ev = inst.get("evidence") or {}
            if ev.get("frame") == d["frame"] and ev.get("bbox2d"):
                b = ev["bbox2d"]
                pad_x = (b[2] - b[0]) * 0.5
                pad_y = (b[3] - b[1]) * 0.5
                if (b[0] - pad_x <= cx <= b[2] + pad_x) and (
                    b[1] - pad_y <= cy <= b[3] + pad_y
                ):
                    return True
        return False

    pool = CandidatePool(
        known_features=known_features,
        known_semantics=known_semantics,
    )
    for d in detections:
        if d["conf"] < args.conf or covered(d):
            continue
        crop = crop_frame(frames_dir, d["frame"], d["bbox"])
        if crop is None:
            continue
        sem = describe(d["frame"], d["bbox"], f"det:{d['frame']}:{d['class']}")
        pool.add_or_merge(
            feature=crop_feature(crop),
            frame=d["frame"],
            bbox=d["bbox"],
            crop_file=f"{d['frame']}",
            label_hint=d["class"],
            semantic=sem,
        )

    out = {
        "data_dir": str(data_dir),
        "conf_threshold": args.conf,
        "merge_report": merge_report,
        "merged_instances": [
            {
                "instance_id": i["instance_id"],
                "class": i["class"],
                "center": [round(v, 2) for v in i["center"]],
                "n_observations": i["n_observations"],
                "first_frame": i["first_frame"],
                "last_frame": i["last_frame"],
                "merged_from": i.get("merged_from", []),
                "semantic": semantics.get(i["instance_id"]),
            }
            for i in merged_instances
        ],
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "label_hint": c.label_hint,
                "semantic": c.semantic,
                "n_observations": c.n_observations,
                "first_seen": c.first_seen,
                "last_seen": c.last_seen,
                "center": [round(v, 2) for v in c.center] if c.center else None,
                "appearances": c.appearances,
            }
            for c in pool.candidates()
        ],
        "pool_stats": pool.stats,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[M5.1] -> {args.out}")

    print(f"[M5.1] 候选池统计: {pool.stats}")
    for c in pool.candidates():
        print(
            f"  候选 {c.candidate_id} [{c.label_hint}] 语义={c.semantic} "
            f"观察 {c.n_observations} 次 {c.first_seen} ~ {c.last_seen}"
        )


if __name__ == "__main__":
    main()
