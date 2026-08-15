#!/usr/bin/env python3
"""Run YOLO-World v2 (open-vocabulary) detection with CLIP ViT-B/32 text embeddings.

Drop-in replacement for run_detections.py using an open vocabulary, so classes
like "水桶/垃圾桶" no longer have to be squeezed into COCO80 categories.

Usage:
    python scripts/run_open_vocab.py <frames_dir> <out.jsonl> \
        [--model data/models/yolov8s-worldv2.onnx] \
        [--clip data/models/clip/text_model.onnx] \
        [--clip-tokenizer data/models/clip/tokenizer.json] \
        [--text "laptop, 水桶, 垃圾桶, ..."] \
        [--conf 0.15] [--iou 0.45] [--step 3] [--rotate 90] \
        [--frames frame_000162,frame_000486] [--draw-dir evidence/]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


def load_clip_text_encoder(clip_onnx: str, tokenizer_json: str):
    """Return (session, tokenize_fn) producing L2-normalized (N,512) embeddings."""
    session = ort.InferenceSession(clip_onnx, providers=["CPUExecutionProvider"])
    tokenizer = Tokenizer.from_file(tokenizer_json)
    sot = tokenizer.token_to_id("<|startoftext|>")
    eot = tokenizer.token_to_id("<|endoftext|>")
    pad = 1  # CLIP pad_token_id
    ctx = 77

    def tokenize(texts: list[str]) -> np.ndarray:
        rows = []
        for t in texts:
            ids = [sot] + tokenizer.encode(t).ids + [eot]
            ids = (ids + [pad] * ctx)[:ctx]
            rows.append(ids)
        return np.asarray(rows, dtype=np.int64)

    def encode(texts: list[str]) -> np.ndarray:
        feats = session.run(None, {"input_ids": tokenize(texts)})[0]
        feats = feats / np.linalg.norm(feats, axis=-1, keepdims=True)
        return feats

    return encode


def letterbox(img: np.ndarray, size: int = 640) -> tuple[np.ndarray, float, tuple[int, int]]:
    h, w = img.shape[:2]
    s = size / max(h, w)
    nh, nw = round(h * s), round(w * s)
    resized = cv2.resize(img, (nw, nh))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_x = (size - nw) // 2
    pad_y = (size - nh) // 2
    canvas[pad_y : pad_y + nh, pad_x : pad_x + nw] = resized
    return canvas, s, (pad_x, pad_y)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """Class-agnostic NMS. boxes: (N,4) xyxy."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        union = areas[i] + areas[order[1:]] - inter
        iou = np.where(union > 0, inter / union, 0)
        order = order[1:][iou <= iou_thr]
    return keep


def postprocess(
    out: np.ndarray,
    texts: list[str],
    s: float,
    pad: tuple[int, int],
    orig: tuple[int, int],
    conf_thr: float,
    iou_thr: float,
) -> list[dict]:
    """out: (1, 4+N, anchors) -> list of {class, conf, bbox(xyxy on orig)}."""
    pred = out[0]
    boxes_xywh = pred[:4].T  # (A,4) center-x, center-y, w, h (letterbox coords)
    # The exported graph already applies sigmoid to the class branch
    # (node sigmoid_4 feeds cat_20), so these are final scores in [0, 1].
    scores = pred[4:].T  # (A, N)
    H, W = orig
    dets = []
    for ci, name in enumerate(texts):
        sc = scores[:, ci]
        idx = np.where(sc >= conf_thr)[0]
        if len(idx) == 0:
            continue
        bx = boxes_xywh[idx]
        cx, cy, bw, bh = bx.T
        x1 = (cx - bw / 2 - pad[0]) / s
        y1 = (cy - bh / 2 - pad[1]) / s
        x2 = (cx + bw / 2 - pad[0]) / s
        y2 = (cy + bh / 2 - pad[1]) / s
        xyxy = np.stack([x1, y1, x2, y2], axis=1)
        keep = nms(xyxy, sc[idx], iou_thr)
        for k in keep:
            b = xyxy[k]
            dets.append(
                {
                    "class_id": ci,  # index in the prompt list (open-vocab)
                    "class": name,
                    "conf": float(sc[idx[k]]),
                    "bbox": [
                        float(max(0.0, b[0])),
                        float(max(0.0, b[1])),
                        float(min(W, b[2])),
                        float(min(H, b[3])),
                    ],
                }
            )
    dets.sort(key=lambda d: d["conf"], reverse=True)
    return dets


def draw_dets(img: np.ndarray, dets: list[dict]) -> np.ndarray:
    colors = {
        "laptop": (0, 200, 0),
        "水桶": (0, 120, 255),
        "垃圾桶": (0, 0, 255),
        "杯子": (255, 120, 0),
    }
    for d in dets:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        color = colors.get(d["class"], (255, 0, 255))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        label = f"{d['class']} {d['conf']:.2f}"
        cv2.putText(img, label, (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir")
    ap.add_argument("out_jsonl")
    ap.add_argument("--model", default="data/models/yolov8s-worldv2.onnx")
    ap.add_argument("--clip", default="data/models/clip/text_model.onnx")
    ap.add_argument("--clip-tokenizer", default="data/models/clip/tokenizer.json")
    ap.add_argument(
        "--text",
        default="laptop, cell phone, chair, 水桶, 垃圾桶, 杯子, 花筒, 花盆, "
        "遥控器, 鼠标, 键盘, 书本, 台灯, 枕头, 被子, 桌子, 茶几, 沙发, 电视, "
        "垃圾桶, bucket, plastic bucket, water bucket, trash can, trash bin, "
        "vase, flower vase, flower pot, cup, mug, bottle, remote, mouse, "
        "pillow, blanket, table, sofa, tv, yoga mat, exercise mat",
    )
    ap.add_argument("--conf", type=float, default=0.15)
    ap.add_argument("--iou", type=float, default=0.45)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0)
    ap.add_argument("--frames", default="")  # e.g. frame_000162,frame_000486
    ap.add_argument("--frame-list", default="")  # metric_cloud.npz -> use registered frames
    ap.add_argument("--draw-dir", default="")
    args = ap.parse_args()

    texts = [t.strip() for t in args.text.split(",") if t.strip()]
    print(f"[open_vocab] {len(texts)} classes: {texts}")

    encode_texts = load_clip_text_encoder(args.clip, args.clip_tokenizer)
    txt_feats = encode_texts(texts)[None, ...]  # (1, N, 512)
    world = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])

    frames_dir = Path(args.frames_dir)
    if args.frame_list:
        data = np.load(args.frame_list, allow_pickle=True)
        names = [str(n) for n in data["frame_names"]]
        frame_files = [frames_dir / n for n in names]
        frame_files = [f for i, f in enumerate(frame_files) if i % args.step == 0]
    else:
        frame_files = sorted(frames_dir.glob("*.jpg"))
        if not args.frames:
            frame_files = [f for i, f in enumerate(frame_files) if i % args.step == 0]
    if args.frames:
        want = {f for f in args.frames.split(",") if f}
        frame_files = [f for f in frame_files if f.name in want]

    draw_dir = Path(args.draw_dir) if args.draw_dir else None
    if draw_dir:
        draw_dir.mkdir(parents=True, exist_ok=True)

    with open(args.out_jsonl, "w") as fh:
        for fpath in frame_files:
            img = cv2.imread(str(fpath))
            if img is None:
                continue
            orig_img = img
            if args.rotate:
                rot = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[args.rotate]
                img = cv2.rotate(img, rot)
            H, W = img.shape[:2]
            canvas, s, pad = letterbox(img)
            inp = (canvas.astype(np.float32) / 255.0).transpose(2, 0, 1)[None, ...]
            out = world.run(None, {"images": inp, "txt_feats": txt_feats})[0]
            dets = postprocess(out, texts, s, pad, (H, W), args.conf, args.iou)
            if args.rotate == 90:
                # rotated (u_rot, v_rot) -> original (u, v): u=v_rot, v=H0-1-u_rot
                H0 = orig_img.shape[0]
                for d in dets:
                    x1, y1, x2, y2 = d["bbox"]
                    d["bbox"] = [float(y1), float(H0 - 1 - x2), float(y2), float(H0 - 1 - x1)]
            for d in dets:
                d["frame"] = fpath.name
            fh.write("\n".join(json.dumps(d, ensure_ascii=False) for d in dets) + ("\n" if dets else ""))
            if draw_dir and dets:
                ann = draw_dets(orig_img.copy(), dets)
                cv2.imwrite(str(draw_dir / fpath.name), ann)
            top = " | ".join(f"{d['class']} {d['conf']:.2f}" for d in dets[:5]) or "-"
            print(f"{fpath.name}: {top}")


if __name__ == "__main__":
    main()
