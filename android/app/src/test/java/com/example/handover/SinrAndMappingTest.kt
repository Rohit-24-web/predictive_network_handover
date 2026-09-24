package com.example.handover

import org.junit.Assert.*
import org.junit.Test

class SinrAndMappingTest {

    @Test
    fun `android unavailable sentinels map to null`() {
        assertNull(AndroidSentinels.sanitize(Int.MAX_VALUE))
        assertNull(AndroidSentinels.sanitize(Int.MIN_VALUE))
        assertNull(AndroidSentinels.sanitize(null))
        assertEquals(12, AndroidSentinels.sanitize(12))
        assertEquals(-97, AndroidSentinels.sanitize(-97))
    }

    @Test
    fun `sentinel SINR never becomes a measurement`() {
        val sinr = CellularTelemetryCollector.sanitizeDouble(Int.MAX_VALUE)
        assertNull(sinr)
        val s = CellularSample(1.0, -99.0, -11.0, -68.0, sinr, NetworkType.LTE)
        assertEquals(SinrSource.UNAVAILABLE, s.sinrSource)
        assertTrue(TelemetryRequest("s", s).toJson().contains("\"sinr_db\":null"))
    }

    @Test
    fun `implausible values are rejected rather than sent`() {
        assertNull(CellularRanges.plausible(-400.0, CellularRanges.RSRP_DBM))
        assertNull(CellularRanges.plausible(10.0, CellularRanges.RSRP_DBM))
        assertNull(CellularRanges.plausible(Double.NaN, CellularRanges.SINR_DB))
        // Explicit delta: JUnit4's assertEquals(double, double) is deprecated and
        // fails unconditionally, so never let overload resolution decide this.
        assertEquals(-99.7, CellularRanges.plausible(-99.7, CellularRanges.RSRP_DBM)!!, 1e-9)
        assertEquals(14.1, CellularRanges.plausible(14.1, CellularRanges.SINR_DB)!!, 1e-9)
    }

    @Test
    fun `network types map to the backend vocabulary`() {
        assertEquals(NetworkType.NR, NetworkType.fromAndroidNetworkType(20))
        assertEquals(NetworkType.LTE, NetworkType.fromAndroidNetworkType(13))
        assertEquals(NetworkType.UMTS, NetworkType.fromAndroidNetworkType(3))
        assertEquals(NetworkType.GSM, NetworkType.fromAndroidNetworkType(1))
        assertEquals(NetworkType.UNKNOWN, NetworkType.fromAndroidNetworkType(0))
        assertEquals(NetworkType.UNKNOWN, NetworkType.fromAndroidNetworkType(999))
    }

    @Test
    fun `sinr source round trips through the wire vocabulary`() {
        assertEquals(SinrSource.MEASURED, SinrSource.fromWire("measured"))
        assertEquals(SinrSource.UNAVAILABLE, SinrSource.fromWire("unavailable"))
        assertEquals(SinrSource.ESTIMATED, SinrSource.fromWire("estimated"))
        assertEquals(SinrSource.UNAVAILABLE, SinrSource.fromWire(null))
        assertEquals(SinrSource.UNAVAILABLE, SinrSource.fromWire("nonsense"))
    }

    @Test
    fun `timestamp is seconds not milliseconds`() {
        val t = CellularTelemetryCollector.nowSeconds()
        assertTrue("expected Unix seconds", t > 1_600_000_000.0 && t < 4_000_000_000.0)
    }
}

/**
 * M6 real-device regression tests.
 *
 * Written against the exact observation from a physical Vivo I2302:
 * RSRP -77.0 dBm, RSRQ -11.0 dB, SINR 17.0 dB measured, RSSI unavailable,
 * network type UNKNOWN. Before the fix the app skipped 211 consecutive samples.
 *
 * NOT EXECUTED in the authoring environment (no Kotlin compiler / Android SDK).
 */
class VivoRealDeviceRegressionTest {

    private fun vivo(rssi: Double? = null, sinr: Double? = 17.0) = CellularSample(
        timestampSeconds = 1789110484.0,
        rsrpDbm = -77.0, rsrqDb = -11.0, rssiDbm = rssi, sinrDb = sinr,
        networkType = NetworkType.UNKNOWN, deviceId = "android-vivo1234"
    )

    // A. complete measured sample is sendable
    @Test
    fun `fully measured sample is sendable and complete`() {
        val s = vivo(rssi = -68.0)
        assertTrue(s.isSendable)
        assertTrue(s.isComplete)
    }

    // B. RSSI unavailable must NOT block sending
    @Test
    fun `vivo sample without rssi is still sendable`() {
        val s = vivo()
        assertTrue("absent RSSI must not skip a real measurement", s.isSendable)
        assertNull(s.rssiDbm)
        assertEquals(SinrSource.UNAVAILABLE, s.rssiSource)
    }

    @Test
    fun `absent rssi serialises as null and is never derived from rsrp`() {
        val json = TelemetryRequest("vivo", vivo()).toJson()
        assertTrue(json.contains("\"rssi_dbm\":null"))
        assertFalse(json.contains("\"rssi_dbm\":-77"))
        // real measurements survive intact
        assertTrue(json.contains("\"rsrp_dbm\":-77.0"))
        assertTrue(json.contains("\"rsrq_db\":-11.0"))
        assertTrue(json.contains("\"sinr_db\":17.0"))
    }

    // C. SINR unavailable stays unavailable, never estimated on-device
    @Test
    fun `absent sinr stays unavailable and is not estimated locally`() {
        val s = vivo(sinr = null)
        assertTrue("missing SINR still sends", s.isSendable)
        assertFalse("but it is not complete", s.isComplete)
        assertEquals(SinrSource.UNAVAILABLE, s.sinrSource)
        assertTrue(TelemetryRequest("vivo", s).toJson().contains("\"sinr_db\":null"))
    }

    // D. sentinels
    @Test
    fun `rssi sentinel becomes unavailable rather than a radio value`() {
        val rssi = CellularTelemetryCollector.sanitizeDouble(Int.MAX_VALUE)
        assertNull(rssi)
        assertTrue(vivo(rssi = rssi).isSendable)
    }

    // E. unknown network type is explicit, never guessed
    @Test
    fun `unknown network type is sent explicitly`() {
        assertTrue(TelemetryRequest("vivo", vivo()).toJson()
            .contains("\"network_type\":\"UNKNOWN\""))
    }

    // F. genuinely required fields still gate sending
    @Test
    fun `missing rsrp or rsrq still blocks sending`() {
        assertFalse(vivo().copy(rsrpDbm = null).isSendable)
        assertFalse(vivo().copy(rsrqDb = null).isSendable)
    }

    @Test
    fun `out of range values are filtered to null by the range guard`() {
        assertNull(CellularRanges.plausible(-400.0, CellularRanges.RSRP_DBM))
        assertNull(CellularRanges.plausible(99.0, CellularRanges.SINR_DB))
    }
}
