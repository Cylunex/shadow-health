plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.parcelize")  // Samsung Health Data SDK 要求
}

android {
    namespace = "com.shadowverse.health"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.shadowverse.health"
        // 2.0 起随 Samsung Health Data SDK 强制 minSdk 29（Android 10）；
        // 1.x 手工流水线时代为 26
        minSdk = 29
        targetSdk = 34
        versionCode = 2
        versionName = "2.0"
    }

    buildTypes {
        getByName("debug") { isMinifyEnabled = false }
        // 自用局域网应用：release 也用 debug 签名直装（沿用旧壳做法）
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    sourceSets {
        getByName("main").java.srcDirs("src/main/kotlin")
    }
}

dependencies {
    // Samsung Health Data SDK 本地 AAR（不入仓，见 libs/README.txt）
    implementation(fileTree(mapOf("dir" to "libs", "include" to listOf("*.aar"))))
    implementation("com.google.code.gson:gson:2.13.2")  // SDK 依赖

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("androidx.work:work-runtime-ktx:2.9.1")  // 周期同步
}
