# 结构化观察服务（独立精简版）

空间记忆复现链路的服务端：接收客户端（Android 手机）上传的当前帧，
用 VLM（默认 qwen-vl-max）输出结构化 JSON，供客户端空间记忆入库。

## 代码边界（重要）

本目录**只包含原创的 `/v1/observe` 结构化观察模块**：

- `app/observe/`：prompts / backend（mock|dashscope）/ router —— 原创代码；
- `app/config.py` / `app/runtime.py` / `app/main.py` / `app/observability/`：
  独立编写的最小入口，仅用于启动本观察服务；
- 测试：`tests/unit/test_observe.py`、`tests/integration/test_observe_endpoint.py`。

本目录只关注“视频帧 → 结构化观察”这一条路径，不包含通用后端服务的传输 /
网关 / 规则 / 存储等基础设施。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/pytest tests -q          # 6 个测试，零外部依赖（mock 后端）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

默认 `INFERENCE_BACKEND=mock`，无需 DashScope key 即可联调；
接真实 VLM 时设 `INFERENCE_BACKEND=dashscope` + `DASHSCOPE_API_KEY=...`。

## 协议

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

`support` 是承载物体的支撑物实体（桌子/床/窗台/地板），客户端会把它 upsert
成独立记忆节点，回答“在什么样的桌子上”时带出支撑物颜色/位置。

## 目录

```text
server/
├── app/observe/          # 原创：prompts / backend(mock|dashscope) / router
├── app/config.py         # 独立最小配置
├── app/runtime.py        # 独立运行时容器（仅装配 observe 后端）
├── app/main.py           # 独立 FastAPI 入口
└── tests/                # unit + integration
```
