plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jarvis.assistant"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jarvis.assistant"

        // API 30 is the floor because setUserAuthenticationParameters — the
        // call that binds the signing key to a per-use biometric check — only
        // exists from R onwards. On older releases the only option is a
        // validity window measured in seconds, which would mean one fingerprint
        // could authorise several approvals. Not worth supporting.
        minSdk = 30
        targetSdk = 34

        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            // Left off deliberately. This APK is sideloaded onto one phone by
            // the person who built it; shrinking buys nothing and makes stack
            // traces from your own assistant harder to read.
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")

    // BiometricPrompt, and the CryptoObject plumbing that ties a fingerprint
    // to a specific signing operation rather than to a vague recent unlock.
    implementation("androidx.biometric:biometric:1.1.0")

    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // OkHttp over Retrofit: the API is a handful of endpoints, and a JSON
    // library plus a HTTP client is less machinery than an interface generator.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    testImplementation("junit:junit:4.13.2")
}
