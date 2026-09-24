package com.example.handover

/**
 * Data model and JSON serialisation for REAL terrestrial cellular telemetry.
 *
 * DESIGN NOTE
 * -----------
 * This file contains NO Android imports on purpose. Serialisation, SINR state
 * mapping and plausibility checks are the parts worth unit-testing, and keeping
 * them free of `android.*` types means they run on a plain JVM with no device,
 * no emulator and no Robolectric.
 *
 * BOUNDARY: everything here describes TERRESTRIAL CELLULAR measurements from the
 * handset's modem. Nothing here is satellite telemetry. The backend's LEO/MEO/GEO
 * candidate paths are simulated and are not represented in this payload.
 */

/** Provenance of the SINR value. A missing SINR is never silently turned into a number. */
enum class SinrSource(val wire: String) {
    /** The modem actually reported SINR. */
    MEASURED("measured"),

    /** The device did not report SINR. Sent as null; the backend marks the sample not-ready. */
    UNAVAILABLE("unavailable"),

    /**
     * The backend derived SINR from RSRQ because the caller opted in
     * (`allow_sinr_estimate = true`). The app never estimates locally — estimation
     * is the backend's documented mechanism, and duplicating it here would risk the
     * two drifting apart.
     */
    ESTIMATED("estimated");

    companion object {
        fun fromWire(value: String?): SinrSource =
            values().firstOrNull { it.wire == value } ?: UNAVAILABLE
    }
}

/** Network technology, mapped to the vocabulary the backend accepts. */
enum class NetworkType(val wire: String) {
    NR("NR"), LTE("LTE"), UMTS("UMTS"), GSM("GSM"), UNKNOWN("UNKNOWN");

    companion object {
        /**
         * Map an Android `TelephonyManager.NETWORK_TYPE_*` constant to the backend
         * vocabulary. Unrecognised technologies map to UNKNOWN rather than guessing.
         *
         * Constants are referenced numerically so this stays JVM-testable.
         */
        fun fromAndroidNetworkType(networkType: Int): NetworkType = when (networkType) {
            20 -> NR                                    // NETWORK_TYPE_NR
            13 -> LTE                                   // NETWORK_TYPE_LTE
            3, 8, 9, 10, 15, 17 -> UMTS                 // UMTS/HSPA family
            1, 2, 16 -> GSM                             // GPRS/EDGE/GSM
            else -> UNKNOWN
        }
    }
}

/**
 * Values Android uses to mean "this measurement is not available".
 *
 * `CellInfo.UNAVAILABLE` is `Integer.MAX_VALUE`. Several OEM modems additionally
 * report `Integer.MIN_VALUE` or 0 for absent LTE fields. Any of these must become
 * null — sending 2147483647 dB as if it were a measurement would be a lie the
 * backend could not detect.
 */
object AndroidSentinels {
    const val UNAVAILABLE_INT: Int = Int.MAX_VALUE

    fun sanitize(raw: Int?): Int? = when (raw) {
        null, Int.MAX_VALUE, Int.MIN_VALUE -> null
        else -> raw
    }
}

/**
 * Plausible ranges for terrestrial cellular measurements.
 *
 * These mirror the published M5 backend contract. The backend remains authoritative
 * and re-validates everything; checking here simply avoids sending a sample we
 * already know will be rejected, and lets the UI show N/A instead of a bogus number.
 */
object CellularRanges {
    val RSRP_DBM = -140.0..-40.0
    val RSRQ_DB = -25.0..-3.0
    val RSSI_DBM = -120.0..-30.0
    val SINR_DB = -20.0..40.0

    fun plausible(value: Double?, range: ClosedFloatingPointRange<Double>): Double? =
        if (value != null && value.isFinite() && value in range) value else null
}

/**
 * One REAL cellular measurement.
 *
 * Null means "this device did not provide it" — never "zero" and never a default.
 */
data class CellularSample(
    val timestampSeconds: Double,
    val rsrpDbm: Double?,
    val rsrqDb: Double?,
    val rssiDbm: Double?,
    val sinrDb: Double?,
    val networkType: NetworkType,
    val deviceId: String? = null,
    val cellId: String? = null
) {
    /** MEASURED when the modem gave us a value, otherwise UNAVAILABLE. */
    val sinrSource: SinrSource
        get() = if (sinrDb != null) SinrSource.MEASURED else SinrSource.UNAVAILABLE

    /** Whether the modem actually reported RSSI. */
    val rssiSource: SinrSource
        get() = if (rssiDbm != null) SinrSource.MEASURED else SinrSource.UNAVAILABLE

    /**
     * The backend requires only RSRP and RSRQ on every sample.
     *
     * RSSI is deliberately NOT required. 5G NR defines no RSSI at all
     * (CellSignalStrengthNr exposes ssRsrp/ssRsrq/ssSinr only), and many LTE modems
     * return CellInfo.UNAVAILABLE for it -- a Vivo I2302 skipped 211 consecutive
     * valid samples for exactly this reason. When RSSI is absent the backend
     * substitutes the simulated candidate path's value and labels that field
     * `simulated` in field_provenance, so nothing is fabricated.
     */
    val isSendable: Boolean
        get() = rsrpDbm != null && rsrqDb != null

    /** True only when the sample can contribute to a prediction without estimation. */
    val isComplete: Boolean
        get() = isSendable && sinrDb != null
}

/**
 * Request body for `POST /telemetry`, matching the existing M5 schema exactly.
 *
 * The app does not define its own API. If this drifts from the backend, the backend
 * wins — see README "Backend contract".
 */
data class TelemetryRequest(
    val sessionId: String,
    val sample: CellularSample,
    val allowSinrEstimate: Boolean = false
) {
    fun toJson(): String = buildString {
        append('{')
        append("\"session_id\":").append(jsonString(sessionId)).append(',')
        append("\"allow_sinr_estimate\":").append(allowSinrEstimate).append(',')
        append("\"sample\":{")
        append("\"timestamp\":").append(jsonNumber(sample.timestampSeconds)).append(',')
        append("\"rsrp_dbm\":").append(jsonNumber(sample.rsrpDbm)).append(',')
        append("\"rsrq_db\":").append(jsonNumber(sample.rsrqDb)).append(',')
        append("\"rssi_dbm\":").append(jsonNumber(sample.rssiDbm)).append(',')
        // Omitting vs null-ing matters: the backend treats an explicit null as
        // "device did not report SINR", which is exactly what we mean.
        append("\"sinr_db\":").append(jsonNumber(sample.sinrDb)).append(',')
        append("\"network_type\":").append(jsonString(sample.networkType.wire))
        sample.deviceId?.let { append(",\"device_id\":").append(jsonString(it)) }
        sample.cellId?.let { append(",\"cell_id\":").append(jsonString(it)) }
        append('}')
        append('}')
    }

    private fun jsonString(s: String): String {
        val sb = StringBuilder("\"")
        for (c in s) when (c) {
            '"' -> sb.append("\\\"")
            '\\' -> sb.append("\\\\")
            '\n' -> sb.append("\\n")
            '\r' -> sb.append("\\r")
            '\t' -> sb.append("\\t")
            else -> if (c < ' ') sb.append("\\u%04x".format(c.code)) else sb.append(c)
        }
        return sb.append('"').toString()
    }

    /** Non-finite doubles must never reach the wire; they serialise as null. */
    private fun jsonNumber(v: Double?): String =
        if (v == null || !v.isFinite()) "null" else v.toString()
}

/** Outcome of one POST, including failures. Never throws at the call site. */
sealed class SendResult {
    data class Success(val httpStatus: Int, val bufferSize: Int, val windowFull: Boolean,
                       val predictionReady: Boolean, val sinrSource: SinrSource,
                       val warnings: List<String>) : SendResult()
    data class HttpError(val httpStatus: Int, val detail: String) : SendResult()
    data class NetworkError(val message: String) : SendResult()
    data class MalformedResponse(val message: String) : SendResult()
}
