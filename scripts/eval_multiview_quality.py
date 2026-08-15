#!/usr/bin/env python3
"""多视角 + 画质敏感性实验（2026-08-11，qwen-vl-max）。

回答两个方法论问题：
1. 单帧评测是否低估/高估了真实能力？→ 把同一场景不同角度的帧组合输入
   （multi-view），并与单帧结果做多数一致（consensus）对比；
2. 画质影响多大？→ 对同一帧做 640→320→160 三档降采样，看指标怎么掉。

只用已人工核对过的 8 张证据帧，不新增任何内容。
产物：data/cup_walk/multiview_quality.json
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from urllib import error, request

from PIL import Image

MODEL = "qwen-vl-max"
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

PROMPT_SINGLE = """你是图像标注助手。严格根据这张第一视角图片回答，看不清或不确定就写"不确定"，绝对不要猜测或编造。
只输出 JSON：{"desk_objects":[{"name","color"}],"ground_objects":[{"name","color"}],"other_objects":[{"name","position"}],"has_mouse":bool,"desk_has_cup":bool,"notes":""}"""

PROMPT_MULTI = """以下是同一场景在不同时刻/角度拍摄的多张第一视角图片。请综合所有图片回答，某个物体只要在其中任意一张清晰可见就算存在；若某张里看不清，以能看清的那张为准。
只输出 JSON：{"desk_objects":[{"name","color"}],"ground_objects":[{"name","color"}],"other_objects":[{"name","position"}],"has_mouse":bool,"desk_has_cup":bool,"notes":""}"""

GT_BY_FRAME = {
    "frame_000162.jpg": {"desk": ["laptop"], "ground": ["pot"], "mouse": False},
    "frame_000183.jpg": {"desk": ["laptop"], "ground": ["pot"], "mouse": False},
    "frame_000276.jpg": {"desk": ["laptop", "cup"], "ground": [], "mouse": False},
    "frame_000486.jpg": {"desk": ["laptop", "cup"], "ground": [], "mouse": False},
}

QUALITY_FRAMES = ["frame_000162.jpg", "frame_000183.jpg", "frame_000276.jpg", "frame_000486.jpg"]
RESOLUTIONS = [640, 320, 160]

MULTIVIEW_GROUPS = {
    "group_A_laptop_and_pot": ["frame_000162.jpg", "frame_000177.jpg", "frame_000183.jpg"],
    "group_B_laptop_and_cup": ["frame_000276.jpg", "frame_000279.jpg", "frame_000486.jpg"],
}


def load_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("未找到 API key：请设置环境变量 DASHSCOPE_API_KEY")
    return key


def call_vlm(key: str, images: list[bytes], prompt: str, timeout_s: int = 120) -> str:
    content = []
    for jpeg in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()},
            }
        )
    content.append({"type": "text", "text": prompt})
    body = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 600,
        "messages": [{"role": "user", "content": content}],
    }
    req = request.Request(
        ENDPOINT,
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
        return f"__HTTP_ERROR__ {exc.code}: {exc.read().decode()[:200]}"
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


def names(items) -> list[str]:
    return [str(x.get("name", "")).lower() for x in (items or []) if isinstance(x, dict)]


def downscale(jpeg: bytes, max_edge: int) -> bytes:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()


def checks(parsed: dict | None, gt: dict) -> dict:
    if parsed is None:
        return {"laptop": False, "cup": False, "pot": False, "mouse_halluc": False}
    desk, ground = names(parsed.get("desk_objects")), names(parsed.get("ground_objects"))
    return {
        "laptop": any("laptop" in n or "笔记本" in n or "电脑" in n for n in desk),
        "cup": any("cup" in n or "杯" in n for n in desk),
        "pot": any(
            any(k in n for k in ["桶", "盆", "罐", "花", "花瓶", "bucket", "vase", "pot", "trash", "bin"])
            for n in ground
        ),
        "mouse_halluc": bool(parsed.get("has_mouse")) is True,
    }


def main() -> None:
    data_dir = Path("data/cup_walk")
    key = load_key()
    evidence = data_dir / "evidence"
    report: dict = {"model": MODEL, "quality": {}, "multiview": {}}

    # ---- 画质敏感性 ----
    for frame in QUALITY_FRAMES:
        orig = (evidence / frame).read_bytes()
        for edge in RESOLUTIONS:
            jpeg = orig if edge >= 640 else downscale(orig, edge)
            text = call_vlm(key, [jpeg], PROMPT_SINGLE)
            parsed = parse(text)
            c = checks(parsed, GT_BY_FRAME[frame])
            gt = GT_BY_FRAME[frame]
            correct = (
                c["laptop"] == ("laptop" in gt["desk"])
                and c["cup"] == ("cup" in gt["desk"])
                and c["pot"] == ("pot" in gt["ground"])
                and not c["mouse_halluc"]
            )
            report["quality"].setdefault(str(edge), {})[frame] = {
                "desk": names(parsed.get("desk_objects")) if parsed else [],
                "ground": names(parsed.get("ground_objects")) if parsed else [],
                "checks": c,
                "correct": correct,
            }
            print(f"  [Q] {frame} {edge}px -> {'✓' if correct else '✗'} {report['quality'][str(edge)][frame]['desk']} | {report['quality'][str(edge)][frame]['ground']}", flush=True)

    # ---- 多视角 ----
    single_raw = {}
    for line in open(data_dir / "vlm_raw_qwen_vl_max.jsonl", encoding="utf-8"):
        rec = json.loads(line)
        single_raw[rec["frame"]] = parse(rec["raw"])

    for group_name, frames in MULTIVIEW_GROUPS.items():
        imgs = [(evidence / f).read_bytes() for f in frames]
        text = call_vlm(key, imgs, PROMPT_MULTI)
        parsed = parse(text)
        # 单帧多数一致：三个单帧回答里 ≥2 帧一致才算
        votes_laptop = sum(1 for f in frames if checks(single_raw[f], {})["laptop"])
        votes_cup = sum(1 for f in frames if checks(single_raw[f], {})["cup"])
        consensus = {"laptop": votes_laptop >= 2, "cup": votes_cup >= 2}
        multi_checks = checks(parsed, {})
        report["multiview"][group_name] = {
            "frames": frames,
            "multi": {
                "desk": names(parsed.get("desk_objects")) if parsed else [],
                "ground": names(parsed.get("ground_objects")) if parsed else [],
                "checks": multi_checks,
                "raw": text[:200],
            },
            "consensus": consensus,
            "per_frame": {
                f: {"desk": names(single_raw[f].get("desk_objects")) if single_raw[f] else [],
                    "ground": names(single_raw[f].get("ground_objects")) if single_raw[f] else []}
                for f in frames
            },
        }
        print(f"  [V] {group_name} 多视角={'✓' if multi_checks['laptop'] or multi_checks['cup'] else '✗'} 共识={consensus}", flush=True)

    Path("data/cup_walk/multiview_quality.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("\nreport -> data/cup_walk/multiview_quality.json")

    print("\n== 画质敏感性（正确帧数/总帧数，4 帧 × 3 档）==")
    for edge in RESOLUTIONS:
        rows = report["quality"][str(edge)]
        ok = sum(1 for r in rows.values() if r["correct"])
        print(f"  {edge}px: {ok}/{len(rows)} 全对")
    print("\n== 多视角 vs 单帧共识 ==")
    for g, r in report["multiview"].items():
        print(f"  {g}: 多视角(桌面)={r['multi']['desk']} | 单帧共识={r['consensus']}")


if __name__ == "__main__":
    main()
