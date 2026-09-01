# SpatialMem 论文复现与 AI 眼镜空间记忆开发交接（2026-08-31）

新会话请先完整阅读本文件，然后从「最新状态」继续；下方各节是继续工作所需的全部上下文。
详细设计/结论/路线图见 `docs/空间物体关系记忆-结论与SpatialMem复现计划.md`（§4.7–4.9）。

## 0. 一句话状态

AI 眼镜空间记忆的**服务端锚点识别（门/窗/墙）、客户端场景隔离、回访恢复与语音置顶、锚点多视角确认、事件驱动采集、完整谓词与多跳查询、锚点关系评测、导航航点、地图级 A* 路径规划、俯视小地图可视化与眼镜 POSE 通道供数**均已实现并通过测试；P0/P1 路线图中 P0-1a、P0-1b、P0-2、P0-3、P1-a、P1-b、P1-c、客户端集成已完成；位姿闭环第 1 档（眼镜 POSE 供数）、第 2 档第 1 阶段（跟随视角：实时朝向旋转小地图 + 相对转向播报）与**第 2 档第 2 阶段（眼镜 IMU 通道 + 步态检测 + 位置递推 + 到达判定）已完成**，剩余项（标定精度调优、相机 AR 叠加）见 §5。

## 1. 代码库结构

工作区根：`/Users/pwn/Desktop/研究生课程/001 导引课/development`

| 目录 | 内容 |
|---|---|
| `spatialmem-repro/` | **论文复现工程（Python）**：度量重建、稠密地图、路径规划、评测 |
| `spatialmem-repro/server/` | **统一服务端（FastAPI）**：智能眼镜全链路 + `/v1/observe` 空间记忆结构化观察（anchors 门/窗/墙） |
| `blindassist/` | **客户端主工程（Android/Kotlin）**：空间记忆 M5（候选池/确认/检索/场景隔离/导航）+ 位姿闭环（POSE/IMU 通道、步态检测、位置递推、到达判定） |
| `linksee-client-android/` | 客户端镜像工程（`spatialmem` 包与 blindassist 完全同步，UI 有差异） |
| `linksee-server/` | 旧服务端仓库（已迁移进 `spatialmem-repro/server`；其 `.venv` 可用于跑测试，`.env.docker` 含 DashScope Key） |
| `glasses-recordings/` | 眼镜/手机录制会话（含 pose.csv/imu.csv 等） |
| `paper/` | BlindAssist 论文草稿（避障方向，与空间记忆分开） |

## 2. 最新状态（2026-08-31 从这继续）

### 2.1 服务端：/v1/observe 识别结构性锚点（门/窗/墙）

`spatialmem-repro/server/app/observe/`（prompts.py / backend.py / router.py）：
`POST /v1/observe` 返回 `{name, color, location, attributes, confidence, support, anchors[]}`；
anchors 为 `{type: door|window|wall, name, direction: left|right|front|back, distance_m, confidence}`。
真实 VLM（qwen-vl-max）对手机拍的教室照片实测：门（right 6.1m）、窗（front 5.2m）、墙（left 3.0m）全部识别。

### 2.2 客户端：空间记忆 + 场景隔离 + 导航

- **锚点入库/检索**：`/v1/observe` 响应解析后入库（门/窗/墙独立节点），问"门在哪"答"在你偏右约6.1米外"；物体位置提到锚点自动挂 `nearAnchor`（"靠近门"）。
- **场景隔离**（`SceneRegistry.kt` + `sceneId/sceneName`）：门/窗/墙锚点指纹识别场景；家自动置顶；临时场景 7 天未回访归档；**去重只在场景内**（教室的门不与家里的门合并）；检索默认当前场景，"家里的门"显式跨场景。
- **回访恢复 + 语音置顶**（P0-2）：归档场景凭**强指纹匹配（≥0.7）或唯一名称**在 `lookup` 中复活（`revived=true`，连续 2 次观察共识后激活并找回旧记忆）；「把这里记为家/设为家/置顶这个场景」→ `PIN_SCENE` 命令 → `pinCurrentScene()` 置顶并重命名为「家」，同步更新该场景已入库节点的场景名，保证「家里的门」等跨场景查询可命中。
- **锚点多视角确认**（P0-3，`AnchorMultiViewConfirmer.kt`）：连续观察管线（`hint=continuous`，5s/次且画面变化才触发，天然多视角）中，锚点需**连续 N=2 帧方向一致 + 距离 2m 容差**才入库；单帧误报/方向变化/距离突变/断帧都推迟确认，已确认锚点不重复入库；场景解析仍用本帧全量锚点（待确认锚点不丢场景指纹证据），切换场景/关闭记忆时清空投票。
- **事件驱动采集**（P1-a，`ObservationEventDetector.kt`）：在 5s 周期 + 场景变化门控的被动观察之外，增加**驻足触发**的高分辨率观察——方位角连续稳定（默认 6s 内漂移 <7°）且场景未显著变化时补发一帧 1280px 的 `event:stationary` 结构化观察（不进多视角确认，事件冷却 20s 控频）；`onSceneChanged()` 在场景显著变化时重置驻足累计，避免「直线行走但朝向稳定」误判；触碰/开门预留 `event:touch` / `event:door_open` 显式事件入口，待触摸/门状态信号接入后复用同一高分辨率观察通道。
- **眼镜 POSE 通道供数**（位姿闭环第 1 档）：眼镜端 `GlassLinkService` 已实现 POSE(0x04) 通道（rotation vector 四元数，5ms 采样/100ms 一批，Wi-Fi Direct TCP 上行）；本次补齐**手机端消费**——`GlassLinkServer` 分发 POSE → `X3ProVideoSource` 解码并经 `ClockSyncEstimator` 换算时间戳、`PoseOrientation` 换算方位角 → `CaptureCoordinator.onGlassesPose` 喂给视觉位姿管线并落盘 pose.csv；收到眼镜 POSE 后自动锁定为位姿源（手机传感器不再喂视觉/落盘，保证 pose.csv 与眼镜视频同源），切回手机模式/停止时复位。
- **跟随视角 + 实时相对转向**（位姿闭环第 2 档第 1 阶段）：眼镜/手机方位角经 `MemoryLearningCoordinator.updateUserPose`（EMA 平滑 + 6Hz 节流）→ `NavPoseState(mapFacingRad)` → `NavigationOverlayView` 以用户为中心旋转小地图（heading-up）+ 绘制用户箭头；`planWithMap` 首段播报与「继续/下一步」剩余分段播报都用**实时朝向**计算相对转向（直行/左转约X度/右转约X度/调头），替换原静态方位。约定：发起导航时用户面向地图 +y（`mapFacing=π/2+startHeading-heading`）。剩余：位置递推（步数/IMU）与自动到达判定。
- **导航航点**（`MemoryNavigator.kt`）："带我去门口/去厨房/走到窗户那"→ 目标词解析 → 航点序列（锚点→物体/支撑物）→ 分步播报（"继续/下一步"推进）。
- **地图级导航**（`WalkableMap.kt`）：Kotlin 版占用网格 + A*（四邻域+转向惩罚）+ RDP 转折点 + 逐段播报；协调器 `navigate()` 优先地图（目标命中 map.goals 且默认起点存在），否则回退记忆方位航点。
- **可视化**（`NavigationOverlayView.kt`）：发起导航时叠加**俯视小地图**（障碍/地板/起点/门口/蓝色路径折线）；演示地图资产 `blindassist/app/src/main/assets/maps/office_demo.json`（办公室稠密地图 + 自动检测门口）。
- **位置递推 + 到达判定**（位姿闭环第 2 档第 2 阶段，2026-08-31）：眼镜端新增 **IMU(0x03) 通道**（`GlassLinkService` 加速度采样 50Hz/100ms 批，`ImuPayloadCodec` 线格式 u16 count + {i64 t, f32 ax,ay,az, u8 acc}；握手能力新末尾字段 `hasLinearAcceleration`，旧 APK 无尾字节按 false 兼容）；手机端 `X3ProVideoSource.onImuPacket`（时钟换算同 POSE 口径）→ `CaptureCoordinator.onGlassesImu` → `MemoryLearningCoordinator.updateUserImu`；`StepDetector`（幅值→高通 0.8s→低通 60ms→自适应阈值 1.6·EMA|·| 下界 1.2 m/s²，上升沿 + 250ms 不应期，dt 断流重置）；步事件 × 步长 0.7m × 实时 mapFacing 递推位置（导航发起时锚定计划起点），`NavPoseState.xM/yM` 填真实位置（替换原固定起点）；距目标 ≤1.5m 自动播报「已到达X附近」并 `clearNavigation()`（每次导航一次）。仅导航用不落盘，pose.csv 语义不变。眼镜无加速度计自动停用 IMU 通道（能力上报 false）。
- **手动服务端地址**（`linksee-client-android` 设置页「配置服务端地址」）：保存的地址同时写入 LinkSee 网关与 `/v1/observe` 运行时覆盖（`AppSettingsStore.linkSeeServerUrlOverride` / `cloudObserveUrlOverride`）；`MemoryLearningCoordinator.observeStructuredFromFrame` 优先用运行时 observe 覆盖，缺省回退 `BuildConfig.CLOUD_OBSERVE_URL`；「测试连接」改为探测 `/healthz`（再回退 `/health`），适配本地 Docker 统一服务端。

### 2.3 复现工程：稠密地图 + 路径规划（P0-1）

- `src/spatialmem/map2d.py`：2D 占用图（地板/障碍分层、障碍膨胀 0.35m 安全余量）+ A* + RDP + 播报；合成教室场景验收：**左绕长桌 → 走廊直行 → 右转到门**。
- `src/spatialmem/dense_map.py`：深度×位姿稠密融合 → 地板估计（首个显著频带）→ 占用图。
- `scripts/run_depth.py`：**MiDaS 深度仿射标定 `d=a/(disp-b)`**（关键修复：原纯比例标定忽略截距 b，实测 b≈315，造成地板系统性偏移；失败帧用成功帧中位数兜底）。
- `scripts/build_dense_map.py`：稠密融合 + 最大连通分量内最长路径验证。
- `scripts/plan_navigation.py`：CLI 从点云规划到目标。
- `src/spatialmem/query.py` + `relations.py`（P1-b）：补齐**完整谓词与多跳查询**——新增 `predicate_visible`（视线遮挡检测，射线对轴对齐盒 slab 测试）与 `multi_hop_query`（沿关系链逐跳行走，论文 §3.4 wall→window→mug）；`above`/`below`/`on`/`near`/`contains` 谓词与单跳 `relational_query` 此前已有。
- `src/spatialmem/anchor_eval.py` + `scripts/eval_anchor_relations.py`（P1-c）：**门/窗/墙锚点关系评测**——按 `type+direction+距离容差` 匹配预测与 GT 锚点，输出每种锚点的 grounding F1（对标论文 Scene 1 门/窗 0.82、墙 0.88）与宏观关系得分；教室照片真实 VLM 响应实测 3/3 全对（门 0.82→1.0、窗 0.82→1.0、墙 0.88→1.0，单样本演示）。

**真实场景验证（办公室 indoor_walk_full，169 帧）**：251.5 万稠密点、地板证据 1389 格、最大连通分量 934 格、最长路径 6 转折点跑通。

## 3. 关键文件索引

### 服务端
- `server/app/observe/prompts.py` / `backend.py` / `router.py`：anchors 提示词、解析、响应模型
- `server/tests/unit/test_observe.py`、`server/tests/integration/test_observe_endpoint.py`：observe 测试

### 客户端（两份工程同源）
- `app/src/main/java/com/example/blindassist/spatialmem/`：
  - `MemoryModels.kt`（ConfirmedNode + StructuralAnchor + sceneId）
  - `MemoryLearningStore.kt`（JSONL 持久化，legacy 归旧场景）
  - `MemoryLearningCoordinator.kt`（观察入库/场景切换/检索/导航/位置递推/到达判定）
  - `MemoryNavigator.kt`（导航航点 + MapNavPlan + advanceStep/isArrived 纯数学）
  - `StepDetector.kt`（步态检测：带通 + 自适应阈值 + 不应期）
  - `WalkableMap.kt`（Kotlin 地图 A*）
  - `SceneRegistry.kt`（场景注册表）
  - `NavigationOverlayView.kt`（俯视小地图叠加）
  - `ObservationEventDetector.kt`（事件驱动采集：驻足检测 + 触碰/开门事件入口）
- `link/`：`ImuPayloadCodec.kt`（IMU 0x03 线格式）+ `ControlCodec`（hasLinearAcceleration 能力字段）
- `app/.../link/transport/`：`X3ProVideoSource.onImuPacket`（IMU 解码→手机时钟域）、`GlassLinkServer`（IMU 通道分发）
- `glasses/.../GlassLinkService.kt`：加速度采样 50Hz/100ms 批上行（`startImuCapture`）
- `app/.../capture/CaptureCoordinator.kt`：`onGlassesImu` 接线 + `onNavArrived` 到达播报
- `app/src/main/java/com/example/blindassist/voice/VoiceCommandRouter.kt`：NAVIGATE 命令
- `app/src/main/java/com/example/blindassist/capture/CaptureCoordinator.kt`：接线（加载演示地图、onNavigationPath 回调）
- `app/src/main/res/layout/activity_main.xml`：navOverlay 叠加层
- `app/src/main/assets/maps/office_demo.json`：演示地图
- 测试：`app/src/test/java/com/example/blindassist/spatialmem/`（含 SceneRegistryTest、WalkableMapTest、MemoryNavigatorTest、StepDetectorTest、SpatialAnchorSimulationTest）；`link/src/test/`（含 ImuPayloadCodecTest、ControlCodecTest 兼容回归门）

### 复现工程
- `src/spatialmem/map2d.py`、`dense_map.py`、`src/spatialmem/anchors.py`（墙 RANSAC，门/窗几何开口检测仍未做）
- `src/spatialmem/query.py`（locate/relational_query/multi_hop_query）、`src/spatialmem/relations.py`（on/above/below/near/contains/visible 谓词）
- `src/spatialmem/anchor_eval.py`（P1-c 锚点关系评测）、`scripts/eval_anchor_relations.py`（CLI）
- `tests/test_map2d.py`、`tests/test_dense_map.py`
- `tests/test_query.py`（多跳 wall→window→mug + 可见性遮挡）
- `tests/test_anchor_eval.py`、`data/classroom/anchor_gt.json`（教室照片锚点 GT）
- `data/`：`indoor_walk_full`（办公室，含深度图/仿射标定）、`cup_walk`（桌面场景，不适合验证地板）、`new_scene`

## 4. 环境与验证命令

### 服务端测试
```bash
cd spatialmem-repro/server
PYTHONPATH=/Users/pwn/Desktop/研究生课程/001\ 导引课/development/spatialmem-repro/server \
  /Users/pwn/Desktop/研究生课程/001\ 导引课/development/linksee-server/.venv/bin/python -m pytest -q
# 276 passed
```

### 复现工程测试
```bash
cd spatialmem-repro
.venv/bin/python -m pytest tests -q   # 67 passed
# lint（只查改动文件）：linksee-server/.venv/bin/ruff check src/spatialmem/map2d.py ...
```

### 客户端测试
```bash
cd blindassist   # 或 linksee-client-android（需额外 export ANDROID_HOME=/Users/pwn/.blindassist-toolchain/android-sdk）
export JAVA_HOME=/Users/pwn/.blindassist-toolchain/jdk/Contents/Home
export GRADLE_USER_HOME=/Users/pwn/.blindassist-toolchain/gradle-home
./gradlew :app:testDebugUnitTest --offline
```

### Docker 本地服务端（真实 VLM）
```bash
docker ps --filter name=linksee-server-anchors   # 容器：localhost:8000，镜像 linksee-server:anchors
# 重建：docker build --build-arg EXTRAS= -t linksee-server:anchors spatialmem-repro/server
# 启动：docker run -d --name linksee-server-anchors -p 8000:8000 \
#   -e ENV=dev -e INFERENCE_BACKEND=dashscope -e DASHSCOPE_API_KEY -e DASHSCOPE_BASE_URL \
#   -e OBSERVE_MODEL=qwen-vl-max -e OBSERVE_TIMEOUT_S=90 -e OBSERVE_MAX_TOKENS=512 \
#   -e KV_BACKEND=memory -e DB_BACKEND=null -e KB_BACKEND=null linksee-server:anchors
# Key 从 linksee-server/.env.docker 读取（DASHSCOPE_API_KEY）
```

### 手机照片测试资产
- 教室照片：`/private/tmp/classroom_test.jpg`（从手机 RFCN20GLDWX 的 DCIM/Camera 拉取）
- Docker 服务端实测响应：`/private/tmp/observe_resp.json`（门 right 6.1m / 窗 front 5.2m / 墙 left 3.0m）
- 客户端全链路模拟：`SpatialAnchorSimulationTest`（解析→入库→"门在哪/带我去门口"）

## 5. P0/P1 路线图与下一步

| 项 | 内容 | 状态 |
|---|---|---|
| P0-1a | 2D 可通行地图 + A* 路径规划（map2d.py） | ✅ |
| P0-1b | 稠密地板/可通行图重建（深度仿射标定 + 稠密融合） | ✅ 核心链路；剩余：标定精度调优（远像素截断致 floor_z 略偏，可做 (a,b) 时间平滑 + 全局地板平面对齐） |
| 客户端集成 | 地图航点 + 俯视小地图可视化 | ✅（演示地图资产） |
| P0-2 | 回访恢复 + 语音置顶（"把这里记为家"） | ✅（归档场景强指纹/唯一名称复活；PIN_SCENE 命令置顶为家并同步节点场景名） |
| P0-3 | 锚点多视角确认（连续多帧一致才入库） | ✅（`AnchorMultiViewConfirmer`：连续 2 帧方向一致+距离 2m 容差；断帧衰减、切换场景清空；仅作用于连续观察管线，主动询问仍即时入库） |
| P1-a | 事件驱动采集（驻足/触碰/开门触发） | ✅ 驻足自动检测已接入（朝向稳定 6s + 场景未变 → 1280px `event:stationary` 高分辨率观察，冷却 20s）；触碰/开门预留 `event:touch` / `event:door_open` 事件入口待信号接入 |
| P1-b | 完整谓词 + 多跳查询（above/below、可见性、wall→window→mug） | ✅ `predicate_visible`（视线遮挡 slab 测试）+ `multi_hop_query`（沿关系链逐跳，wall→window→mug 单测覆盖）；above/below/on/near/contains 与单跳 relational_query 此前已有 |
| P1-c | 门/窗锚点关系评测（对标原文 Scene 1 的 0.82） | ✅ `anchor_eval.py` + `scripts/eval_anchor_relations.py`：按 type+direction+距离容差匹配，输出每类型 grounding F1 对标 0.82/0.88；教室照片真实 VLM 响应 3/3 全对（单样本演示，缺大规模 GT 场景，离线几何开口检测仍未做） |
| 位姿闭环 | 眼镜 POSE 通道供数 + 相机注册 AR 叠加 + 实时跟随导航 | 🟡 第 1 档 ✅（POSE 消费链路 + pose.csv 同源）；第 2 档第 1 阶段 ✅（跟随视角：heading-up 小地图 + 用户箭头 + 实时相对转向播报，单测覆盖纯数学）；第 2 档第 2 阶段 ✅（IMU 通道 + StepDetector 步态检测 + 位置递推 + 到达自动播报收尾，纯数学单测覆盖）；剩余：相机 AR 叠加、步长自适应（漂移大时引入步频/身高先验）、真机眼镜联调验证步数精度 |

## 6. 已知问题与重要决策

### 已知问题/边界
- **无实时位姿（大部分解决）**：眼镜 POSE + IMU 已接入，位置递推驱动小地图与到达判定；剩余相机 AR 叠加未做。递推步长固定 0.7m（无绝对尺度参考），长距离行走会漂移，待真机联调评估是否引入步长自适应。
- **StepDetector 离线标定（2026-09-01，`scripts/eval_step_detector.py`）**：StepDetector.kt 的 Python 逐行移植（`--self-test` 复刻全部 7 个单测向量，结果一致）+ 真实 IMU 回放。结论：
  1. **真实数据假阳性 0**：唯一有 IMU 的会话（hq720p，83.2s 手机静止，50.3Hz）检出 0 步——正确，且含 5.17 m/s² 单次冲击瞬态不虚警；纯噪声地板扫描显示 MIN_THRESHOLD ≥0.3 即零虚警（0.15–0.2 仅因该冲击 1 次虚警），当前 1.2 下界余量约 4 倍。
  2. **灵敏度悬崖（关键发现）**：半波步态经高通（扣均值）+ 低通后有效峰值仅 **0.5–0.6×A**（A 为合加速度步态幅值），MIN_THRESHOLD=1.2 要求 A ≥ 2.2–2.8 m/s² 才能检出——弱步态（A<2）完全漏检；且连续行走下自适应阈值（1.6×EMA≈0.48×峰值）恒小于 1.2 下界，**实际上被下界压死，自适应机制是死代码**。
  3. **已应用（两客户端同步）**：MIN_THRESHOLD 降到 **0.6**（混合测试：A≥1.5 全步频 100% 检出、A=1.2@1.4Hz 101%、静止虚警 0；注意噪声地板是"手机放桌上"口径，眼镜佩戴时头动/说话会更高）。StepDetector.kt 常量与注释已更新（blindassist 与 linksee-client-android 同步），7 个单测全过；Python 移植默认值同步。真机联调时录「静止 60s + 正常行走 60s」两段，用同一脚本回放定 A 分布后终定。
  4. 其余参数（REFRACTORY 250ms、FAC 1.6、LP/HP τ）在扫描中不敏感，暂不动。
- **真机步态参数未标定**：见上条；实机步频/加速度口径（LINEAR_ACCELERATION vs ACCELEROMETER 回退）需眼镜联调后复核。现有 glasses-recordings 其余会话 imu.jsonl 均为空（已知差距），无真实行走 IMU 数据。
- **COLMAP 稀疏点云地板点极少**（24~83 点）：必须走深度稠密融合；`cup_walk` 是桌面场景（最低点 0.89m），不适合验证地板，用 `indoor_walk_full`。
- **MiDaS disparity 与深度是仿射关系**（`disp=a/d+b`，b 实测≈315）：已修 run_depth.py，勿回退纯比例。
- **numpy `[x,y]` 与 Kotlin row-major 网格序不一致**：导出 JSON 地图必须 `occ.T.ravel()`，否则障碍错位（已修，勿回退）。
- **Docker Desktop 怪癖**：venv httpx 大请求体返回 503（用 curl 正常）；docker CLI 偶发 `permission denied`（重试即可）。
- 复现工程整体 lint 非全绿（历史遗留），只保证**本次改动文件** lint 干净。

### 重要决策
- **不做短期效果**：导航绕障不走"VLM 布局提示"式补丁，采用真正的度量地图 + 路径规划方案。
- **场景隔离**：场景内强隔离 + 跨场景显式复用（教室的门绝不与家里的门合并）。
- **导航双通道**：有地图走格点 A* 航点，无地图回退记忆方位航点。
- **标定正确性优先**：深度用仿射模型，失败帧用中位数兜底，不引入 b=0 的系统偏移。

## 7. 参考文档

- `docs/空间物体关系记忆-结论与SpatialMem复现计划.md`：核心结论（§2）、架构（§3）、复现计划与差距（§4）、场景隔离（§4.7）、导航航点（§4.8）、P0/P1 路线图（§4.9）
- 论文：SpatialMem（arXiv:2601.14895）——门/窗/墙为 L1 锚点层，导航=锚点+物体航点序列
