package com.example.spatialmem.capture

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.example.spatialmem.capture.databinding.ActivityMainBinding
import java.io.File

/**
 * 空间记忆复现用的手机摄像头采集器。
 *
 * 录制第一视角视频（mp4），保存到 MediaStore Movies/SpatialMem 目录。
 * 后续用仓库根目录 `scripts/prepare_session.py` 抽帧生成会话数据。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var videoCapture: VideoCapture<Recorder>? = null
    private var recording: Recording? = null
    private var isRecording = false

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
            if (result.values.all { it }) {
                startCamera()
            } else {
                Toast.makeText(this, "需要相机与麦克风权限", Toast.LENGTH_SHORT).show()
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.recordButton.setOnClickListener {
            if (isRecording) stopRecording() else startRecording()
        }

        val required = listOf(
            Manifest.permission.CAMERA,
            Manifest.permission.RECORD_AUDIO,
        )
        if (required.any {
                ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
            }
        ) {
            permissionLauncher.launch(required.toTypedArray())
        } else {
            startCamera()
        }
    }

    private fun startCamera() {
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            val provider = providerFuture.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.previewView.surfaceProvider)
            }
            val recorder = Recorder.Builder()
                .setQualitySelector(
                    androidx.camera.video.QualitySelector.fromOrderedList(
                        listOf(
                            androidx.camera.video.Quality.FHD,
                            androidx.camera.video.Quality.SD,
                            androidx.camera.video.Quality.LOWEST,
                        ),
                        androidx.camera.video.FallbackStrategy.lowerQualityOrHigherThan(
                            androidx.camera.video.Quality.SD
                        ),
                    )
                )
                .build()
            videoCapture = VideoCapture.withOutput(recorder)
            provider.unbindAll()
            provider.bindToLifecycle(
                this,
                CameraSelector.DEFAULT_BACK_CAMERA,
                preview,
                videoCapture,
            )
        }, ContextCompat.getMainExecutor(this))
    }

    private fun startRecording() {
        val capture = videoCapture ?: return
        val dir = File(getExternalFilesDir(null), "SpatialMem")
        dir.mkdirs()
        val output = File(dir, "spatialmem_${System.currentTimeMillis()}.mp4")
        val options = FileOutputOptions.Builder(output).build()
        recording = capture.output
            .prepareRecording(this, options)
            .withAudioEnabled()
            .start(ContextCompat.getMainExecutor(this)) { event ->
                when (event) {
                    is VideoRecordEvent.Finalize -> {
                        if (event.hasError()) {
                            Toast.makeText(this, "录制失败: ${event.error}", Toast.LENGTH_SHORT).show()
                        } else {
                            Toast.makeText(this, "已保存: ${output.absolutePath}", Toast.LENGTH_LONG).show()
                        }
                        isRecording = false
                        binding.recordButton.text = getString(R.string.start_record)
                    }
                    is VideoRecordEvent.Start -> {
                        isRecording = true
                        binding.recordButton.text = getString(R.string.stop_record)
                    }
                    else -> Unit
                }
            }
    }

    private fun stopRecording() {
        recording?.stop()
        recording = null
    }

    override fun onDestroy() {
        super.onDestroy()
        recording?.stop()
    }
}
