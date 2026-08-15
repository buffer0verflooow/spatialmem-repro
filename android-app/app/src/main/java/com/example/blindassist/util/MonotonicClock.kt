package com.example.blindassist.util

import android.os.SystemClock

fun interface MonotonicClock {
    fun nowNs(): Long

    companion object {
        val SYSTEM = MonotonicClock { SystemClock.elapsedRealtimeNanos() }
    }
}

