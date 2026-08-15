plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.blindassist.glasses"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.example.blindassist.glasses"
        minSdk = 29
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0-m1-03"

        ndk {
            abiFilters += "arm64-v8a"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(project(":link"))
    implementation(project(":p2p"))
    // 仅用于前台服务通知；不引入 CameraX / TFLite / ONNX / MediaPipe / ML Kit / MMKV。
    implementation("androidx.core:core-ktx:1.15.0")
}
