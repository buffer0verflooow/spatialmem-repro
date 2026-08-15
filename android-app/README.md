# BlindAssist 眼镜视频链路（精简版）

空间记忆复现链路的 APP 端：雷鸟 X3 Pro 眼镜 → 手机的视频数据传输与落盘。

这是原 `blindassist`（buffer0verflooow/blindassist）的**精简版**：只保留
眼镜视频采集上行（glasses）、链路协议（link）、手机端接收解码与 H.264 落盘
（app/link/transport + recording）、无线连接（p2p）和 PC 端录制脚本（scripts）。
无障碍 UI、视觉/风险/语音等与复现无关的模块已移除。

## 数据流

```text
雷鸟 X3 Pro（glasses 模块）
  Camera2 → H.264 → TCP 上行（link 协议，15 FPS 640×360）
        ↓
手机（app 模块）
  GlassLinkServer 接收 → H264Decoder 解码 → X3ProVideoSource
  h264-tee 原样落盘：video.h264 + video_timeline.csv（零转码）
        ↓
PC（scripts/m1_record.py）
  会话目录 video.h264 + video_timeline.csv → ffmpeg 转 mp4 / 供 COLMAP 等分析
```

## 目录

```text
android-app/
├── glasses/     # 眼镜端：采集编码上行（独立 App，仅 core-ktx 依赖）
├── link/        # 纯 JVM 协议：编解码/状态机/时钟对齐/过期门（可独立测试）
├── p2p/         # Wi-Fi Direct 无线连接
├── app/         # 手机端：接收/解码/落盘（link/transport + recording + source + util）
├── scripts/     # PC 端录制/回放：m1_record.py / m1_mock_glasses.py / m1_mock_phone.py / pose_check.py
└── docs/        # M1 眼镜手机链路设计
```

## 构建与测试

```bash
# 需要 Android SDK（compileSdk 35, minSdk 29）
./gradlew :link:test :app:compileDebugKotlin :glasses:compileDebugKotlin :p2p:compileDebugKotlin
```

链路协议（link）为纯 JVM 模块，89 个单测覆盖编解码/状态机/时钟同步。

## 录制示例

```bash
# 眼镜模式录 30–60s，手机会话目录出现 video.h264（>1MB）与 video_timeline.csv
python3 scripts/m1_record.py --label indoor_walk --seconds 60
```
