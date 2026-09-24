"""M5 tests: REAL Android cellular telemetry ingestion.

Values here are representative TERRESTRIAL cellular measurements (RSRP -70..-120 dBm,
RSRQ -5..-20 dB, RSSI -50..-110 dBm, SINR -10..30 dB). They are not satellite
measurements and are never described as such.
"""
from __future__ import annotations

import time

import pytest

from app.telemetry import (
    SINR_ESTIMATED,
    SINR_MEASURED,
    SINR_UNAVAILABLE,
    TelemetryError,
    TelemetryStore,
    estimate_sinr_from_rsrq,
    field_provenance,
    normalize_android_sample,
)

NOW = time.time()


def android(**over) -> dict:
    """A realistic LTE sample from a handset."""
    base = {
        "timestamp": NOW,
        "rsrp_dbm": -95.2,
        "rsrq_db": -11.0,
        "rssi_dbm": -68.0,
        "sinr_db": 12.5,
        "network_type": "LTE",
        "device_id": "pixel-test",
    }
    base.update(over)
    return base


# ===========================================================================
# Normalisation
# ===========================================================================
def test_valid_sample_normalizes():
    s, w = normalize_android_sample(android())
    assert s.rsrp_dbm == -95.2 and s.rsrq_db == -11.0 and s.rssi_dbm == -68.0
    assert s.sinr_db == 12.5 and s.sinr_source == SINR_MEASURED
    assert s.network_type == "LTE" and s.has_sinr
    assert w == []


@pytest.mark.parametrize("net,expected", [
    ("lte", "LTE"), ("NR", "NR"), ("gsm", "GSM"), (None, "UNKNOWN"), ("6G", "UNKNOWN"),
])
def test_network_type_normalisation(net, expected):
    s, _ = normalize_android_sample(android(network_type=net))
    assert s.network_type == expected


def test_missing_sinr_is_explicit_not_defaulted():
    """A missing SINR must never become a silent number."""
    raw = android()
    del raw["sinr_db"]
    s, w = normalize_android_sample(raw)
    assert s.sinr_db is None
    assert s.sinr_source == SINR_UNAVAILABLE
    assert not s.has_sinr
    assert any("not prediction-ready" in x for x in w)


def test_missing_sinr_estimate_is_opt_in_and_labelled():
    raw = android()
    del raw["sinr_db"]
    s, w = normalize_android_sample(raw, allow_sinr_estimate=True)
    assert s.sinr_db == pytest.approx(estimate_sinr_from_rsrq(-11.0))
    assert s.sinr_source == SINR_ESTIMATED
    assert any("ESTIMATED" in x for x in w)


def test_android_sentinel_sinr_treated_as_unavailable():
    """Real devices return Integer.MAX_VALUE when SINR is not measurable."""
    s, w = normalize_android_sample(android(sinr_db=2147483647))
    assert s.sinr_db is None and s.sinr_source == SINR_UNAVAILABLE
    assert any("sentinel" in x for x in w)


def test_estimate_is_clamped_to_plausible_range():
    assert -10.0 <= estimate_sinr_from_rsrq(-25.0) <= 30.0
    assert -10.0 <= estimate_sinr_from_rsrq(-3.0) <= 30.0


@pytest.mark.parametrize("field,value", [
    ("rsrp_dbm", -400.0), ("rsrp_dbm", 10.0),
    ("rsrq_db", -80.0), ("rssi_dbm", 5.0), ("sinr_db", 200.0),
])
def test_implausible_values_rejected(field, value):
    with pytest.raises(TelemetryError, match="outside plausible"):
        normalize_android_sample(android(**{field: value}))


# NOTE (M6 real-device finding): rssi_dbm was removed from this list. A Vivo I2302
# reports RSRP/RSRQ/SINR but no RSSI, and 5G NR does not define RSSI at all, so
# requiring it rejected otherwise-valid real measurements. See
# test_missing_rssi_is_accepted_and_marked_unavailable below.
@pytest.mark.parametrize("field", ["timestamp", "rsrp_dbm", "rsrq_db"])
def test_missing_required_field_rejected(field):
    raw = android()
    del raw[field]
    with pytest.raises(TelemetryError, match="missing required"):
        normalize_android_sample(raw)


def test_missing_rssi_is_accepted_and_marked_unavailable():
    """The exact Vivo I2302 observation: RSRP/RSRQ/SINR measured, RSSI absent."""
    raw = {"timestamp": NOW, "rsrp_dbm": -77.0, "rsrq_db": -11.0,
           "sinr_db": 17.0, "network_type": "UNKNOWN"}
    s, w = normalize_android_sample(raw)
    assert s.rsrp_dbm == -77.0 and s.rsrq_db == -11.0 and s.sinr_db == 17.0
    assert s.rssi_dbm is None
    assert s.rssi_source == "unavailable"
    assert s.sinr_source == "measured"      # a real measurement, still measured
    assert s.network_type == "UNKNOWN"      # never guessed
    assert any("RSSI not reported" in x for x in w)


def test_rssi_sentinel_treated_as_unavailable():
    s, w = normalize_android_sample(android(rssi_dbm=2147483647))
    assert s.rssi_dbm is None and s.rssi_source == "unavailable"
    assert any("sentinel" in x for x in w)


def test_missing_rssi_does_not_block_prediction_readiness():
    """RSSI falls back to the simulated path, so it must not gate readiness."""
    store = TelemetryStore(window_size=3)
    for i in range(3):
        raw = {"timestamp": NOW + i, "rsrp_dbm": -77.0, "rsrq_db": -11.0,
               "sinr_db": 17.0, "network_type": "LTE"}
        session = store.ingest("vivo", normalize_android_sample(raw)[0])
    assert session.window_full
    assert session.prediction_ready(), "absent RSSI must not block prediction"


def test_provenance_reports_unmeasured_rssi_as_simulated():
    raw = {"timestamp": NOW, "rsrp_dbm": -77.0, "rsrq_db": -11.0,
           "sinr_db": 17.0, "network_type": "LTE"}
    s, _ = normalize_android_sample(raw)
    prov = field_provenance("LEO-1", s)
    assert prov["rsrp_dbm"] == "real_cellular"
    assert prov["sinr_db"] == "real_cellular"
    assert prov["rssi_dbm"] == "simulated", "must not claim an RSSI measurement"


@pytest.mark.parametrize("ts", [0.0, -5.0])
def test_invalid_timestamp_rejected(ts):
    with pytest.raises(TelemetryError, match="positive finite"):
        normalize_android_sample(android(timestamp=ts))


def test_far_future_timestamp_rejected():
    with pytest.raises(TelemetryError, match="future"):
        normalize_android_sample(android(timestamp=NOW + 5 * 86400))


def test_nan_value_rejected():
    with pytest.raises(TelemetryError, match="finite"):
        normalize_android_sample(android(rsrp_dbm=float("nan")))


# ===========================================================================
# Buffering
# ===========================================================================
def test_store_buffers_and_reports_readiness():
    store = TelemetryStore(window_size=5)
    for i in range(5):
        s, _ = normalize_android_sample(android(timestamp=NOW + i))
        session = store.ingest("dev-1", s)
    assert session.buffer_size == 5 and session.window_full
    assert session.prediction_ready()
    assert session.received == 5


def test_buffer_is_bounded_by_window_size():
    store = TelemetryStore(window_size=3)
    for i in range(10):
        s, _ = normalize_android_sample(android(timestamp=NOW + i))
        session = store.ingest("dev-2", s)
    assert session.buffer_size == 3 and session.received == 10


def test_missing_sinr_blocks_prediction_readiness():
    store = TelemetryStore(window_size=2)
    good, _ = normalize_android_sample(android(timestamp=NOW))
    raw = android(timestamp=NOW + 1)
    del raw["sinr_db"]
    bad, _ = normalize_android_sample(raw)
    store.ingest("dev-3", good)
    session = store.ingest("dev-3", bad)
    assert session.window_full
    assert not session.prediction_ready()


def test_out_of_order_sample_rejected():
    store = TelemetryStore(window_size=5)
    s1, _ = normalize_android_sample(android(timestamp=NOW + 10))
    s2, _ = normalize_android_sample(android(timestamp=NOW))
    store.ingest("dev-4", s1)
    with pytest.raises(TelemetryError, match="out-of-order"):
        store.ingest("dev-4", s2)


def test_sessions_are_isolated():
    store = TelemetryStore(window_size=5)
    s, _ = normalize_android_sample(android())
    store.ingest("a", s)
    assert store.get("b") is None
    assert store.get("a").buffer_size == 1


def test_provenance_marks_real_vs_simulated():
    # `sample` is a required argument: a fully-measured sample is passed here so the
    # four radio fields are legitimately real. See the null-RSSI test below.
    full, _ = normalize_android_sample(android())
    prov = field_provenance("LEO-1", full)
    assert prov["rsrp_dbm"] == "real_cellular"
    assert prov["sinr_db"] == "real_cellular"
    assert prov["latency_ms"] == "simulated"
    assert prov["orbit"] == "simulated"
    assert prov["elevation_deg"] == "simulated"
    # Fully-simulated run: nothing may claim to be real.
    assert set(field_provenance(None, full).values()) == {"simulated"}


# ===========================================================================
# HTTP
# ===========================================================================
class TestTelemetryEndpoints:
    @pytest.mark.anyio
    async def test_valid_telemetry_accepted(self, client):
        r = await client.post("/telemetry",
                              json={"session_id": "http-t1", "sample": android()})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["accepted"] and d["session_id"] == "http-t1"
        assert d["normalized"]["sinr_source"] == "measured"
        assert d["required_window_steps"] == 26
        assert d["buffer_size"] == 1 and not d["window_full"]

    @pytest.mark.anyio
    async def test_session_id_preserved_across_samples(self, client):
        for i in range(3):
            r = await client.post("/telemetry", json={
                "session_id": "http-t2", "sample": android(timestamp=NOW + i)})
            assert r.json()["session_id"] == "http-t2"
        assert r.json()["samples_received"] == 3

    @pytest.mark.anyio
    async def test_missing_sinr_reported_not_faked(self, client):
        raw = android()
        del raw["sinr_db"]
        r = await client.post("/telemetry", json={"session_id": "http-t3", "sample": raw})
        assert r.status_code == 200
        d = r.json()
        assert d["normalized"]["sinr_db"] is None
        assert d["normalized"]["sinr_source"] == "unavailable"
        assert not d["prediction_ready"]
        assert d["warnings"]

    @pytest.mark.anyio
    async def test_sinr_estimate_opt_in(self, client):
        raw = android()
        del raw["sinr_db"]
        r = await client.post("/telemetry", json={
            "session_id": "http-t4", "sample": raw, "allow_sinr_estimate": True})
        d = r.json()
        assert d["normalized"]["sinr_source"] == "estimated_from_rsrq"
        assert d["normalized"]["sinr_db"] is not None

    @pytest.mark.anyio
    async def test_implausible_value_is_4xx_not_500(self, client):
        r = await client.post("/telemetry", json={
            "session_id": "http-t5", "sample": android(rsrp_dbm=-400.0)})
        assert 400 <= r.status_code < 500

    @pytest.mark.anyio
    async def test_malformed_body_rejected(self, client):
        assert (await client.post("/telemetry", json={"session_id": "x"})).status_code == 422
        assert (await client.post("/telemetry", json={})).status_code == 422

    @pytest.mark.anyio
    async def test_out_of_order_returns_400(self, client):
        await client.post("/telemetry", json={
            "session_id": "http-t6", "sample": android(timestamp=NOW + 100)})
        r = await client.post("/telemetry", json={
            "session_id": "http-t6", "sample": android(timestamp=NOW)})
        assert r.status_code == 400
        assert "out-of-order" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_status_endpoint(self, client):
        await client.post("/telemetry", json={"session_id": "http-t7", "sample": android()})
        r = await client.get("/telemetry/http-t7")
        assert r.status_code == 200
        assert r.json()["samples_received"] == 1
        assert (await client.get("/telemetry/nope")).status_code == 404

    @pytest.mark.anyio
    async def test_decision_requires_full_buffer(self, client):
        await client.post("/telemetry", json={"session_id": "http-t8", "sample": android()})
        r = await client.post("/telemetry/decision", json={"session_id": "http-t8"})
        assert r.status_code == 400
        assert "buffered samples" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_telemetry_flows_into_orchestration(self, client):
        """End to end: REAL telemetry -> normalisation -> buffer -> M4 decision."""
        sid = "http-t9"
        for i in range(26):
            await client.post("/telemetry", json={
                "session_id": sid, "sample": android(timestamp=NOW + i)})
        r = await client.post("/telemetry/decision",
                              json={"session_id": sid, "mode": "predictive"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["merged_path"] == "LEO-1"
        assert d["field_provenance"]["rsrp_dbm"] == "real_cellular"
        assert d["field_provenance"]["latency_ms"] == "simulated"
        dec = d["decision"]
        assert dec["decision"] in ("STAY", "SWITCH")
        assert dec["decision_reason"]
        assert dec["model_version"] == "phase1-v1.0.0"
        assert set(dec["risk_probabilities"]) == {"LEO-1", "MEO-1", "GEO-1"}

    @pytest.mark.anyio
    async def test_reactive_decision_uses_no_model(self, client):
        sid = "http-t10"
        for i in range(26):
            await client.post("/telemetry", json={
                "session_id": sid, "sample": android(timestamp=NOW + i)})
        r = await client.post("/telemetry/decision",
                              json={"session_id": sid, "mode": "reactive"})
        assert r.status_code == 200
        assert r.json()["decision"]["risk_probabilities"] is None
        assert r.json()["decision"]["model_version"] is None

    @pytest.mark.anyio
    async def test_decision_blocked_when_sinr_unavailable(self, client):
        sid = "http-t11"
        raw = android()
        del raw["sinr_db"]
        for i in range(26):
            await client.post("/telemetry", json={
                "session_id": sid, "sample": {**raw, "timestamp": NOW + i}})
        r = await client.post("/telemetry/decision", json={"session_id": sid})
        assert r.status_code == 400
        assert "SINR" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_null_rssi_is_never_labelled_real_cellular_end_to_end(self, client):
        """M6 Vivo I2302 regression, through the REAL endpoint.

        The unit test on field_provenance() already passed while the live response
        was still wrong, because the endpoint called it without the sample. This
        exercises the whole path -- ingest -> buffer -> merge -> provenance -- so a
        caller that drops the argument is caught here.
        """
        sid = "vivo-provenance"
        vivo = {"rsrp_dbm": -75.0, "rsrq_db": -11.0, "sinr_db": 25.0,
                "network_type": "UNKNOWN"}          # no rssi_dbm at all
        for i in range(26):
            r = await client.post("/telemetry", json={
                "session_id": sid, "sample": {**vivo, "timestamp": NOW + i}})
            assert r.status_code == 200
        assert r.json()["normalized"]["rssi_dbm"] is None
        assert r.json()["normalized"]["rssi_source"] == "unavailable"
        assert r.json()["prediction_ready"] is True

        resp = await client.post("/telemetry/decision", json={
            "session_id": sid, "mode": "predictive", "merge_real_radio_into": "LEO-1"})
        assert resp.status_code == 200, resp.text
        prov = resp.json()["field_provenance"]

        # The measured fields are real.
        assert prov["rsrp_dbm"] == "real_cellular"
        assert prov["rsrq_db"] == "real_cellular"
        assert prov["sinr_db"] == "real_cellular"
        # The unmeasured one must NOT claim to be a measurement.
        assert prov["rssi_dbm"] != "real_cellular", "null RSSI labelled as measured"
        assert prov["rssi_dbm"] == "simulated"
        # Simulated path context stays simulated.
        assert prov["latency_ms"] == "simulated"
        assert prov["orbit"] == "simulated"

    @pytest.mark.anyio
    async def test_merged_path_is_not_the_selected_path(self, client):
        """`merged_path` names where REAL radio was overlaid, not what the engine chose."""
        sid = "vivo-merged-semantics"
        vivo = {"rsrp_dbm": -75.0, "rsrq_db": -11.0, "sinr_db": 25.0, "network_type": "UNKNOWN"}
        for i in range(26):
            await client.post("/telemetry", json={
                "session_id": sid, "sample": {**vivo, "timestamp": NOW + i}})
        body = (await client.post("/telemetry/decision", json={
            "session_id": sid, "mode": "predictive",
            "merge_real_radio_into": "LEO-1"})).json()
        assert body["merged_path"] == "LEO-1"
        # The engine is free to select any candidate; the two are independent.
        assert body["decision"]["selected_path"] in {"LEO-1", "MEO-1", "GEO-1"}

    @pytest.mark.anyio
    async def test_fully_simulated_run_claims_nothing_as_real(self, client):
        sid = "vivo-nomerge"
        vivo = {"rsrp_dbm": -75.0, "rsrq_db": -11.0, "sinr_db": 25.0, "network_type": "UNKNOWN"}
        for i in range(26):
            await client.post("/telemetry", json={
                "session_id": sid, "sample": {**vivo, "timestamp": NOW + i}})
        body = (await client.post("/telemetry/decision", json={
            "session_id": sid, "mode": "predictive",
            "merge_real_radio_into": None})).json()
        assert body["merged_path"] is None
        assert set(body["field_provenance"].values()) == {"simulated"}

    @pytest.mark.anyio
    async def test_unknown_merge_path_rejected(self, client):
        sid = "http-t12"
        for i in range(26):
            await client.post("/telemetry", json={
                "session_id": sid, "sample": android(timestamp=NOW + i)})
        r = await client.post("/telemetry/decision", json={
            "session_id": sid, "merge_real_radio_into": "STARLINK-9"})
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_existing_endpoints_unchanged(self, client):
        assert (await client.get("/health")).json()["status"] == "ok"
        assert (await client.get("/model/info")).status_code == 200
        assert (await client.get("/docs")).status_code == 200
        schema = (await client.get("/openapi.json")).json()
        for p in ("/health", "/model/info", "/predict", "/decision",
                  "/telemetry", "/telemetry/decision"):
            assert p in schema["paths"]
