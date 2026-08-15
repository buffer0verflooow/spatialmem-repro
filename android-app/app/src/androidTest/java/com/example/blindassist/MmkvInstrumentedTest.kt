package com.example.blindassist

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.blindassist.util.AppSettingsStore
import com.tencent.mmkv.MMKV
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MmkvInstrumentedTest {

    @Test
    fun packagedMmkvInitializesAndPersistsSettings() {
        // MMKV reports its version with a leading "v" (e.g. "v2.1.0"); the point of the
        // check is that the packaged native library matches the Java API version.
        assertEquals("2.1.0", MMKV.version().removePrefix("v"))

        val settings = AppSettingsStore()
        settings.selectedSceneModelId = "instrumentation-probe"
        assertEquals("instrumentation-probe", settings.selectedSceneModelId)

        settings.selectedSceneModelId = null
        assertTrue(settings.selectedSceneModelId == null)
    }
}
