package com.example.blindassist.recording

import java.io.File

data class FrameTimelineEntry(
    val frameIndex: Long,
    val captureTimestampNs: Long,
    val width: Int,
    val height: Int,
    val rotationDegrees: Int
)

object FrameTimelineParser {
    fun parse(file: File): List<FrameTimelineEntry> {
        if (!file.isFile) return emptyList()
        return file.useLines { lines ->
            lines
                .drop(1)
                .filter { it.isNotBlank() }
                .mapNotNull(::parseLine)
                .toList()
        }
    }

    private fun parseLine(line: String): FrameTimelineEntry? {
        val columns = line.split(',')
        if (columns.size < 5) return null
        return runCatching {
            FrameTimelineEntry(
                frameIndex = columns[0].toLong(),
                captureTimestampNs = columns[1].toLong(),
                width = columns[2].toInt(),
                height = columns[3].toInt(),
                rotationDegrees = columns[4].toInt()
            )
        }.getOrNull()
    }
}

