#!/usr/bin/env python3
"""P1-c 门/窗锚点关系评测 CLI。

读入 GT 与预测锚点（/v1/observe 的 `anchors` 数组），按
type + direction + 距离容差匹配，输出每种锚点的 grounding F1（对标论文
Scene 1 的门/窗 0.82、墙 0.88）以及宏观关系得分。

Usage:
    python scripts/eval_anchor_relations.py \
        --gt data/classroom/anchor_gt.json \
        --pred /private/tmp/observe_resp.json \
        --out /tmp/anchor_relation_report.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from spatialmem.anchor_eval import (
    ANCHOR_TYPES,
    PAPER_BASELINE,
    Anchor,
    evaluate_anchors,
    relation_support,
)


def _anchors(obj: object) -> list[Anchor]:
    if isinstance(obj, dict):
        raw = obj.get("anchors", obj)
    else:
        raw = obj
    return [Anchor.from_dict(a) for a in raw]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--distance-tol-m", type=float, default=2.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    gt_raw = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    pred_raw = json.loads(Path(args.pred).read_text(encoding="utf-8"))
    gt = _anchors(gt_raw)
    pred = _anchors(pred_raw)

    report = evaluate_anchors(pred, gt, distance_tol_m=args.distance_tol_m)
    if isinstance(gt_raw, dict) and isinstance(gt_raw.get("relations"), list):
        report["relation_support"] = relation_support(
            pred, gt, gt_raw["relations"], distance_tol_m=args.distance_tol_m
        )

    print(f"anchor relation score = {report['relation_score']:.3f}")
    for t in ANCHOR_TYPES:
        p = report["per_type"][t]
        if p["tp"] + p["fp"] + p["fn"] == 0:
            print(f"  {t:8s}: (未参与)  paper baseline {PAPER_BASELINE[t]:.2f}")
            continue
        print(
            f"  {t:8s}: f1={p['f1']:.3f} "
            f"(p={p['precision']:.3f} r={p['recall']:.3f} tp={p['tp']} fp={p['fp']} fn={p['fn']}) "
            f"paper baseline {PAPER_BASELINE[t]:.2f}"
        )
    if "relation_support" in report:
        print(f"relation support = {report['relation_support']:.3f}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"report written to {args.out}")


if __name__ == "__main__":
    main()
