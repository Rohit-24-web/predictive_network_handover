# No obfuscation rules are needed: this app has no reflection-based serialisation
# (JSON is built by hand in TelemetryModels.kt) and no dynamic class loading.
# The file must exist because app/build.gradle.kts references it in the release
# buildType; an absent file fails the AGP configuration phase.
-dontwarn kotlinx.coroutines.**
