# linksee-server API 文档 v0.1

> 状态：草案。W3 定稿后发固件团队，W6 真机联调。
> 改动必须同步 `app/transport/wire.py` 与固件版本约定。

## 目录

- [1. 快速开始](#1-快速开始)
- [2. 鉴权](#2-鉴权)
- [3. WebSocket API（主通道）](#3-websocket-api主通道)
  - [3.1 连接](#31-连接)
  - [3.2 上行：推帧](#32-上行推帧)
  - [3.3 上行：心跳](#33-上行心跳)
  - [3.4 下行：结果消息](#34-下行结果消息)
  - [3.5 下行：协议错误](#35-下行协议错误)
  - [3.6 阅读模式](#36-阅读模式-triggerread)
  - [3.7 背压语义](#37-背压语义)
- [4. HTTP API（备用通道）](#4-http-api备用通道)
  - [4.1 推帧](#41-推帧)
- [5. 服务端管理接口](#5-服务端管理接口)
  - [5.1 健康检查](#51-健康检查)
  - [5.2 Prometheus 指标](#52-prometheus-指标)
  - [5.3 Swagger 文档](#53-swagger-文档)
  - [5.4 在线设备列表](#54-在线设备列表)
  - [5.5 知识库热切换](#55-知识库热切换)
  - [5.6 配置热更新](#56-配置热更新)
- [6. 消息类型速查](#6-消息类型速查)
- [7. 限流策略](#7-限流策略)
- [8. 错误码参考](#8-错误码参考)
- [9. 图像预处理约定](#9-图像预处理约定)
- [10. 联调检查清单](#10-联调检查清单)

---

## 1. 快速开始

### 连接信息

| 项目 | 值 |
|---|---|
| WebSocket 地址 | `ws://<host>/ws/glass/{device_id}?token=<token>` |
| HTTP 地址 | `POST http://<host>/v1/frame` |
| 鉴权方式 | HMAC-SHA256（见 §2） |
| 图像编码 | JPEG base64，不带 `data:` 前缀 |

### 最小可用示例

```bash
# 计算 token
DEVICE_ID="glass-001"
SECRET="dev-secret-change-me"
TOKEN=$(echo -n "$DEVICE_ID" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')

# WebSocket 连接
websocat "ws://localhost:8000/ws/glass/$DEVICE_ID?token=$TOKEN"

# 发送一帧（需要 base64 编码的 JPEG）
echo '{"type":"frame","seq":1,"trigger":"auto","image":"<base64>"}'
```

---

## 2. 鉴权

**算法**：`token = HMAC-SHA256(key=共享密钥, msg=device_id)` 的小写十六进制串。

| 通道 | 传递方式 |
|---|---|
| WebSocket | 查询参数 `?token=<token>` |
| HTTP | 请求头 `X-Device-Token: <token>` |

- 共享密钥由后端统一下发，**不要硬编码进固件镜像**。
- 鉴权失败：WebSocket 以 close code `4401` 断开，HTTP 返回 `401`。

**生成示例（Python）**：

```python
import hashlib, hmac
token = hmac.new(secret.encode(), device_id.encode(), hashlib.sha256).hexdigest()
```

---

## 3. WebSocket API（主通道）

**推荐所有正常场景走这条。** 断连后固件应自动重连，建议退避间隔 3 秒。重连视为新会话，服务端重置该设备的帧比较基准。

### 3.1 连接

```
ws://<host>/ws/glass/{device_id}?token=<token>
```

| 参数 | 位置 | 必填 | 说明 |
|---|---|---|---|
| `device_id` | path | 是 | 设备唯一标识 |
| `token` | query | 是 | HMAC-SHA256 签名（见 §2） |

### 3.2 上行：推帧

```json
{
  "type": "frame",
  "seq": 12,
  "ts": 1785000000.123,
  "trigger": "auto",
  "image": "<JPEG 的 base64，不带 data URI 前缀>"
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `type` | string | 是 | — | 固定 `"frame"` |
| `seq` | int | 否 | `0` | 帧序号，服务端原样回显，用于固件对齐请求与响应 |
| `ts` | float | 否 | — | 设备侧 Unix 时间戳（秒），仅用于排查 |
| `trigger` | string | 否 | `"auto"` | `"auto"` 自动推帧 / `"manual"` 用户主动按键 / `"read"` 阅读模式 |
| `image` | string | 是 | — | base64 编码的 JPEG，不带 `data:` 前缀；≤ 6MB（base64 后） |

**`trigger` 取值含义**：

| 值 | 行为 |
|---|---|
| `auto` | 正常场景识别。受全部闸门约束（去重、场景门控、限流）。 |
| `manual` | 用户按键触发。**跳过去重和场景门控**，仅受限流（1 次/秒）。 |
| `read` | 阅读模式。跳过去重和场景门控，走独立限流桶（6 次/分）。详见 [§3.6](#36-阅读模式-triggerread)。 |

### 3.3 上行：心跳

```json
{"type": "ping"}
```

服务端回复 `{"type": "pong"}` 并刷新在线状态（TTL 30 秒）。建议每 10-15 秒发一次。

### 3.4 下行：结果消息

#### 通用结构

```json
{
  "type": "<消息类型>",
  "content": "<文案内容>",
  "seq": 12,
  "latency_ms": 1340,
  "index": 1,
  "total": 1,
  "end": true
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 消息类型，决定眼镜端行为（见下表） |
| `content` | string | 文案内容，≤ 30 字符（中文按字符计） |
| `seq` | int | 帧序号，原样回显 |
| `latency_ms` | int | 服务端处理耗时，不含网络往返 |
| `index` | int | 当前分片序号，从 1 开始（仅阅读模式有意义） |
| `total` | int | 总分片数（仅阅读模式有意义） |
| `end` | bool | 是否最后一片（仅阅读模式有意义） |

#### 六种消息类型

| `type` | 眼镜端行为 | 触发条件 |
|---|---|---|
| `alert` | **震动 + 语音播报 + 显示** | 危险场景：闯红灯、车辆逼近、坠落风险 |
| `voice` | **语音播报 + 显示** | 需注意：台阶、湿滑、施工围挡 |
| `text` | **仅显示** | 有信息但无安全风险：店招、路牌 |
| `noop` | **保持上一结果不变，不清屏** | 本帧被闸门驳回（同场景/限流） |
| `read` | **入队顺序播报** | 阅读模式分片，见 [§3.6](#36-阅读模式-triggerread) |
| `error` | 显示错误提示 | 协议错误、上游异常 |

> ⚠️ **`noop` 不清屏**是最容易被固件做错的一条。收到 `noop` 时保持当前显示不变，不要刷成空白。

#### 消息示例

```json
// alert — 危险告警
{"type":"alert","content":"红灯，请等待","seq":42,"latency_ms":1340,"index":1,"total":1,"end":true}

// voice — 安全提醒
{"type":"voice","content":"前方台阶，注意脚下","seq":43,"latency_ms":1280,"index":1,"total":1,"end":true}

// text — 信息展示
{"type":"text","content":"前方 50 米便利店","seq":44,"latency_ms":1410,"index":1,"total":1,"end":true}

// noop — 闸门驳回，不清屏
{"type":"noop","content":"","seq":45,"latency_ms":5,"index":1,"total":1,"end":true}

// error — 协议错误
{"type":"error","content":"bad_frame: base64 解码失败","seq":0,"latency_ms":0,"index":1,"total":1,"end":true}
```

### 3.5 下行：协议错误

```json
{"type":"error","content":"bad_frame: base64 解码失败: ...","seq":0,"latency_ms":0,"index":1,"total":1,"end":true}
```

收到 `error` 后**连接不会断开**，固件可以继续发送后续帧。不要因为收到一条 error 就触发重连。

### 3.6 阅读模式（`trigger: "read"`）

用户对着菜单、说明书、告示牌主动触发。服务端把画面文字逐字读出，切成 ≤ 30 字的分片连续下发。

#### 上行

```json
{"type":"frame","seq":7,"trigger":"read","image":"<base64>"}
```

#### 下行：连续 N 条分片

```json
{"type":"read","content":"川菜馆菜单，凉菜类：","seq":7,"index":1,"total":8,"end":false,"latency_ms":2150}
{"type":"read","content":"口水鸡 38 元，夫妻肺片 42 元","seq":7,"index":2,"total":8,"end":false,"latency_ms":2150}
{"type":"read","content":"热菜类：水煮鱼 68 元，回锅肉","seq":7,"index":3,"total":8,"end":false,"latency_ms":2150}
...
{"type":"read","content":"完","seq":7,"index":8,"total":8,"end":true,"latency_ms":2150}
```

#### 固件实现要点

| # | 要点 |
|---|---|
| 1 | 收到 `end: false` → 继续等下一条；收到 `end: true` → 本次阅读结束 |
| 2 | 分片保证按序下发（同一条 WS 连接、同一个发送循环），固件按到达顺序入队即可 |
| 3 | `index` 从 1 开始，`total` 所有分片一致。可以用来显示「3/8」这类进度 |
| 4 | **用户中途中断（转头、按键、手势）→ 固件清空本地播报队列即可，不要回报服务端** |
| 5 | 画面中无文字 → 返回单条 `{"type":"text","content":"未发现文字"}`，**不是** `read` 类型 |
| 6 | 限流超限 → 返回 `{"type":"text","content":"阅读太频繁，请稍后再试"}`，**不是** `noop` |
| 7 | **`seq` 是帧序号，不是分片序号**——同一次阅读的所有分片 `seq` 相同。分片位置看 `index` |

#### 与实时帧的差异

| | 实时帧 (`auto`) | 阅读模式 (`read`) |
|---|---|---|
| 模型 | `qwen-vl-plus` | `qwen-vl-ocr` |
| 超时 | 10s | 15s |
| 限流 | 1 次/秒 | 6 次/分（独立令牌桶，与实时帧互不挤占） |
| 去重 / 场景门控 | 生效 | 跳过 |
| 人脸驳回 | 生效 | 跳过 |
| 脱敏 | 生效 | **照常生效**（身份证/银行卡/手机号 → `***`） |
| 回传 | 单条 ≤ 30 字 | 连续 N 条分片 |

### 3.7 背压语义

服务端每台设备只保留**最新 1 帧**待处理。如果在上一帧处理完之前推了 3 帧，前 2 帧会被静默丢弃，**不会**收到它们的响应。

**固件不能假设「发 N 帧必收 N 条响应」。** 用 `seq` 对齐，不要用计数或顺序假设。

---

## 4. HTTP API（备用通道）

仅在 WebSocket 不可用时使用（老设备、长连接被网络中间设备切断）。

### 4.1 推帧

```
POST /v1/frame
Content-Type: application/json
X-Device-Token: <token>
```

**请求体**：

```json
{
  "device_id": "glass-001",
  "seq": 12,
  "trigger": "auto",
  "image": "<JPEG 的 base64，不带 data URI 前缀>"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `device_id` | string | 是 | 设备唯一标识 |
| `seq` | int | 否 | 帧序号，原样回显（默认 `0`） |
| `trigger` | string | 否 | `"auto"`（默认）/ `"manual"` / `"read"` |
| `image` | string | 是 | base64 编码的 JPEG，≤ 6MB |

**响应体（实时帧）**：与 WebSocket 下行格式一致。

```json
{
  "type": "alert",
  "content": "红灯，请等待",
  "seq": 12,
  "latency_ms": 1340,
  "index": 1,
  "total": 1,
  "end": true,
  "segments": null
}
```

**响应体（阅读模式）**：HTTP 是单次请求-响应，没有连续下发语义。阅读模式的分片通过 `segments` 数组一次返回：

```json
{
  "type": "read",
  "content": "川菜馆菜单，凉菜类：",
  "seq": 12,
  "latency_ms": 2150,
  "index": 1,
  "total": 8,
  "end": false,
  "segments": [
    "川菜馆菜单，凉菜类：",
    "口水鸡 38 元，夫妻肺片 42 元",
    "热菜类：水煮鱼 68 元，回锅肉 38 元",
    "家常豆腐 28 元，干煸四季豆 22 元",
    "主食类：米饭 2 元，蛋炒饭 12 元",
    "汤品类：紫菜蛋花汤 8 元",
    "饮料类：可乐 5 元，雪碧 5 元",
    "完"
  ]
}
```

- `content` / `index` / `end` 指向**第一片**，仅用于兼容只认单条响应的老实现。
- HTTP 调用方应直接使用 `segments` 数组。
- 非阅读模式下 `segments` 为 `null`。

**状态码**：

| 状态码 | 含义 |
|---|---|
| `200` | 正常（含 `type: "noop"`，也是 200） |
| `400` | base64 解码失败 |
| `401` | 鉴权失败 |
| `422` | 请求体字段不合法（`image` 为空或超长） |

---

## 5. 服务端管理接口

### 5.1 健康检查

```
GET /healthz
```

存活探针，不检查任何外部依赖。用于负载均衡器判断进程是否存活。

**响应**：

```json
{"status": "ok"}
```

---

```
GET /readyz
```

就绪探针，检查 KV 存储连通性。KV 挂了就返回 503——闸门依赖 KV 做限流和去重。

**正常响应（200）**：

```json
{"status": "ok", "kv": true, "kb_ready": false}
```

**降级响应（503）**：

```json
{"status": "degraded", "kv": false, "kb_ready": false}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | `"ok"` 或 `"degraded"` |
| `kv` | bool | KV 存储是否连通 |
| `kb_ready` | bool | 知识库是否已加载 |

### 5.2 Prometheus 指标

```
GET /metrics
```

返回 Prometheus 格式的指标数据。主要指标：

| 指标名 | 类型 | 说明 |
|---|---|---|
| `frames_total` | Counter | 帧处理总数，按 `outcome` 分（`replied`/`noop`/`fallback`/`error`） |
| `e2e_latency_seconds` | Histogram | 端到端延迟分布 |
| `devices_online` | Gauge | 当前在线设备数 |
| `second_call_total` | Counter | 二次复核调用次数（目标占比 <5%） |
| `model_latency_seconds` | Histogram | 模型调用耗时 |

### 5.3 Swagger 文档

```
GET /docs        # Swagger UI（交互式）
GET /redoc       # ReDoc（只读）
```

由 FastAPI 自动生成，可直接在浏览器中调试 HTTP 接口。

### 5.4 在线设备列表

```
GET /admin/devices
```

**响应**：

```json
{
  "online": 3,
  "device_ids": ["glass-001", "glass-003", "glass-007"]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `online` | int | 当前在线设备数 |
| `device_ids` | string[] | 在线设备 ID 列表（按字母排序） |

### 5.5 知识库热切换

```
POST /admin/kb/reload
Content-Type: application/json

{
  "persist_dir": "/app/data/kb/20260807-120000"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `persist_dir` | string | 否 | 新版本持久化目录路径。不传则重新加载当前目录。 |

**响应**：

```json
{
  "persist_dir": "/app/data/kb/20260807-120000",
  "chunks": 1250,
  "ready": true
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `persist_dir` | string | 已加载的目录路径 |
| `chunks` | int | 知识库 chunk 数量 |
| `ready` | bool | 是否加载成功 |

> 知识库入库由离线脚本 `scripts/kb_ingest.py` 完成，这个接口只做版本切换。

### 5.6 配置热更新

```
POST /admin/config/reload
```

重读 `.env` 文件。**注意**：已注入到节点闭包里的闸门/规则阈值不会变——阈值调参请直接重启进程。

**响应**：

```json
{
  "env": "prod",
  "gate_rate_limit_per_sec": 1.0,
  "gate_phash_dup_distance": 8,
  "gate_min_interval_s": 3.0,
  "note": "闸门/规则阈值需重启进程生效"
}
```

---

## 6. 消息类型速查

### 上行（眼镜端 → 服务端）

| type | 说明 | 必填字段 |
|---|---|---|
| `frame` | 推帧 | `image` |

### 下行（服务端 → 眼镜端）

| type | content | 眼镜端行为 |
|---|---|---|
| `alert` | ≤ 30 字警告 | 震动 + 语音 + 显示 |
| `voice` | ≤ 30 字提醒 | 语音 + 显示 |
| `text` | ≤ 30 字信息 | 仅显示 |
| `noop` | 空 | **不清屏**，保持上一结果 |
| `read` | ≤ 30 字分片 | 入队顺序播报 |
| `error` | 错误描述 | 显示错误提示 |
| `pong` | — | 心跳响应 |

---

## 7. 限流策略

| 限流项 | 实时帧 | 阅读模式 |
|---|---|---|
| 算法 | 令牌桶 | 令牌桶（独立） |
| 速率 | 1 次/秒 | 6 次/分 |
| 突发容量 | 1 帧 | 3 帧 |
| 超限行为 | 丢弃帧，不回传 | 返回文本提示 |
| 去重 | phash 汉明距离 < 8 | 跳过 |
| 场景门控 | 距上次成功 ≥ 3s | 跳过 |

> 限流是本项目成本的**主控开关**。不要绕过它——提高阈值前先评估对月成本的影响。

---

## 8. 错误码参考

### WebSocket Close Code

| Code | 含义 |
|---|---|
| `4401` | 鉴权失败（token 错误或过期） |

### HTTP 状态码

| 状态码 | 含义 | 响应体 |
|---|---|---|
| `200` | 成功（含 `noop`） | 标准 ReplyMessage |
| `400` | base64 解码失败 | `{"detail": "..."}` |
| `401` | 鉴权失败 | `{"detail": "unauthorized"}` |
| `422` | 请求体字段不合法 | `{"detail": [{"loc": [...], "msg": "..."}]}` |
| `500` | 服务端内部错误 | `{"detail": "..."}` |
| `503` | 服务未就绪（KV 不可用） | readyz 响应 |

### 下行 error 消息

| content 模式 | 含义 |
|---|---|
| `bad_frame: ...` | 协议解析失败（base64 解码/字段校验） |
| `识别失败，请稍后重试` | 模型调用超时或异常 |

---

## 9. 图像预处理约定

| 参数 | 要求 | 原因 |
|---|---|---|
| 分辨率 | 长边 ≤ 1024px | 线性影响推理延迟和 token 成本 |
| 格式 | JPEG | 兼容性最好 |
| 质量 | q ≈ 75 | 识别准确率和体积的平衡点 |
| 编码 | base64，不带 `data:` 前缀 | 协议统一 |

- **固件能做到就在固件侧做**，减少网络传输量。
- 固件做不到服务端会补做，但会白付一次解码+编码（20-40ms）。

---

## 10. 联调检查清单

### 基础功能

- [ ] 鉴权：正确 token 能连，错误 token 收到 4401
- [ ] 心跳：ping/pong 正常，断网后固件能在 3 秒内重连
- [ ] 正常帧：5 种 `type`（alert/voice/text/noop/error）固件都能正确呈现
- [ ] **`noop` 不清屏**（最容易做错的一条）
- [ ] `seq` 回显对齐正确

### 阅读模式

- [ ] **阅读模式：连续分片按序播报完整，中途不丢片、不重排**
- [ ] **阅读模式：播报到一半中断（转头/按键），固件清空本地队列且不回报服务端**
- [ ] 阅读模式：所有分片的 `seq` 相同，固件没把它们当成多个不同的帧
- [ ] 阅读模式：无文字画面收到 `"未发现文字"` 而不是静默
- [ ] 阅读模式：连按超限收到明确提示文案（`"阅读太频繁，请稍后再试"`），不是 `noop`

### 鲁棒性

- [ ] 背压：快速连推 10 帧，固件不因收不到全部响应而卡死或重连
- [ ] 坏帧：收到 `error` 后连接保持，可继续推帧
- [ ] 图像规格：固件上传的图长边 ≤ 1024、q ≈ 75
- [ ] 中文文案在小屏上不截断、不乱码
- [ ] `alert` 的震动+播报时序符合预期
- [ ] HTTP 备用通道：老设备能正常收发
