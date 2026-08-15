# SpatialMem 复现工程

目标：按论文《SpatialMem: Unified 3D Memory with Metric Anchoring and Fast
Retrieval》（arXiv:2601.14895v2）复现"物体 + 属性 + 关系"的层级空间记忆，
并用第一视角视频数据验证"找东西"能力。视频只需普通手机摄像头即可采集。

论文要点：不需要稠密场景重建；把 3D 几何当作"可解释索引支架"，在其上构建
有根树记忆（房间 → 结构性锚点 → 物体 → 双层描述），支持度量关系查询
（距离/方向/可见性）与长期物体检索。

## 仓库结构（完整链路）

本仓库自包含复现所需的三块代码：

```text
spatialmem-repro/
├── android-app/          # APP 端：手机摄像头视频采集与落盘（可选，也可用任意手机录像）
│   ├── app/              #   手机端：采集/解码/落盘（video + timeline）
│   ├── link/             #   纯 JVM 链路协议（编解码/状态机/时钟对齐）
│   └── scripts/          #   PC 端录制/回放工具
├── src/spatialmem/       # 核心算法：记忆树/关系/查询/几何/管线
├── scripts/              # 复现脚本（COLMAP/深度/锚点/物体提升/评测）
├── docs/                 # 方案与复现计划
└── tests/                # 核心算法单测
```

数据流：**手机摄像头录像 → 会话目录（video + timeline）→ 核心算法
（src/spatialmem：COLMAP/深度/锚点/物体提升）**。采集端可以是任意 Android 手机
（`android-app/`，可选），也可以直接用手机相机录像后由
`scripts/prepare_session.py` 抽帧。另有一条结构化观察支路：把视频帧发给
观察服务（`server/`，`/v1/observe`）→ VLM 结构化 JSON → 空间记忆入库。

## 目录

```text
spatialmem-repro/
├── README.md
├── requirements.txt
├── docs/
│   └── 空间物体关系记忆-结论与SpatialMem复现计划.md
├── android-app/          # 手机摄像头视频采集（独立 Android 工程，见其 README）
├── src/spatialmem/
│   ├── memory.py      # 记忆树：节点/关系/双层描述/更新
│   ├── relations.py   # 关系谓词：on/above/below/near/contains/视角方向
│   ├── query.py       # locate / 关系链查询 / 视角化输出
│   ├── geometry.py    # 3D 框与位姿工具
│   └── pipeline.py    # 感知管线接口（位姿/深度/锚点/物体提升，可插拔）
├── scripts/
│   └── prepare_session.py  # 从录制会话提取帧 + 时间线清单
└── tests/
    └── test_core.py   # 核心逻辑冒烟测试（合成数据）
```

## 快速开始

```bash
cd spatialmem-repro
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests -q          # 核心逻辑测试
.venv/bin/python scripts/prepare_session.py \
    ../recordings/session_20260803_125024_indoor_walk \
    data/indoor_walk --fps 3
```

链路代码的构建/启动方式见各子目录 README：

- 手机摄像头采集：`android-app/README.md`
- 结构化观察服务：`server/README.md`

## 里程碑

- M0（当前）：记忆树/谓词/查询核心 + 数据准备 + 冒烟测试
- M1：几何层——从录制视频得到位姿与度量点云（选定后端）
- M2：锚点（墙/门/窗）+ 物体 3D 框提升与实例关联
- M3（已完成）：描述层——属性/关系文本与双层合并（`scripts/describe_memory.py`）
- M4（已完成）：评测——LongSpace 风格 QA + 真实录制回放（`scripts/run_eval_qa.py`，
  报告 `data/cup_walk/qa_report.json`）

## 已知差距

- 录制会话 pose.jsonl/imu.jsonl 为空：需开启 App 位姿采集或离线估计
- 深度使用 MiDaS（相对逆深度），需高度先验恢复尺度
- 描述层 VLM 未定（端侧轻量 VLM vs 云端 API）

## License

MIT License，Copyright (c) 2026 buffer0verflooow。详见 [LICENSE](LICENSE)。
