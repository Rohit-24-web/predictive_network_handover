package com.example.handover

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * Runtime configuration. The backend URL lives here and nowhere else, so it is
 * configurable rather than scattered as a hardcoded literal.
 */
class AppConfig(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    var backendBaseUrl: String
        get() = prefs.getString(KEY_URL, DEFAULT_BASE_URL) ?: DEFAULT_BASE_URL
        set(value) = prefs.edit().putString(KEY_URL, value.trimEnd('/')).apply()

    var samplingIntervalMs: Long
        get() = prefs.getLong(KEY_INTERVAL, DEFAULT_INTERVAL_MS)
        set(value) = prefs.edit().putLong(KEY_INTERVAL, value.coerceIn(MIN_INTERVAL_MS, 60_000L)).apply()

    var allowSinrEstimate: Boolean
        get() = prefs.getBoolean(KEY_ESTIMATE, false)
        set(value) = prefs.edit().putBoolean(KEY_ESTIMATE, value).apply()

    /**
     * Anonymous, locally generated device label. Deliberately NOT IMEI, IMSI, the
     * phone number or the Android ID: the backend only needs something stable to
     * group samples, so there is no reason to touch a real identifier.
     */
    val anonymousDeviceId: String
        get() {
            prefs.getString(KEY_DEVICE, null)?.let { return it }
            val generated = "android-" + UUID.randomUUID().toString().take(8)
            prefs.edit().putString(KEY_DEVICE, generated).apply()
            return generated
        }

    companion object {
        private const val PREFS = "handover_telemetry_prefs"
        private const val KEY_URL = "backend_base_url"
        private const val KEY_INTERVAL = "sampling_interval_ms"
        private const val KEY_ESTIMATE = "allow_sinr_estimate"
        private const val KEY_DEVICE = "anonymous_device_id"

        /** 10.0.2.2 is the host loopback as seen from the Android emulator. */
        const val DEFAULT_BASE_URL = "http://10.0.2.2:8000"

        /** ~1 Hz matches the backend's sampling assumption. */
        const val DEFAULT_INTERVAL_MS = 1_000L

        /** Guard against an aggressive polling loop draining the battery. */
        const val MIN_INTERVAL_MS = 500L

        /** Fresh per app launch: stable while running, not persisted across restarts. */
        fun newSessionId(): String = "android-" + UUID.randomUUID().toString().take(12)
    }
}
