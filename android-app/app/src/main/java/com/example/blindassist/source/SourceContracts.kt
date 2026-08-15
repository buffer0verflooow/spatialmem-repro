package com.example.blindassist.source

import java.io.File

enum class SourceState {
    IDLE,
    STARTING,
    RUNNING,
    STOPPING,
    ERROR
}

data class VideoFrame(
    val frameIndex: Long,
    val captureTimestampNs: Long,
    val width: Int,
    val height: Int,
    val rotationDegrees: Int,
    val rgba8888: ByteArray
)

data class AudioChunk(
    val captureTimestampNs: Long,
    val sampleRateHz: Int,
    val channelCount: Int,
    val samples: ShortArray
)

enum class ImuSensorType(val wireName: String) {
    ACCELEROMETER("accelerometer"),
    GYROSCOPE("gyroscope")
}

data class ImuSample(
    val captureTimestampNs: Long,
    val sensorType: ImuSensorType,
    val x: Float,
    val y: Float,
    val z: Float,
    val accuracy: Int
)

data class PoseSample(
    val captureTimestampNs: Long,
    val quaternionX: Float,
    val quaternionY: Float,
    val quaternionZ: Float,
    val quaternionW: Float,
    val azimuthRad: Float,
    val pitchRad: Float,
    val rollRad: Float,
    val accuracy: Int
)

data class VideoSourceRequest(
    val videoOutputFile: File? = null,
    /** 眼镜模式：把收到的 H.264 码流原样落盘（SpatialMem 复现临时用，零转码）。 */
    val h264TeeFile: File? = null,
    /** 眼镜模式：H.264 落盘时间线，与 PC 端 m1_record.py 的 video_timeline.csv 对齐。 */
    val videoTimelineFile: File? = null
)

interface VideoSource {
    fun start(
        request: VideoSourceRequest,
        onFrame: (VideoFrame) -> Unit,
        onState: (SourceState, String) -> Unit,
        onError: (Throwable) -> Unit
    )

    fun stop()
}

interface AudioSource {
    fun start(
        onAudio: (AudioChunk) -> Unit,
        onState: (SourceState, String) -> Unit,
        onError: (Throwable) -> Unit
    )

    fun stop()
}

interface PoseSource {
    fun start(
        onImu: (ImuSample) -> Unit,
        onPose: (PoseSample) -> Unit,
        onState: (SourceState, String) -> Unit,
        onError: (Throwable) -> Unit
    )

    fun stop()
}
