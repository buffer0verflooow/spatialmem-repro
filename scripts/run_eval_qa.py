#!/usr/bin/env python3
"""M4 评测：LongSpace 风格 QA + 真实录制回放，量化找物/关系/描述可用性。

评测集见 data/cup_walk/qa_ground_truth.json；记忆由 M2 产物（anchors/supports/
instances/点云）重新装配，描述层（M3）在其上生成，不依赖任何训练数据。

指标：
  - locate@1：中文问句 → 记忆返回的 top-1 节点是否落在 GT 的实例集合内
  - 桌面组成召回：GT 实例是否都能经「on 桌面高度支撑面」找到
  - 多余桌面类别：桌面物体里混入了 GT 之外的类别（误检暴露）
  - 描述覆盖率：桌面物体的 Layer2 稳定描述是否生成
  - 自我中心回答：locate + 视角化输出的方位/距离一致性（几何精度已由 M1/M2 单独验证）

Usage:
    python scripts/run_eval_qa.py --data-dir data/cup_walk
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from spatialmem.builder import build_memory_from_artifacts
from spatialmem.descriptions import zh_direction, zh_label
from spatialmem.query import locate, to_egocentric
from spatialmem.relations import egocentric_direction


def load_gt(data_dir: Path) -> dict:
    return json.loads((data_dir / "qa_ground_truth.json").read_text(encoding="utf-8"))


def build_memory(data_dir: Path):
    def read_jsonl(name: str) -> list[dict]:
        p = data_dir / name
        return [json.loads(line) for line in open(p)] if p.exists() else []

    npz = np.load(data_dir / "metric_cloud.npz")
    points = npz["points_metric"]
    poses = npz["poses_metric"]
    return build_memory_from_artifacts(
        anchors=read_jsonl("anchors.jsonl"),
        supports=read_jsonl("supports.jsonl"),
        instances=read_jsonl("instances_focus.jsonl"),
        points_metric=points,
    ), poses


def desk_supports(mem, gt: dict) -> list:
    lo, hi = gt["desk_support_top_z"]
    out = []
    for n in mem.nodes():
        if n.node_type == "anchor" and n.category == "support_surface" and n.box:
            if lo <= n.box[5] <= hi:
                out.append(n)
    return out


def desk_objects(mem, supports: list) -> list:
    """通过 on 关系挂在桌面高度支撑面上的物体。"""
    support_ids = {s.node_id for s in supports}
    return [
        n
        for n in mem.nodes()
        if n.node_type == "object"
        and any(
            r.status == "active" and r.predicate == "on" and r.object in support_ids
            for r in mem.relations_of(n.node_id)
        )
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/cup_walk")
    ap.add_argument("--descriptions", default=None,
                    help="描述文件路径（默认 data_dir/memory_descriptions.json；"
                         "VLM 版传 memory_descriptions_vlm.json）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    desc_path = Path(args.descriptions) if args.descriptions else data_dir / "memory_descriptions.json"
    gt = load_gt(data_dir)
    mem, poses = build_memory(data_dir)
    final_pose = poses[-1]
    supports = desk_supports(mem, gt)
    desk = desk_objects(mem, supports)

    report: dict = {
        "scene": data_dir.name,
        "memory": {"nodes": len(mem.nodes()), "relations": len(mem.active_relations())},
        "desk_supports": [s.node_id for s in supports],
        "desk_objects": [n.node_id for n in sorted(desk, key=lambda n: n.node_id)],
    }

    # ---- 1. locate@1 ----
    locate_rows = []
    for q in gt["locate_queries"]:
        hits = locate(mem, q["q"])
        top = hits[0] if hits else None
        # expect 为空：正确行为是查不到（如「杯子」不应返回误检节点）；
        # expect 非空：top-1 必须在集合内。
        ok = top is None if not q["expect"] else (top is not None and top.node_id in q["expect"])
        locate_rows.append(
            {
                "query": q["q"],
                "expect": q["expect"],
                "top1": top.node_id if top else None,
                "top1_class": top.category if top else None,
                "pass": ok,
            }
        )
    report["locate"] = {
        "rows": locate_rows,
        "accuracy": sum(1 for r in locate_rows if r["pass"]) / max(1, len(locate_rows)),
    }

    # ---- 2. 桌面组成召回（实例级）+ 多余类别 ----
    desk_by_id = {n.node_id: n for n in desk}
    desk_rows = []
    for node_id in gt["desk_objects"]:
        ok = node_id in desk_by_id
        desk_rows.append({"instance": node_id, "found": ok})
    report["desk_recall"] = {
        "rows": desk_rows,
        "recall": sum(1 for r in desk_rows if r["found"]) / max(1, len(desk_rows)),
    }
    gt_classes = {mem.get_node(i).category for i in gt["desk_objects"] if mem.get_node(i)}
    spurious = sorted({n.category for n in desk} - gt_classes)
    report["desk_spurious"] = {
        "classes": [zh_label(c) for c in spurious],
        "answer": "桌面上有：" + "、".join(
            sorted(zh_label(n.category) for n in desk)
        ),
    }

    # ---- 3. 描述覆盖率（M3 Layer2 是否生成）----
    described = json.loads(desc_path.read_text(encoding="utf-8"))
    l2_by_id = {o["instance_id"]: o["layer2_text"] for o in described["objects"]}
    desk_ids = {n.node_id for n in desk}
    cov = [n.node_id for n in desk if l2_by_id.get(n.node_id)]
    report["description_coverage"] = {
        "covered": sorted(cov),
        "coverage": len(cov) / max(1, len(desk_ids)),
        "source": str(desc_path),
    }

    # ---- 3.5 VLM 命名核对（描述文件含 vlm_name/consensus_ok 时）----
    zh_to_en = {
        "笔记本电脑": "laptop", "笔记本": "laptop", "电脑": "laptop",
        "杯子": "cup", "马克杯": "cup", "杯": "cup",
        "鼠标": "mouse", "花筒": "flower_pot", "水桶": "bucket", "桶": "bucket",
        "床": "bed", "椅子": "chair", "办公椅": "chair", "风扇": "fan", "电风扇": "fan",
    }
    naming_rows = []
    for node_id, expect in gt.get("naming", {}).items():
        row = next((o for o in described["objects"] if o["instance_id"] == node_id), None)
        if row is None:
            naming_rows.append({"instance": node_id, "expect": expect, "pass": False, "detail": "missing"})
            continue
        vlm_name = row.get("vlm_name")
        consensus_ok = bool(row.get("consensus_ok"))
        en = zh_to_en.get((vlm_name or "").strip(), (vlm_name or "").lower())
        if expect in ("laptop", "cup", "mouse", "bed", "chair", "fan"):
            ok = consensus_ok and en == expect
        elif expect == "not_cup":
            ok = (not consensus_ok) or en != "cup"
        elif expect == "no_mouse":
            ok = (not consensus_ok) or en != "mouse"
        else:
            ok = False
        naming_rows.append(
            {
                "instance": node_id,
                "expect": expect,
                "vlm_name": vlm_name,
                "consensus_ok": consensus_ok,
                "pass": ok,
            }
        )
    report["vlm_naming"] = {
        "rows": naming_rows,
        "accuracy": sum(1 for r in naming_rows if r["pass"]) / max(1, len(naming_rows)),
    }

    # ---- 4. 自我中心回答（方位/距离一致性 + 回答文本）----
    ego_rows = []
    for node_id in gt["egocentric_instances"]:
        node = mem.get_node(node_id)
        if node is None:
            ego_rows.append({"instance": node_id, "pass": False, "reason": "node missing"})
            continue
        ans = to_egocentric(mem, node_id, final_pose)
        expect = egocentric_direction(node.position(), final_pose)
        dist_ok = abs(ans["distance"] - expect["distance"]) <= gt["distance_tolerance_m"]
        tag_ok = ans["tag"] == expect["tag"]
        answer = f"{zh_label(node.label)}在{zh_direction(ans['tag'])}约 {ans['distance']:.1f} 米"
        ego_rows.append(
            {
                "instance": node_id,
                "label_zh": zh_label(node.label),
                "answer": answer,
                "distance_m": round(ans["distance"], 2),
                "tag": ans["tag"],
                "consistency": dist_ok and tag_ok,
            }
        )
    report["egocentric"] = {
        "rows": ego_rows,
        "note": "方位/距离由同一几何源计算，此项验证查询→回答文本管线一致性；几何精度见 M1/M2",
        "consistency": sum(1 for r in ego_rows if r.get("consistency")) / max(1, len(ego_rows)),
    }

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"report -> {args.out}")

    print(f"\n== M4 QA 评测（{data_dir.name}）==")
    print(
        f"记忆: {report['memory']['nodes']} 节点 / {report['memory']['relations']} 关系"
    )
    print(f"桌面支撑面: {report['desk_supports']}")
    print(f"桌面物体: {report['desk_objects']}")
    print(f"locate@1: {report['locate']['accuracy']:.0%} "
          f"({sum(r['pass'] for r in report['locate']['rows'])}/{len(report['locate']['rows'])})")
    for r in report["locate"]["rows"]:
        mark = "✓" if r["pass"] else "✗"
        print(f"  {mark} {r['query']} -> top1={r['top1']} (期望 {r['expect']})")
    print(
        f"桌面组成召回: {report['desk_recall']['recall']:.0%} "
        f"({sum(r['found'] for r in report['desk_recall']['rows'])}/"
        f"{len(report['desk_recall']['rows'])})  answer: {report['desk_spurious']['answer']}"
    )
    if report["desk_spurious"]["classes"]:
        print(f"多余桌面类别: {report['desk_spurious']['classes']}")
    print(
        f"描述覆盖率: {report['description_coverage']['coverage']:.0%} "
        f"({len(report['description_coverage']['covered'])}/{len(desk)})"
    )
    if report.get("vlm_naming"):
        print(
            f"VLM 命名核对: {report['vlm_naming']['accuracy']:.0%} "
            f"({sum(r['pass'] for r in report['vlm_naming']['rows'])}/"
            f"{len(report['vlm_naming']['rows'])})"
        )
        for r in report["vlm_naming"]["rows"]:
            mark = "✓" if r["pass"] else "✗"
            print(
                f"  {mark} {r['instance']}: VLM={'/'.join(str(r[k]) for k in ('vlm_name', 'consensus_ok'))}"
                f" 期望={r['expect']}"
            )
    print(f"自我中心回答一致性: {report['egocentric']['consistency']:.0%}")
    for r in report["egocentric"]["rows"]:
        print(f"  {r.get('instance')}: {r.get('answer')}")


if __name__ == "__main__":
    main()
