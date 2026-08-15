plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.blindassist"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.example.blindassist"
        minSdk = 29
        targetSdk = 35
        versionCode = 15
        versionName = "0.6.0-scene-description"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        buildConfig = true
    }
}

dependencies {
    implementation(project(":link"))
    implementation("androidx.core:core-ktx:1.15.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
