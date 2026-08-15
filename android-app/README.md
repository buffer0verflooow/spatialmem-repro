# 手机摄像头视频采集器（可选）

空间记忆复现需要一段第一视角视频。最简单的方式就是用普通手机摄像头录一段
视频，然后交给核心算法处理。本目录是一个可选的 Android 采集器，方便在手机
上直接录制并保存到统一目录。

**不依赖任何特定硬件**：普通 Android 手机即可。

## 数据流

```text
手机摄像头（本 App）
  录制 mp4 → App 私有目录 files/SpatialMem/spatialmem_<时间戳>.mp4
        ↓
PC（仓库根目录 scripts/prepare_session.py）
  抽帧 + 时间线 → data/ 会话目录 → 核心算法（src/spatialmem）
```

也可以完全不用本 App：用手机自带相机录像，把 mp4 拷到电脑后
`scripts/prepare_session.py` 一样能处理。

视频文件位于 `Android/data/com.example.spatialmem.capture/files/SpatialMem/`，
可用 `adb pull` 或文件管理器导出。

## 构建与运行

```bash
# 需要 Android SDK（compileSdk 35, minSdk 29）
./gradlew :app:assembleDebug
```

安装到手机后：授权相机/麦克风 → 开始录制 → 走动一圈 → 停止。
视频保存在系统相册 Movies/SpatialMem 下。

## 目录

```text
android-app/
├── app/         # Android 采集器（CameraX：预览 + 录制 mp4）
└── README.md
```
