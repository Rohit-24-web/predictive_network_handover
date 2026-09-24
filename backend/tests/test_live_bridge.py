"""M8 live bridge: real handset -> HTTP /telemetry -> observer socket -> dashboard.

The Vivo values used here (RSRP -75.0, RSRQ -11.0, SINR 25.0, no RSSI, network
UNKNOWN) are the ones reported by the physical device, so provenance assertions
reflect a real handset rather than a convenient fixture.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.events import SessionHub
from app.main import app, _state

WINDOW = 26
NOW = time.time()
VIVO = {"rsrp_dbm": -75.0, "rsrq_db": -11.0, "sinr_db": 25.0, "network_type": "UNKNOWN"}


def vivo_sample(i: int, **over) -> dict:
    return {**VIVO, "timestamp": NOW + i, **over}


@pytest.fixture(scope="module")
def sync_client():
    with TestClient(app) as c:
        yield c


def post_samples(client, sid: str, n: int, **over):
    last = None
    for i in range(n):
        last = client.post("/telemetry", json={"session_id": sid, "sample": vivo_sample(i, **over)})
        assert last.status_code == 200, last.text
    return last


# --- hub unit behaviour ----------------------------------------------------
def test_publish_is_a_noop_without_observers():
    """With no dashboard attached the backend must behave exactly as M5/M7 did."""
    hub = SessionHub()
    assert hub.publish("nobody", {"type": "telemetry_ack"}) == 0
    assert hub.observer_count() == 0


def test_hub_drops_oldest_when_a_slow_observer_falls_behind():
    hub = SessionHub(queue_size=3)
    q = hub.subscribe("s")
    for i in range(10):
        hub.publish("s", {"i": i})
    assert q.qsize() == 3
    assert q.get_nowait()["i"] == 7          # oldest dropped, newest retained


def test_unsubscribe_removes_the_session_entry():
    hub = SessionHub()
    q = hub.subscribe("s")
    hub.unsubscribe("s", q)
    assert hub.observer_count("s") == 0
    assert "s" not in hub.sessions_with_observers()


# --- session discovery -----------------------------------------------------
def test_sessions_endpoint_lists_the_producer(sync_client):
    sid = "android-discovery"
    post_samples(sync_client, sid, 3)
    body = sync_client.get("/sessions").json()
    entry = next(s for s in body["sessions"] if s["session_id"] == sid)
    assert entry["samples_received"] == 3
    assert entry["required_window_steps"] == WINDOW
    assert entry["network_type"] == "UNKNOWN"
    assert entry["sinr_source"] == "measured"


def test_dashboard_finds_the_phone_without_a_shared_id(sync_client):
    """The handset picks its own id; the dashboard discovers it. No agreement needed."""
    sid = "android-6567b8f8-b1a"
    post_samples(sync_client, sid, 2)
    ids = [s["session_id"] for s in sync_client.get("/sessions").json()["sessions"]]
    assert sid in ids


# --- the bridge itself -----------------------------------------------------
def test_observer_receives_real_telemetry_posted_over_http(sync_client):
    sid = "android-bridge-1"
    with sync_client.websocket_connect(f"/ws/observe/{sid}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected" and hello["role"] == "observer"

        sync_client.post("/telemetry", json={"session_id": sid, "sample": vivo_sample(0)})

        ack = ws.receive_json()
        assert ack["type"] == "telemetry_ack"
        # The observer never sent the sample, so the frame must carry the values.
        assert ack["normalized"]["rsrp_dbm"] == -75.0
        assert ack["normalized"]["rsrq_db"] == -11.0
        assert ack["normalized"]["sinr_db"] == 25.0
        assert ack["normalized"]["sinr_source"] == "measured"
        assert ack["normalized"]["rssi_dbm"] is None
        assert ack["normalized"]["rssi_source"] == "unavailable"
        assert ws.receive_json()["type"] == "status"


def test_observer_gets_a_decision_once_the_window_fills(sync_client):
    sid = "android-bridge-2"
    post_samples(sync_client, sid, WINDOW - 1)
    with sync_client.websocket_connect(f"/ws/observe/{sid}") as ws:
        ws.receive_json()                     # connected
        sync_client.post("/telemetry", json={"session_id": sid, "sample": vivo_sample(WINDOW)})
        ack = ws.receive_json()
        assert ack["prediction_ready"] is True
        frame = ws.receive_json()
        assert frame["type"] == "decision"
        d = frame["decision"]
        assert d["model_version"] == "phase1-v1.0.0"
        assert d["prediction_horizon_seconds"] == 5
        assert set(d["risk_probabilities"]) == {"LEO-1", "MEO-1", "GEO-1"}
        assert d["decision"] in ("STAY", "SWITCH")
        assert d["decision_reason"]
        assert len(d["candidates"]) == 3


def test_provenance_over_the_bridge_never_claims_unmeasured_rssi(sync_client):
    """The whole point: real fields real, unmeasured RSSI not labelled a measurement."""
    sid = "android-bridge-prov"
    post_samples(sync_client, sid, WINDOW - 1)
    with sync_client.websocket_connect(f"/ws/observe/{sid}") as ws:
        ws.receive_json()
        sync_client.post("/telemetry", json={"session_id": sid, "sample": vivo_sample(WINDOW)})
        ws.receive_json()                     # ack
        prov = ws.receive_json()["field_provenance"]
    assert prov["rsrp_dbm"] == "real_cellular"
    assert prov["rsrq_db"] == "real_cellular"
    assert prov["sinr_db"] == "real_cellular"
    assert prov["rssi_dbm"] != "real_cellular"
    assert prov["rssi_dbm"] == "simulated"
    assert prov["latency_ms"] == "simulated"
    assert prov["orbit"] == "simulated"


def test_observer_is_read_only(sync_client):
    """An observer must not be able to inject telemetry into someone else's session."""
    sid = "android-readonly"
    post_samples(sync_client, sid, 2)
    before = sync_client.get(f"/telemetry/{sid}").json()["samples_received"]
    with sync_client.websocket_connect(f"/ws/observe/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"type": "telemetry", "sample": vivo_sample(99)})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "read-only" in err["message"]
    after = sync_client.get(f"/telemetry/{sid}").json()["samples_received"]
    assert after == before, "observer mutated the producer session"


def test_sessions_are_isolated_across_observers(sync_client):
    a, b = "android-iso-a", "android-iso-b"
    post_samples(sync_client, a, 1)
    post_samples(sync_client, b, 1)
    with sync_client.websocket_connect(f"/ws/observe/{a}") as wa:
        wa.receive_json()
        sync_client.post("/telemetry", json={"session_id": b, "sample": vivo_sample(5)})
        # Nothing from B may reach A: post to A and confirm the next frame is A's.
        sync_client.post("/telemetry", json={"session_id": a, "sample": vivo_sample(6, rsrp_dbm=-70.0)})
        ack = wa.receive_json()
        assert ack["session_id"] == a
        assert ack["normalized"]["rsrp_dbm"] == -70.0


def test_observer_reconnect_resumes_streaming(sync_client):
    sid = "android-reconnect"
    post_samples(sync_client, sid, 2)
    with sync_client.websocket_connect(f"/ws/observe/{sid}") as ws:
        ws.receive_json()
    assert _state["hub"].observer_count(sid) == 0, "observer not deregistered on close"

    with sync_client.websocket_connect(f"/ws/observe/{sid}") as ws:
        hello = ws.receive_json()
        assert hello["producer_present"] is True
        assert hello["samples_received"] == 2
        sync_client.post("/telemetry", json={"session_id": sid, "sample": vivo_sample(3)})
        assert ws.receive_json()["type"] == "telemetry_ack"


def test_missing_sinr_still_blocks_prediction_over_the_bridge(sync_client):
    sid = "android-nosinr"
    with sync_client.websocket_connect(f"/ws/observe/{sid}") as ws:
        ws.receive_json()
        for i in range(WINDOW):
            sync_client.post("/telemetry", json={
                "session_id": sid, "sample": {**vivo_sample(i), "sinr_db": None}})
            ws.receive_json()                 # ack
            last = ws.receive_json()
        assert last["type"] == "status", "a decision must not appear without SINR"
        assert last["prediction_ready"] is False


def test_producer_socket_still_works_alongside_observers(sync_client):
    """M7's producer path is unchanged by the bridge."""
    with sync_client.websocket_connect("/ws/android-producer") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "telemetry", "sample": vivo_sample(0), "mode": "predictive"})
        assert ws.receive_json()["type"] == "telemetry_ack"


def test_existing_http_endpoints_unchanged(sync_client):
    assert sync_client.get("/health").json()["status"] == "ok"
    g = json.load(open("artifacts/golden_fixtures.json"))["cases"][0]
    r = sync_client.post("/predict", json={"telemetry_window": g["telemetry_window"]})
    assert abs(r.json()["degradation_probability"] - g["expected_probability"]) < 1e-6
