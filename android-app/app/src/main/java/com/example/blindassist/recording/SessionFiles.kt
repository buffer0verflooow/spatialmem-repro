package com.example.blindassist.recording

import java.io.File

data class SessionFiles(
    val directory: File,
    val video: File = File(directory, "video.mp4"),
    /** 眼镜模式原始 H.264 码流（Annex-B），SpatialMem 复现取数用。 */
    val glassesVideo: File = File(directory, "video.h264"),
    /** 眼镜模式 H.264 时间线（frame_index,sender_ts_ns,arrival_ns,flags,bytes）。 */
    val glassesVideoTimeline: File = File(directory, "video_timeline.csv"),
    val audio: File = File(directory, "audio.wav"),
    val audioTimeline: File = File(directory, "audio.csv"),
    val vadActivity: File = File(directory, "vad_activity.csv"),
    val frames: File = File(directory, "frames.csv"),
    val imu: File = File(directory, "imu.csv"),
    val pose: File = File(directory, "pose.csv"),
    val interactions: File = File(directory, "interactions.jsonl"),
    val detections: File = File(directory, "detections.jsonl"),
    val depth: File = File(directory, "depth.jsonl"),
    val distanceEstimates: File = File(directory, "distance_estimates.jsonl"),
    val groundHazardCandidates: File = File(directory, "ground_hazard_candidates.jsonl"),
    val groundHazardEvents: File = File(directory, "ground_hazard_events.jsonl"),
    val riskEvents: File = File(directory, "risk_events.jsonl"),
    val riskAssessments: File = File(directory, "risk_assessments.jsonl"),
    val guidanceEvents: File = File(directory, "guidance_events.jsonl"),
    val voiceCommands: File = File(directory, "voice_commands.jsonl"),
    /** 语音/按钮求助记录（工单 V-04 §3.3：时间戳/触发方式/帧摘要/OCR 摘要/播报是否成功）。 */
    val helpRequests: File = File(directory, "help_requests.jsonl"),
    val inferenceLatency: File = File(directory, "inference_latency.csv"),
    val deviceMetrics: File = File(directory, "device_metrics.csv"),
    val configSnapshot: File = File(directory, "config_snapshot.json"),
    val stageBConfigSnapshot: File = File(directory, "stage_b_config_snapshot.json"),
    val stageCConfigSnapshot: File = File(directory, "stage_c_config_snapshot.json"),
    val stageDConfigSnapshot: File = File(directory, "stage_d_config_snapshot.json"),
    val metadata: File = File(directory, "session.json"),
    val summary: File = File(directory, "session_summary.json"),
    val evidenceDirectory: File = File(directory, "evidence")
)
