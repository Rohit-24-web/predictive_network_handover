package com.example.handover

import org.junit.Assert.*
import org.junit.Test

class TelemetryClientTest {

    @Test
    fun `url building never doubles or drops a slash`() {
        assertEquals("http://10.0.2.2:8000/telemetry",
            TelemetryClient.buildUrl("http://10.0.2.2:8000", "/telemetry"))
        assertEquals("http://10.0.2.2:8000/telemetry",
            TelemetryClient.buildUrl("http://10.0.2.2:8000/", "/telemetry"))
        assertEquals("https://host/api/telemetry",
            TelemetryClient.buildUrl("https://host/api", "telemetry"))
    }

    @Test
    fun `success fields are parsed from a real M5 response`() {
        val body = """
            {"session_id":"android-demo-001","accepted":true,"samples_received":3,
             "buffer_size":3,"required_window_steps":26,"window_full":false,
             "prediction_ready":false,
             "normalized":{"sinr_db":14.1,"sinr_source":"measured","network_type":"LTE"},
             "warnings":[]}
        """.trimIndent()
        assertEquals(3, TelemetryClient.intField(body, "buffer_size"))
        assertEquals(false, TelemetryClient.boolField(body, "window_full"))
        assertEquals(false, TelemetryClient.boolField(body, "prediction_ready"))
        assertEquals("measured", TelemetryClient.stringField(body, "sinr_source"))
        assertTrue(TelemetryClient.stringArray(body, "warnings").isEmpty())
    }

    @Test
    fun `warnings array is parsed`() {
        val body = """{"warnings":["SINR not reported by device; sample is not prediction-ready."]}"""
        val w = TelemetryClient.stringArray(body, "warnings")
        assertEquals(1, w.size)
        assertTrue(w[0].contains("not prediction-ready"))
    }

    @Test
    fun `error detail is extracted from a 400 body`() {
        val body = """{"detail":"rsrp_dbm=-400.0 outside plausible cellular range [-140.0, -40.0]"}"""
        assertTrue(TelemetryClient.extractDetail(body)!!.contains("outside plausible"))
    }

    @Test
    fun `malformed body yields nulls rather than throwing`() {
        assertNull(TelemetryClient.intField("not json", "buffer_size"))
        assertNull(TelemetryClient.stringField("", "sinr_source"))
        assertTrue(TelemetryClient.stringArray("{}", "warnings").isEmpty())
    }
}
