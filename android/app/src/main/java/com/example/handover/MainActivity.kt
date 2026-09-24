package com.example.handover

import android.Manifest
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Single-screen UI for the REAL cellular telemetry demo.
 *
 * Unavailable values render as "N/A". Nothing on this screen is satellite data.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var config: AppConfig
    private lateinit var collector: CellularTelemetryCollector
    private lateinit var client: TelemetryClient

    private val sessionId: String by lazy { AppConfig.newSessionId() }
    private var samplingJob: Job? = null
    private var samplesSent = 0
    private var samplesSkipped = 0

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            render(null, "permission result received")
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        config = AppConfig(this)
        collector = CellularTelemetryCollector(this)
        client = TelemetryClient(config)

        findViewById<EditText>(R.id.urlInput).setText(config.backendBaseUrl)
        findViewById<TextView>(R.id.sessionValue).text = sessionId

        findViewById<Button>(R.id.toggleButton).setOnClickListener {
            if (samplingJob == null) start() else stop()
        }

        requestPermissionsIfNeeded()
        render(null, "idle")
    }

    private fun requestPermissionsIfNeeded() {
        if (collector.permissionState() != CellularTelemetryCollector.PermissionState.GRANTED) {
            permissionLauncher.launch(
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.READ_PHONE_STATE)
            )
        }
    }

    private fun start() {
        config.backendBaseUrl = findViewById<EditText>(R.id.urlInput).text.toString()
        findViewById<Button>(R.id.toggleButton).text = getString(R.string.stop)

        samplingJob = lifecycleScope.launch {
            while (isActive) {
                val sample = collector.collect(config.anonymousDeviceId)
                when {
                    sample == null -> render(null, "no telemetry: permission denied or no telephony")
                    !sample.isSendable -> {
                        samplesSkipped++
                        render(sample, "sample incomplete (RSRP or RSRQ missing) — not sent")
                    }
                    else -> {
                        val result = client.send(
                            TelemetryRequest(sessionId, sample, config.allowSinrEstimate)
                        )
                        if (result is SendResult.Success) samplesSent++
                        render(sample, describe(result))
                    }
                }
                delay(config.samplingIntervalMs)
            }
        }
    }

    private fun stop() {
        samplingJob?.cancel()
        samplingJob = null
        findViewById<Button>(R.id.toggleButton).text = getString(R.string.start)
        render(null, "stopped")
    }

    private fun describe(r: SendResult): String = when (r) {
        is SendResult.Success ->
            "HTTP ${r.httpStatus} · buffer ${r.bufferSize} · " +
                "ready=${r.predictionReady}" +
                (if (r.warnings.isNotEmpty()) " · ${r.warnings.first()}" else "")
        is SendResult.HttpError -> "HTTP ${r.httpStatus}: ${r.detail}"
        is SendResult.NetworkError -> "network error: ${r.message}"
        is SendResult.MalformedResponse -> "bad response: ${r.message}"
    }

    private fun na(v: Double?, unit: String): String =
        if (v == null) "N/A" else String.format(Locale.US, "%.1f %s", v, unit)

    private fun render(sample: CellularSample?, status: String) {
        findViewById<TextView>(R.id.statusValue).text = status
        findViewById<TextView>(R.id.permissionValue).text = when (collector.permissionState()) {
            CellularTelemetryCollector.PermissionState.GRANTED -> "granted"
            CellularTelemetryCollector.PermissionState.DENIED -> "DENIED — values unavailable"
            CellularTelemetryCollector.PermissionState.NO_TELEPHONY -> "no telephony hardware"
        }
        findViewById<TextView>(R.id.networkValue).text = sample?.networkType?.wire ?: "N/A"
        findViewById<TextView>(R.id.rsrpValue).text = na(sample?.rsrpDbm, "dBm")
        findViewById<TextView>(R.id.rsrqValue).text = na(sample?.rsrqDb, "dB")
        findViewById<TextView>(R.id.rssiValue).text = na(sample?.rssiDbm, "dBm")
        findViewById<TextView>(R.id.rssiSourceValue).text =
            sample?.rssiSource?.wire ?: SinrSource.UNAVAILABLE.wire
        findViewById<TextView>(R.id.sinrValue).text = na(sample?.sinrDb, "dB")
        findViewById<TextView>(R.id.sinrSourceValue).text =
            sample?.sinrSource?.wire ?: SinrSource.UNAVAILABLE.wire
        findViewById<TextView>(R.id.timestampValue).text = sample?.let {
            SimpleDateFormat("HH:mm:ss", Locale.US).format(Date((it.timestampSeconds * 1000).toLong()))
        } ?: "N/A"
        findViewById<TextView>(R.id.sentValue).text =
            if (samplesSkipped > 0) "$samplesSent sent · $samplesSkipped skipped" else "$samplesSent"
    }

    override fun onDestroy() {
        samplingJob?.cancel()
        super.onDestroy()
    }
}
