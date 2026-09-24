package com.example.handover

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL

/**
 * Minimal HTTP client for `POST /telemetry`.
 *
 * Uses HttpURLConnection rather than OkHttp/Retrofit: one endpoint and one JSON
 * body does not justify a networking dependency, and the platform client keeps the
 * APK small.
 *
 * Every failure mode returns a [SendResult]; nothing throws to the caller, so a
 * dropped connection or a 500 never crashes the app.
 */
class TelemetryClient(private val config: AppConfig) {

    fun telemetryUrl(): String = buildUrl(config.backendBaseUrl, PATH_TELEMETRY)

    suspend fun send(request: TelemetryRequest): SendResult = withContext(Dispatchers.IO) {
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(telemetryUrl()).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = CONNECT_TIMEOUT_MS
                readTimeout = READ_TIMEOUT_MS
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
                setRequestProperty("Accept", "application/json")
            }
            conn.outputStream.use { it.write(request.toJson().toByteArray(Charsets.UTF_8)) }

            val status = conn.responseCode
            val body = readBody(conn, status)

            if (status in 200..299) parseSuccess(status, body)
            else SendResult.HttpError(status, extractDetail(body) ?: "HTTP $status")
        } catch (e: java.net.SocketTimeoutException) {
            SendResult.NetworkError("timeout contacting backend: ${e.message ?: "timed out"}")
        } catch (e: java.io.IOException) {
            SendResult.NetworkError("cannot reach backend: ${e.message ?: e::class.java.simpleName}")
        } catch (e: Exception) {
            SendResult.NetworkError("unexpected error: ${e.message ?: e::class.java.simpleName}")
        } finally {
            conn?.disconnect()
        }
    }

    private fun readBody(conn: HttpURLConnection, status: Int): String = try {
        val stream = if (status in 200..299) conn.inputStream else conn.errorStream
        stream?.bufferedReader()?.use(BufferedReader::readText) ?: ""
    } catch (e: Exception) {
        ""
    }

    /**
     * Tolerant extraction: a malformed or unexpected body yields MalformedResponse
     * rather than an exception, because a backend change must not crash the phone.
     */
    private fun parseSuccess(status: Int, body: String): SendResult = try {
        SendResult.Success(
            httpStatus = status,
            bufferSize = intField(body, "buffer_size") ?: -1,
            windowFull = boolField(body, "window_full") ?: false,
            predictionReady = boolField(body, "prediction_ready") ?: false,
            sinrSource = SinrSource.fromWire(stringField(body, "sinr_source")),
            warnings = stringArray(body, "warnings")
        )
    } catch (e: Exception) {
        SendResult.MalformedResponse("could not parse backend response: ${e.message}")
    }

    companion object {
        const val PATH_TELEMETRY = "/telemetry"
        const val CONNECT_TIMEOUT_MS = 4_000
        const val READ_TIMEOUT_MS = 6_000

        /** Join base and path without producing `//` or dropping a segment. */
        fun buildUrl(base: String, path: String): String =
            base.trimEnd('/') + "/" + path.trimStart('/')

        // --- deliberately tiny field readers -------------------------------
        // Only a handful of scalars are needed for the status UI, so the app
        // avoids a JSON dependency. These are pure functions and unit-tested.
        fun intField(json: String, key: String): Int? =
            Regex("\"$key\"\\s*:\\s*(-?\\d+)").find(json)?.groupValues?.get(1)?.toIntOrNull()

        fun boolField(json: String, key: String): Boolean? =
            Regex("\"$key\"\\s*:\\s*(true|false)").find(json)?.groupValues?.get(1)?.toBooleanStrictOrNull()

        fun stringField(json: String, key: String): String? =
            Regex("\"$key\"\\s*:\\s*\"([^\"]*)\"").find(json)?.groupValues?.get(1)

        fun stringArray(json: String, key: String): List<String> {
            val block = Regex("\"$key\"\\s*:\\s*\\[(.*?)]", RegexOption.DOT_MATCHES_ALL)
                .find(json)?.groupValues?.get(1) ?: return emptyList()
            return Regex("\"((?:[^\"\\\\]|\\\\.)*)\"").findAll(block)
                .map { it.groupValues[1] }.toList()
        }

        fun extractDetail(body: String): String? = stringField(body, "detail")
    }
}
