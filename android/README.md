# Android Live Cellular Telemetry (M6)

A minimal Kotlin app that reads **real terrestrial cellular measurements** from the
handset's modem and posts them to the existing FastAPI `/telemetry` endpoint (M5).

> **Cellular measurements shown by this app are real terrestrial cellular telemetry.
> LEO/MEO/GEO candidate conditions used by the backend remain simulated.**

The app is a **telemetry producer only**. It does not run the model, does not do
feature engineering, and cannot command a carrier handover — Android exposes no such
API to a normal application.

---

## Requirements

| Item | Value |
|---|---|
| Language | Kotlin 1.9.24 |
| Gradle plugin | Android Gradle Plugin 8.5.2 |
| `minSdk` | **29** (Android 10) |
| `targetSdk` / `compileSdk` | 34 |
| JDK | 17 |

`minSdk 29` is deliberate: `TelephonyManager.getSignalStrength()` and
`CellSignalStrengthLte.getRssi()` both require API 29. On older releases several
priority fields do not exist at all, and this project refuses to fabricate values it
cannot measure.

## Permissions

| Permission | Why |
|---|---|
| `INTERNET`, `ACCESS_NETWORK_STATE` | reach the backend |
| `ACCESS_FINE_LOCATION` | **required by Android** to read cell signal detail |
| `READ_PHONE_STATE` | network type on some devices |

Requested at runtime. If denied, the app shows `Permission: DENIED — values
unavailable`, displays `N/A`, sends nothing, and **does not crash**.

No IMEI, IMSI, phone number or contacts are read. `device_id` is a locally generated
anonymous UUID stored in SharedPreferences.

## Telemetry collected

| Field | Source | May be unavailable? |
|---|---|---|
| `timestamp` | `System.currentTimeMillis()/1000.0` | no |
| `rsrp_dbm` | `CellSignalStrengthLte.rsrp` / `Nr.ssRsrp` | yes (2G/3G only) |
| `rsrq_db` | `CellSignalStrengthLte.rsrq` / `Nr.ssRsrq` | yes (2G/3G only) |
| `rssi_dbm` | `CellSignalStrengthLte.rssi` (API 29+) / `dbm` | yes |
| `sinr_db` | `CellSignalStrengthLte.rssnr` / `Nr.ssSinr` | **often** |
| `network_type` | `TelephonyManager.dataNetworkType` | falls back to `UNKNOWN` |

Availability varies by Android version, OEM, modem and carrier. **SINR is the field
most commonly withheld.**

## SINR handling — three explicit states

| State | Meaning | Wire |
|---|---|---|
| `measured` | modem reported it | `"sinr_db": 14.1` |
| `unavailable` | device gave nothing, or a sentinel | `"sinr_db": null` |
| `estimated` | backend derived it from RSRQ, opt-in | backend sets `estimated_from_rsrq` |

Android's `CellInfo.UNAVAILABLE` (`Integer.MAX_VALUE`) and `Integer.MIN_VALUE` are
mapped to `null`. A missing SINR is **never** turned into 0 or any other default.

The app never estimates locally. Estimation is the backend's documented mechanism
(`allow_sinr_estimate`); duplicating it here would risk the two drifting apart.

Without SINR the backend reports `prediction_ready: false`, and
`/telemetry/decision` returns **400**. That is intended: an incomplete window must
not yield a confident-looking prediction.

## Configuring the backend URL

Set it in the URL field on the main screen. It is persisted in SharedPreferences and
read only from `AppConfig.backendBaseUrl` — there is no hardcoded URL elsewhere.

- Emulator → `http://10.0.2.2:8000` (host loopback) — the default
- Physical device → `http://<your-computer-LAN-IP>:8000`

Sampling interval defaults to **1000 ms** (floor 500 ms).

## Build

```bash
# 1. Start the backend on all interfaces so a phone can reach it
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 2. Generate the Gradle wrapper jar ONCE (it is a binary and is not checked in).
#    Android Studio does this automatically when you open the project.
cd android
gradle wrapper --gradle-version 8.7     # skip if opening in Android Studio

# 3. Build the app
./gradlew :app:assembleDebug          # APK at app/build/outputs/apk/debug/
./gradlew :app:installDebug           # build + install to a connected device
./gradlew :app:testDebugUnitTest      # JVM unit tests, no device needed
```

Or open `android/` in Android Studio (Hedgehog or newer) and press Run.

## Physical device verification

1. Install the APK on a real phone with a SIM (an emulator has no modem, so
   RSRP/RSRQ/SINR will read `N/A` — that is correct behaviour, not a bug).
2. Grant the location permission when prompted.
3. Set the backend URL to your computer's LAN IP; phone and computer on the same network.
4. Press **Start telemetry**.
5. Confirm real RSRP/RSRQ/RSSI appear, and note whether SINR shows a value or `N/A`.
6. Watch `Samples sent` increase and `Status` show `HTTP 200 · buffer N`.
7. Check the backend: `curl http://localhost:8000/telemetry/<session_id>`.
8. After 26 valid samples, confirm `prediction_ready: true` (requires SINR on every
   buffered sample).
9. Run a decision:
   `curl -X POST http://localhost:8000/telemetry/decision -H 'Content-Type: application/json' -d '{"session_id":"<id>","mode":"predictive"}'`

If your device withholds SINR, step 8 will report `prediction_ready: false`. Either
accept it as an honest limitation or enable the labelled backend estimate.

## Backend contract

The app matches the **existing M5 schema**; it does not define its own API. If this
app and the backend ever disagree, the backend wins.

```json
{
  "session_id": "android-demo-001",
  "allow_sinr_estimate": false,
  "sample": {
    "timestamp": 1789092774.16,
    "rsrp_dbm": -99.7,
    "rsrq_db": -11.0,
    "rssi_dbm": -68.0,
    "sinr_db": 14.1,
    "network_type": "LTE",
    "device_id": "android-abc12345"
  }
}
```

Plausibility ranges mirrored from the backend (which re-validates and stays
authoritative): RSRP −140..−40 dBm, RSRQ −25..−3 dB, RSSI −120..−30 dBm,
SINR −20..40 dB. Out-of-range readings are shown as `N/A` and not sent.

## Real vs simulated

| REAL | SIMULATED |
|---|---|
| Cellular RSRP, RSRQ, RSSI, SINR, network type, timestamps | LEO/MEO/GEO candidate paths |
| HTTP transport, buffering, model inference, engine decisions | Orbital geometry, latency, jitter, loss, throughput, load |

The backend returns per-field `field_provenance` so a UI can never present simulated
data as measured.

**Limitation worth stating plainly:** real terrestrial RSRP/RSRQ distributions differ
from the simulated distribution the model was trained on. A merged-path prediction
shows the pipeline working end to end — it is not a validated satellite-link forecast.

## Tests

`app/src/test/` contains pure-JVM tests (no device, no emulator, no Robolectric):

- `TelemetrySerializationTest` — wire format, null SINR, NaN handling, escaping, sendability
- `SinrAndMappingTest` — sentinel mapping, plausibility, network-type mapping, timestamp units
- `TelemetryClientTest` — URL building, response parsing, error extraction

Run with `./gradlew :app:testDebugUnitTest`.

These verify **serialisation and state mapping only**. No test claims that Android
hardware produced a real RSRP or SINR; that requires the physical procedure above.
