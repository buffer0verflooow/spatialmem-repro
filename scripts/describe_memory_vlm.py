#!/usr/bin/env python3
"""M3 描述层 · VLM 版：关键帧 + 跨帧共识生成属性（2026-08-11）。

与 describe_memory.py（HSV 启发式）的区别：
- 每个实例取最多 3 个关键帧（首/中/末，先过画质门控）；
- 每帧用 qwen-vl-max 聚焦 bbox 区域回答「这是什么、什么颜色、置信度」；
- Layer 2 跨帧共识：名称/颜色取多数一致（≥2/3）才写入，否则保守留空并标注
  不确定——单帧 VLM 有抖动与幻觉，必须靠共识兜住；
- 尺寸仍用度量 3D 框（几何层，VLM 给不了度量值）。

Usage:
    python scripts/describe_memory_vlm.py [--data-dir data/cup_walk]
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import re
from pathlib import Path
from urllib import error, request

from spatialmem.builder import build_memory_from_artifacts
from spatialmem.descriptions import box_dims_cm, size_text_cm, zh_label
from spatialmem.quality import evaluate as evaluate_quality

PROMPT = """这张第一视角图片中，大约位于 [x1={x1}, y1={y1}, x2={x2}, y2={y2}]（像素坐标）的物体是什么？
只输出 JSON：{{"name": "物体名称", "color": "主要颜色", "confidence": 0.0}}
name 用中文；看不清就写 "不确定"，不要猜测。"""


def load_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("未找到 API key：请设置环境变量 DASHSCOPE_API_KEY")
    return key


def call_vlm(key: str, jpeg: bytes, prompt: str, model: str, timeout_s: int = 90) -> str:
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 120,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
                    {"type": "text", "text": prompt},
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
        with request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode())
        text = data["choices"][0]["message"]["content"]
        return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    except error.HTTPError as exc:
        return f"__HTTP_ERROR__ {exc.code}: {exc.read().decode()[:150]}"
    except Exception as exc:  # noqa: BLE001
        return f"__ERROR__ {type(exc).__name__}: {exc}"


def parse(text: str) -> dict | None:
    if text.startswith("__"):
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def evidence_frames(inst: dict, max_views: int = 3) -> list[str]:
    first = inst.get("first_frame")
    last = inst.get("last_frame")
    if not first:
        return []
    if not last or last == first:
        return [first]
    try:
        n1 = int(Path(first).stem.split("_")[-1])
        n2 = int(Path(last).stem.split("_")[-1])
    except ValueError:
        return [first, last]
    mid = f"frame_{n1 + (n2 - n1) // 2:06d}.jpg"
    return [first, mid, last][:max_views]


def consensus(values: list[str]) -> tuple[str | None, bool]:
    """多数一致：≥2/3 同一值才返回 (值, True)；否则 (None, False)。"""
    if not values:
        return None, False
    counter = collections.Counter(v for v in values if v and v != "不确定")
    top, count = counter.most_common(1)[0] if counter else (None, 0)
    need = max(2, (len(values) + 1) // 2)
    if top is not None and count >= need:
        return top, True
    return None, False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/cup_walk")
    ap.add_argument("--poses", default="poses_model0.jsonl",
                    help="位姿文件（COLMAP 多模型时指定，如 poses_model3.jsonl）")
    ap.add_argument("--model", default="qwen-vl-max")
    ap.add_argument("--out", default="data/cup_walk/memory_descriptions_vlm.json")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    frames_dir = data_dir / "frames"
    key = load_key()

    def read_jsonl(name: str) -> list[dict]:
        p = data_dir / name
        return [json.loads(line) for line in open(p)] if p.exists() else []

    import numpy as np

    arts = {
        "anchors": read_jsonl("anchors.jsonl"),
        "supports": read_jsonl("supports.jsonl"),
        "instances": read_jsonl("instances_focus.jsonl"),
        "detections": read_jsonl("detections.jsonl"),
        "poses": {
            row["frame_file"]: row for row in read_jsonl(args.poses)
        },
        "points": np.load(data_dir / "metric_cloud.npz")["points_metric"],
    }
    mem = build_memory_from_artifacts(
        anchors=arts["anchors"],
        supports=arts["supports"],
        instances=arts["instances"],
        points_metric=arts["points"],
    )
    det_by_frame: dict[str, list[dict]] = {}
    for d in arts["detections"]:
        det_by_frame.setdefault(d["frame"], []).append(d)

    raw_path = data_dir / f"vlm_desc_raw_{args.model.replace('.', '_').replace('-', '_')}.jsonl"
    results = []
    with open(raw_path, "w", encoding="utf-8") as rawf:
        for inst in arts["instances"]:
            node = mem.get_node(inst["instance_id"])
            if node is None or node.node_type != "object":
                continue
            views: list[dict] = []
            for frame in evidence_frames(inst):
                jpeg = (frames_dir / frame).read_bytes()
                quality = evaluate_quality(jpeg)
                ok, reasons = quality.acceptable()
                if not ok:
                    views.append({"frame": frame, "quality_rejected": reasons})
                    continue
                dets = [d for d in det_by_frame.get(frame, []) if d["class"] == inst["class"]]
                if not dets:
                    views.append({"frame": frame, "quality_rejected": ["no_detection"]})
                    continue
                det = max(dets, key=lambda d: d["conf"])
                x1, y1, x2, y2 = (int(v) for v in det["bbox"])
                prompt = PROMPT.format(x1=x1, y1=y1, x2=x2, y2=y2)
                text = call_vlm(key, jpeg, prompt, args.model)
                rawf.write(json.dumps({"instance": inst["instance_id"], "frame": frame, "raw": text}, ensure_ascii=False) + "\n")
                rawf.flush()
                parsed = parse(text)
                views.append(
                    {
                        "frame": frame,
                        "name": parsed.get("name") if parsed else None,
                        "color": parsed.get("color") if parsed else None,
                        "confidence": parsed.get("confidence") if parsed else None,
                        "raw": text[:80],
                    }
                )
                print(f"  [VLM] {inst['instance_id']} {frame} -> "
                      f"{views[-1].get('name')} / {views[-1].get('color')}", flush=True)

            accepted = [v for v in views if "name" in v]
            names = [v["name"] for v in accepted]
            colors = [v["color"] for v in accepted]
            name, name_ok = consensus(names)
            color, color_ok = consensus(colors)
            size = box_dims_cm(node.box)
            layer2_parts = []
            if name_ok:
                layer2_parts.append(f"{color}的{name}" if color_ok else name)
            elif color_ok:
                layer2_parts.append(f"颜色{color}")
            layer2_parts.append(size_text_cm(node.box))
            layer2_text = "，".join(layer2_parts) if layer2_parts else ""
            results.append(
                {
                    "instance_id": node.node_id,
                    "label": node.label,
                    "label_zh": zh_label(node.label),
                    "vlm_name": name,
                    "vlm_color": color,
                    "consensus_ok": name_ok,
                    "layer2_text": layer2_text,
                    "size_cm": list(size),
                    "views": views,
                }
            )

    Path(args.out).write_text(
        json.dumps({"scene": data_dir.name, "model": args.model, "objects": results}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"\ndescriptions_vlm -> {args.out}")
    print("\n== VLM 描述样本 ==")
    for r in results:
        state = "✓共识" if r["consensus_ok"] else "✗未共识"
        print(
            f"- {r['instance_id']} [{r['label_zh']}] {state} "
            f"VLM名={r['vlm_name']} 色={r['vlm_color']} | L2: {r['layer2_text']}"
        )


if __name__ == "__main__":
    main()
