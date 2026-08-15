package com.example.blindassist.source

/**
 * 主视频源选择：眼镜采集流（首选）或手机摄像头（辅助/备用）。
 *
 * 眼镜不可用/效果不好时切换到 [PHONE_CAMERA]，用手机摄像头继续走同一套
 * 视觉/风险/播报管线；眼镜恢复正常后切回 [GLASSES]。
 */
enum class VideoSourceMode { GLASSES, PHONE_CAMERA }
