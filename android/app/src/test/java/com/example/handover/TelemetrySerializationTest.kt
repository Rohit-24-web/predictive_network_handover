package com.example.handover

import org.junit.Assert.*
import org.junit.Test

/**
 * Pure-JVM tests for the wire format. No device, no emulator, no Robolectric.
 *
 * NOTE ON HONESTY: these tests verify SERIALISATION and STATE MAPPING only. They do
 * not and cannot demonstrate that a modem produced a real RSRP or SINR — that
 * requires physical hardware (see README, "Physical device verification").
 */
class TelemetrySerializationTest {

    private fun sample(
        sinr: Double? = 14.1,
        rsrp: Double? = -99.7,
        rsrq: Double? = -11.0,
        rssi: Double? = -68.0
    ) = CellularSample(
        timestampSeconds = 1789092774.16,
        rsrpDbm = rsrp, rsrqDb = rsrq, rssiDbm = rssi, sinrDb = sinr,
        networkType = NetworkType.LTE, deviceId = "android-abc12345"
    )

    @Test
    fun `valid sample serialises to the M5 contract`() {
        val json = TelemetryRequest("android-demo-001", sample()).toJson()
        assertTrue(json.contains("\"session_id\":\"android-demo-001\""))
        assertTrue(json.contains("\"timestamp\":1.78909277416E9") ||
                   json.contains("\"timestamp\":1789092774.16"))
        assertTrue(json.contains("\"rsrp_dbm\":-99.7"))
        assertTrue(json.contains("\"rsrq_db\":-11.0"))
        assertTrue(json.contains("\"rssi_dbm\":-68.0"))
        assertTrue(json.contains("\"sinr_db\":14.1"))
        assertTrue(json.contains("\"network_type\":\"LTE\""))
        assertTrue(json.contains("\"device_id\":\"android-abc12345\""))
    }

    @Test
    fun `missing SINR serialises as explicit null, never zero`() {
        val json = TelemetryRequest("s", sample(sinr = null)).toJson()
        assertTrue("SINR must be explicit null", json.contains("\"sinr_db\":null"))
        assertFalse("SINR must never be defaulted to 0", json.contains("\"sinr_db\":0"))
    }

    @Test
    fun `sinr source reflects availability`() {
        assertEquals(SinrSource.MEASURED, sample(sinr = 12.0).sinrSource)
        assertEquals(SinrSource.UNAVAILABLE, sample(sinr = null).sinrSource)
    }

    @Test
    fun `non finite values never reach the wire`() {
        val json = TelemetryRequest("s", sample(sinr = Double.NaN)).toJson()
        assertTrue(json.contains("\"sinr_db\":null"))
        assertFalse(json.contains("NaN"))
    }

    @Test
    fun `allow_sinr_estimate defaults to false and is explicit`() {
        assertTrue(TelemetryRequest("s", sample()).toJson()
            .contains("\"allow_sinr_estimate\":false"))
        assertTrue(TelemetryRequest("s", sample(), allowSinrEstimate = true).toJson()
            .contains("\"allow_sinr_estimate\":true"))
    }

    @Test
    fun `optional identifiers are omitted when absent`() {
        val s = sample().copy(deviceId = null, cellId = null)
        val json = TelemetryRequest("s", s).toJson()
        assertFalse(json.contains("device_id"))
        assertFalse(json.contains("cell_id"))
    }

    @Test
    fun `session id is escaped rather than breaking the json`() {
        val json = TelemetryRequest("we\"ird\\id", sample()).toJson()
        assertTrue(json.contains("\\\"ird"))
    }

    @Test
    fun `sample without rsrp is not sendable`() {
        assertFalse(sample(rsrp = null).isSendable)
        assertFalse(sample(rsrq = null).isSendable)
        assertFalse(sample(rssi = null).isSendable)
        assertTrue(sample().isSendable)
    }

    @Test
    fun `completeness requires sinr as well`() {
        assertTrue(sample().isComplete)
        assertFalse("missing SINR cannot be complete", sample(sinr = null).isComplete)
    }
}
