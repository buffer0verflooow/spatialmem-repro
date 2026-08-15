#!/usr/bin/env python3
"""Run YOLO11n (COCO80) on registered frames, subsampled.

Usage:
    python scripts/run_detections.py <frames_dir> <metric_cloud.npz> \
        <detections.jsonl> [--model yolo11n.onnx] [--step 3] [--conf 0.35]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]


def letterbox(img: np.ndarray, size: int = 640) -> tuple[np.ndarray, float, float]:
    h, w = img.shape[:2]
    s = size / max(h, w)
    nh, nw = round(h * s), round(w * s)
    resized = cv2.resize(img, (nw, nh))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_x = (size - nw) // 2
    pad_y = (size - nh) // 2
    canvas[pad_y : pad_y + nh, pad_x : pad_x + nw] = resized
    return canvas, s, (pad_x, pad_y)


def postprocess(
    out: np.ndarray,
    s: float,
    pad: tuple[float, float],
    orig: tuple[int, int],
    conf_thr: float,
) -> list[dict]:
    """out: (1,84,8400) -> list of {class_id, conf, bbox(xyxy on orig)}."""
    preds = out[0].T  # (8400, 84)
    boxes_xywh = preds[:, :4]
    scores = preds[:, 4:]
    cls = scores.argmax(axis=1)
    conf = scores.max(axis=1)
    keep = conf >= conf_thr
    boxes_xywh, cls, conf = boxes_xywh[keep], cls[keep], conf[keep]
    if len(cls) == 0:
        return []
    # xywh (letterbox coords) -> xyxy (orig coords)
    cx, cy, w, h = boxes_xywh.T
    x1 = (cx - w / 2 - pad[0]) / s
    y1 = (cy - h / 2 - pad[1]) / s
    x2 = (cx + w / 2 - pad[0]) / s
    y2 = (cy + h / 2 - pad[1]) / s
    H, W = orig
    dets = []
    for i in range(len(cls)):
        dets.append(
            {
                "class_id": int(cls[i]),
                "class": COCO_NAMES[int(cls[i])],
                "conf": float(conf[i]),
                "bbox": [
                    float(max(0, x1[i])),
                    float(max(0, y1[i])),
                    float(min(W, x2[i])),
                    float(min(H, y2[i])),
                ],
            }
        )
    # NMS
    if dets:
        boxes = np.array([d["bbox"] for d in dets])
        scores = np.array([d["conf"] for d in dets])
        idx = cv2.dnn.NMSBoxes(
            boxes.tolist(), scores.tolist(), score_threshold=conf_thr, nms_threshold=0.45
        )
        idx = np.asarray(idx).ravel()
        dets = [dets[i] for i in idx]
    return dets


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir", type=Path)
    ap.add_argument("npz", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--model", type=Path, default=Path("yolo11n.onnx"))
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--rotate", type=int, default=0, help="90: rotate input CW (portrait) before detection")
    args = ap.parse_args()

    data = np.load(args.npz)
    frame_names = [str(n) for n in data["frame_names"]]
    sess = ort.InferenceSession(str(args.model), providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name

    rows = []
    n = len(frame_names)
    for i in range(0, n, args.step):
        name = frame_names[i]
        img = cv2.imread(str(args.frames_dir / name))
        if img is None:
            continue
        detect_img = img
        if args.rotate == 90:
            detect_img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        canvas, s, pad = letterbox(detect_img)
        blob = (canvas[:, :, ::-1].astype(np.float32) / 255.0)[None, ...].transpose(0, 3, 1, 2)
        out = sess.run(None, {in_name: blob})[0]
        dets = postprocess(out, s, pad, (detect_img.shape[0], detect_img.shape[1]), args.conf)
        if args.rotate == 90:
            # rotated (u_rot, v_rot) -> original (u, v): u=v_rot, v=H-1-u_rot
            H = img.shape[0]
            for d in dets:
                x1, y1, x2, y2 = d["bbox"]
                d["bbox"] = [float(y1), float(H - 1 - x2), float(y2), float(H - 1 - x1)]
        for d in dets:
            rows.append({"frame": name, **d})
        if (i // args.step) % 20 == 0:
            print(f"frame {i}/{n}: {len(dets)} dets")

    with args.out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} detections -> {args.out}")


if __name__ == "__main__":
    main()
