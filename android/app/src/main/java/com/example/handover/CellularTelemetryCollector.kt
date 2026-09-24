package com.example.handover

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.telephony.CellSignalStrengthGsm
import android.telephony.CellSignalStrengthLte
import android.telephony.CellSignalStrengthNr
import android.telephony.CellSignalStrengthWcdma
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat

/**
 * Reads REAL cellular measurements from the modem via TelephonyManager.
 *
 * WHAT THIS IS NOT
 * ----------------
 * This observes terrestrial cellular signal quality. It does not measure a
 * satellite link, and it cannot command a carrier handover — Android exposes no
 * such API to a normal app. The backend's LEO/MEO/GEO candidates are simulated.
 *
 * DEVICE VARIABILITY IS THE NORM
 * ------------------------------
 * Field availability varies by Android version, OEM, modem and carrier. Anything
 * the device does not report stays null all the way to the wire. There is no
 * default, no zero, and no local estimate.
 */
class CellularTelemetryCollector(private val context: Context) {

    enum class PermissionState { GRANTED, DENIED, NO_TELEPHONY }

    fun permissionState(): PermissionState {
        val tm = context.getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager
            ?: return PermissionState.NO_TELEPHONY
        if (tm.phoneType == TelephonyManager.PHONE_TYPE_NONE) return PermissionState.NO_TELEPHONY
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        return if (granted) PermissionState.GRANTED else PermissionState.DENIED
    }

    /**
     * Collect one sample, or null when permission is denied / no telephony.
     *
     * Returning null (rather than a sample of nulls) keeps "we were not allowed to
     * look" distinct from "we looked and the modem had nothing".
     */
    fun collect(deviceId: String?): CellularSample? {
        if (permissionState() != PermissionState.GRANTED) return null
        val tm = context.getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager
            ?: return null

        var rsrp: Double? = null
        var rsrq: Double? = null
        var rssi: Double? = null
        var sinr: Double? = null

        try {
            // getSignalStrength() requires API 29, which is this app's minSdk.
            val strengths = tm.signalStrength?.cellSignalStrengths ?: emptyList()

            for (s in strengths) {
                when {
                    // 5G NR. ssRsrp/ssRsrq/ssSinr exist from API 29 but are widely
                    // unreported in practice; sentinels are mapped to null below.
                    // 5G NR defines no RSSI, so `rssi` stays null on an NR-only
                    // serving cell. That is correct, not a gap to be filled.
                    s is CellSignalStrengthNr -> {
                        rsrp = rsrp ?: sanitizeDouble(s.ssRsrp)
                        rsrq = rsrq ?: sanitizeDouble(s.ssRsrq)
                        sinr = sinr ?: sanitizeDouble(s.ssSinr)
                    }
                    s is CellSignalStrengthLte -> {
                        rsrp = rsrp ?: sanitizeDouble(s.rsrp)
                        rsrq = rsrq ?: sanitizeDouble(s.rsrq)
                        rssi = rssi ?: sanitizeDouble(s.rssi)   // API 29+
                        // rssnr is the LTE SINR analogue and is the single field
                        // most often withheld by OEM modems.
                        sinr = sinr ?: sanitizeDouble(s.rssnr)
                    }
                    s is CellSignalStrengthWcdma || s is CellSignalStrengthGsm -> {
                        // 2G/3G expose dBm only. RSRP/RSRQ/SINR are LTE/NR concepts
                        // and genuinely do not exist here, so they stay null.
                        rssi = rssi ?: sanitizeDouble(s.dbm)
                    }
                }
            }
        } catch (e: SecurityException) {
            return null                     // permission revoked mid-session
        } catch (e: Exception) {
            return null                     // OEM quirk; never crash the sampler
        }

        val networkType = try {
            NetworkType.fromAndroidNetworkType(tm.dataNetworkType)
        } catch (e: SecurityException) {
            NetworkType.UNKNOWN
        }

        return CellularSample(
            timestampSeconds = nowSeconds(),
            rsrpDbm = CellularRanges.plausible(rsrp, CellularRanges.RSRP_DBM),
            rsrqDb = CellularRanges.plausible(rsrq, CellularRanges.RSRQ_DB),
            rssiDbm = CellularRanges.plausible(rssi, CellularRanges.RSSI_DBM),
            sinrDb = CellularRanges.plausible(sinr, CellularRanges.SINR_DB),
            networkType = networkType,
            deviceId = deviceId,
            cellId = null                   // omitted: not needed by the backend contract
        )
    }

    companion object {
        /** Seconds with millisecond resolution — the representation M5 expects. */
        fun nowSeconds(): Double = System.currentTimeMillis() / 1000.0

        /** Map Android's "unavailable" sentinels to null before widening to Double. */
        fun sanitizeDouble(raw: Int?): Double? =
            AndroidSentinels.sanitize(raw)?.toDouble()
    }
}
