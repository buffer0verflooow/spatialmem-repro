# spatialmem-repro/server — 统一服务端

本目录是**唯一服务端代码维护点**（2026-08-15 起）。由原 `linksee-server`
仓库整体迁入（原仓库保留为历史快照，不再作为维护源），同时承载两条链路：

1. **智能眼镜全链路**：WS/HTTP 接入、帧准入闸门、Qwen-VL 推理、阅读模式 OCR、
   RAG 旁路、规则/脱敏、存储、Agent 交互、可观测性；
2. **空间记忆结构化观察**：`POST /v1/observe`，客户端上传当前帧 → VLM 输出
   `{name, color, location, attributes, confidence, support}` 供空间记忆入库。

架构决策、延迟预算、成本模型与开发约定见 **[CLAUDE.md](CLAUDE.md)**——那是
这个服务端唯一权威文档，改架构先改它。

## 快速开始

```bash
make install          # 创建 .venv 并安装依赖
cp .env.example .env
make test             # 274 个测试，无需任何外部服务
make run              # 起服务，http://127.0.0.1:8000/docs
```

默认配置是 **mock 推理后端 + 内存 KV + 不落库**，不需要 DashScope key、
Redis 或 MySQL 就能跑通全链路（含 `/v1/observe`）。

另开一个终端验证端到端：

```bash
python scripts/fake_glasses.py --frames 12 --fps 2 --scene-change-every 3
```

预期输出（`·` 是被闸门驳回的帧）：

```
     seq=  0 text      24ms  前方右转是出口
   · seq=  1 noop       6ms
   · seq=  2 noop       3ms
     seq=  4 text      24ms  前方右转是出口
...
闸门驳回率: 66.7%
实际模型调用: 4 次 / 12 帧
```

## 与空间记忆链路（客户端）联调

Android 端 `BuildConfig.CLOUD_OBSERVE_URL` 指向本服务（如
`http://<PC-IP>:8000`），视觉问答/持续观察路径会调用 `/v1/observe`。

`POST /v1/observe`，body：

```json
{"frame": "<jpeg-base64>", "hint": "continuous | 用户问题 | 空串"}
```

响应：

```json
{
  "name": "笔记本电脑",
  "color": "黑色",
  "location": "桌上",
  "attributes": "ThinkPad",
  "confidence": 0.92,
  "support": {"name": "桌子", "color": "棕色", "location": "客厅", "attributes": "木质,圆形"}
}
```

`support` 是承载物体的支撑物实体，客户端会把它 upsert 成独立记忆节点。
`ENV != dev` 时该接口返回 503（生产接入真云端时再开设备鉴权）。

## 切到真实依赖

在 `.env` 里逐项打开，各自独立、可单独启用：

| 想启用 | 改什么 | 额外准备 |
|---|---|---|
| 真实模型 | `INFERENCE_BACKEND=dashscope` + `DASHSCOPE_API_KEY=...` | — |
| Redis | `KV_BACKEND=redis` | Redis 实例（多 worker 部署必须） |
| MySQL | `DB_BACKEND=mysql` + `MYSQL_DSN=...` | 库已建好，表会自动创建 |
| RAG | `KB_BACKEND=chroma` | `make install-kb`，再跑 `kb_ingest.py` |
| 人脸检测 | `FACE_DETECT_ENABLED=true` | `pip install -e ".[face]"` |

## 项目结构

```
app/
├── main.py          FastAPI 入口、健康检查、/metrics
├── config.py        全部阈值，禁止在业务代码硬编码
├── runtime.py       依赖装配 + 旁路任务 + 单帧处理入口
├── transport/       WS / HTTP 接入、鉴权、协议
├── gate/            帧准入闸门（成本主控开关）+ 背压
├── rules/           前置/后置规则、人脸检测、脱敏
├── inference/       Qwen-VL 客户端、提示词、结构化解析
├── shaping/         模板压缩、类型标注、兜底
├── kb/              Chroma 只读检索 + 上下文预取旁路
├── graph/           FrameState + LangGraph 线性管线装配
├── storage/         KV(内存/Redis) + MySQL 5 表
├── observe/         结构化观察（/v1/observe，空间记忆专用）
└── observability/   structlog + Prometheus

scripts/
├── fake_glasses.py    假眼镜推流（协议模拟器）
├── bench_latency.py   延迟压测，输出逐节点耗时分解
├── bench_cost.py      token 实测 + 月成本推算
└── kb_ingest.py       知识库离线入库
```

## 常用命令

```bash
make test                    # 全部测试（274 个）
make test-unit               # 只跑单测
make lint                    # ruff
make bench                   # 20 台并发压测
make bench-mock-realistic    # 带 1200ms 模拟模型延迟的压测
```

## 三条必须知道的设计约束

1. **不处理每一帧。** 2 帧/秒全量处理 vs 场景变化触发，模型调用量差 20 倍。
   `app/gate/` 是关键，改阈值前先读 CLAUDE.md §5.1。
2. **单请求 1 次模型调用。** 模型占端到端延迟 88%（实测），非模型环节的优化
   都在噪声级别。
3. **RAG 用上一帧的关键词预取。** 靠帧间场景连续性把检索移出延迟预算
   （CLAUDE.md §5.2）。

## 迁移记录

- 2026-08-15：由 `linksee-server` 仓库整体迁入 `spatialmem-repro/server`，
  包名改为 `spatialmem-server`，接口与配置保持兼容；274 个测试全部通过。
- 原 `linksee-server` 保留为历史快照，不再更新。
