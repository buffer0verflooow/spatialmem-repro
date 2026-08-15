#!/usr/bin/env python3
"""在线 VLM vs 本地检测对比实验（M4 补充，2026-08-11）。

输入：8 张人工核对过的证据帧 + 实例级 GT（qa_ground_truth.json 的结论）。
任务：让一个或多个在线 VLM 对每帧回答「桌面上有什么/地面有什么/有没有鼠标」，
与 YOLO11n（detections.jsonl）、开放词表 YOLO-World+CLIP
（detections_openvocab.jsonl）对比。

每个模型的原始回答存 data/cup_walk/vlm_raw_<model>.jsonl（已存在则跳过，
--force 重跑）；汇总报告输出到 --out。

VLM 密钥：通过环境变量 LINKSEE_API_KEY 提供（DashScope key，已验证可走
OpenAI 兼容 HTTP 接口）。请求只发证据帧本身，不发其它数据。

Usage:
    python scripts/eval_vlm_compare.py \
        --models qwen-vl-plus,qwen3-vl-plus,qwen-vl-max-latest,qwen3.5-omni-plus
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from pathlib import Path
from urllib import error, request

PROMPT = """你是图像标注助手。严格根据这张第一视角图片回答，看不清或不确定就写"不确定"，绝对不要猜测或编造画面里没有的东西。
只输出一个 JSON 对象，不要任何解释：
{
  "desk_objects": [{"name": "桌面上的物品名称", "color": "颜色"}],
  "ground_objects": [{"name": "地面上的物品名称", "color": "颜色"}],
  "other_objects": [{"name": "其他位置物品", "position": "位置说明"}],
  "has_mouse": false,
  "desk_has_cup": false,
  "notes": "一句话说明"
}
desk_objects 只列桌面/桌子表面的物品；ground_objects 只列地面物品；鼠标指电脑鼠标。"""

# 人工核对结论（qa_ground_truth.json note）：162–183 桌面=笔记本，地面=花筒/水桶；
# 276/279/486 桌面=笔记本+真杯子；全程无鼠标。
GT_BY_FRAME = {
    "frame_000162.jpg": {"desk": ["laptop"], "ground": ["flower_pot_or_bucket"], "mouse": False},
    "frame_000174.jpg": {"desk": ["laptop"], "ground": ["flower_pot_or_bucket"], "mouse": False},
    "frame_000177.jpg": {"desk": ["laptop"], "ground": ["flower_pot_or_bucket"], "mouse": False},
    "frame_000180.jpg": {"desk": ["laptop"], "ground": ["flower_pot_or_bucket"], "mouse": False},
    "frame_000183.jpg": {"desk": ["laptop"], "ground": ["flower_pot_or_bucket"], "mouse": False},
    "frame_000276.jpg": {"desk": ["laptop", "cup"], "ground": [], "mouse": False},
    "frame_000279.jpg": {"desk": ["laptop", "cup"], "ground": [], "mouse": False},
    "frame_000486.jpg": {"desk": ["laptop", "cup"], "ground": [], "mouse": False},
}

EVIDENCE_FRAMES = [
    "frame_000162.jpg", "frame_000174.jpg", "frame_000177.jpg",
    "frame_000180.jpg", "frame_000183.jpg", "frame_000276.jpg",
    "frame_000279.jpg", "frame_000486.jpg",
]


def load_key() -> str:
    key = os.environ.get("LINKSEE_API_KEY")
    if not key:
        raise SystemExit("未找到 API key：请设置环境变量 LINKSEE_API_KEY")
    return key


def call_vlm(key: str, image_path: Path, model: str, timeout_s: int = 120) -> str:
    image_b64 = base64.b64encode(image_path.read_bytes()).decode()
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 600,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": PROMPT},
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
        content = data["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    except error.HTTPError as exc:
        return f"__HTTP_ERROR__ {exc.code}: {exc.read().decode()[:200]}"
    except Exception as exc:  # noqa: BLE001
        return f"__ERROR__ {type(exc).__name__}: {exc}"


def parse_json_content(text: str) -> dict | None:
    if text.startswith("__"):
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    m = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def norm_names(obj_list: list) -> list[str]:
    out = []
    for o in obj_list or []:
        if isinstance(o, dict):
            out.append(str(o.get("name", "")).lower())
        elif isinstance(o, str):
            out.append(o.lower())
    return out


def raw_filename(model: str) -> str:
    return f"vlm_raw_{model.replace('.', '_').replace('-', '_')}.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/cup_walk")
    ap.add_argument("--models", default="qwen-vl-plus")
    ap.add_argument("--out", default="data/cup_walk/vlm_comparison_multi.json")
    ap.add_argument("--force", action="store_true", help="忽略已存在的原始回答，重新调用")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    key = load_key()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    raw_by_model: dict[str, dict[str, dict]] = {}
    for model in models:
        raw_path = data_dir / raw_filename(model)
        if raw_path.exists() and not args.force:
            print(f"  [SKIP] {model}: 已有原始回答，--force 可重跑")
        else:
            with open(raw_path, "w", encoding="utf-8") as f:
                for frame in EVIDENCE_FRAMES:
                    img = data_dir / "evidence" / frame
                    text = call_vlm(key, img, model)
                    f.write(json.dumps({"frame": frame, "model": model, "raw": text}, ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"  [VLM] {model} {frame} -> {len(text)} chars", flush=True)
        rows = {}
        for line in open(raw_path, encoding="utf-8"):
            rec = json.loads(line)
            rows[rec["frame"]] = rec
        raw_by_model[model] = rows

    report: dict = {"models": models, "per_model": {}, "summary": {}}
    for model in models:
        rows = raw_by_model[model]
        frames_out = []
        for frame in EVIDENCE_FRAMES:
            gt = GT_BY_FRAME[frame]
            rec = rows.get(frame, {"raw": "__MISSING__"})
            parsed = parse_json_content(rec["raw"])
            vlm_desk = norm_names(parsed.get("desk_objects")) if parsed else []
            vlm_ground = norm_names(parsed.get("ground_objects")) if parsed else []
            vlm_mouse = bool(parsed.get("has_mouse")) if parsed else None
            vlm_cup = bool(parsed.get("desk_has_cup")) if parsed else None

            desk_ok_laptop = any("laptop" in n or "笔记本" in n or "电脑" in n for n in vlm_desk)
            desk_ok_cup = any("cup" in n or "杯" in n for n in vlm_desk)
            ground_pot_ok = any(
                any(k in n for k in ["桶", "盆", "罐", "花", "花瓶", "bucket", "vase", "pot", "trash", "bin"])
                for n in vlm_ground
            )
            mouse_halluc = vlm_mouse is True
            frames_out.append(
                {
                    "frame": frame,
                    "gt": gt,
                    "vlm": {
                        "desk_objects": vlm_desk,
                        "ground_objects": vlm_ground,
                        "has_mouse": vlm_mouse,
                        "desk_has_cup": vlm_cup,
                        "raw_prefix": rec["raw"][:100],
                    },
                    "checks": {
                        "desk_laptop_found": desk_ok_laptop,
                        "desk_cup_found": desk_ok_cup,
                        "ground_pot_found": ground_pot_ok,
                        "mouse_hallucination": mouse_halluc,
                    },
                }
            )
        n_laptop = sum(1 for r in frames_out if r["checks"]["desk_laptop_found"])
        n_cup_frames = sum(1 for r in frames_out if "cup" in r["gt"]["desk"])
        n_cup = sum(
            1
            for r in frames_out
            if r["checks"]["desk_cup_found"] and "cup" in r["gt"]["desk"]
        )
        n_pot_frames = sum(1 for r in frames_out if "flower_pot_or_bucket" in r["gt"]["ground"])
        n_pot = sum(
            1
            for r in frames_out
            if r["checks"]["ground_pot_found"] and "flower_pot_or_bucket" in r["gt"]["ground"]
        )
        n_mouse_halluc = sum(1 for r in frames_out if r["checks"]["mouse_hallucination"])
        summary = {
            "desk_laptop_recall": f"{n_laptop}/{len(frames_out)}",
            "desk_cup_recall": f"{n_cup}/{n_cup_frames}",
            "ground_flowerpot_recall": f"{n_pot}/{n_pot_frames}",
            "mouse_hallucination_frames": n_mouse_halluc,
        }
        report["per_model"][model] = {"summary": summary, "frames": frames_out}
        report["summary"][model] = summary

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"report -> {args.out}")

    print("\n== 多模型对比报告 ==")
    print(f"{'指标':<28}" + "".join(f"{m:<24}" for m in models))
    for name, key_name in [
        ("桌面笔记本检出", "desk_laptop_recall"),
        ("桌面杯子检出(276/279/486)", "desk_cup_recall"),
        ("地面花筒/水桶检出(162–183)", "ground_flowerpot_recall"),
        ("鼠标幻觉帧数", "mouse_hallucination_frames"),
    ]:
        print(f"{name:<28}" + "".join(f"{report['summary'][m][key_name]:<24}" for m in models))
    print()
    for model in models:
        print(f"== {model} ==")
        for r in report["per_model"][model]["frames"]:
            c = r["checks"]
            marks = "".join(
                ("✓" if v else "✗")
                for v in [c["desk_laptop_found"], c["desk_cup_found"], c["ground_pot_found"], not c["mouse_hallucination"]]
            )
            print(
                f"  {r['frame']} [{marks}] 桌面={r['vlm']['desk_objects']} "
                f"地面={r['vlm']['ground_objects']} 鼠标={r['vlm']['has_mouse']}"
            )


if __name__ == "__main__":
    main()
