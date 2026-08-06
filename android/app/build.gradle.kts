plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "com.hks.app"
    compileSdk = 33

    defaultConfig {
        applicationId = "com.hks.app"
        minSdk = 24
        targetSdk = 33
        versionCode = 1
        versionName = "1.0"
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        buildPython("python3")
        pyc {
            src = false
        }
        pip {
            install("Flask==3.1.3")
            install("requests==2.34.2")
            install("numpy")
            install("pandas")
            install("easy-tdx==1.20.4")
        }
    }
}

val copyPythonSources = tasks.register<Copy>("copyPythonSources") {
    from(rootProject.file("..")) {
        include("*.py")
        include("frontend/**")
        include("config.example.json")
        exclude("wsgi.py")
    }
    into(layout.projectDirectory.dir("src/main/python"))
}

tasks.named("preBuild") {
    dependsOn(copyPythonSources)
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.6.1")
}
