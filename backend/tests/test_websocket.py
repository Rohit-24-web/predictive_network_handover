"""M7 WebSocket tests.

Uses Starlette's TestClient (sync) because it supports websocket_connect and runs
the app lifespan, so the model and stores are actually loaded.

Telemetry fixtures are deterministic, realistic TERRESTRIAL cellular values. No test
claims Android hardware produced them.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.inference import Predictor
from app.main import app

WINDOW = 26
BASE_TS = 1_789_000_000.0


def sample(i: int = 0, sinr: float | None = 12.5, **over) -> dict:
    """Deterministic LTE-style sample. Values vary slightly so the window is not constant."""
    s = {
        "timestamp": BASE_TS + i,
        "rsrp_dbm": -99.7 + (i % 5) * 0.9,
        "rsrq_db": -11.0 + (i % 3) * 0.4,
        "rssi_dbm": -68.0,
        "sinr_db": sinr if sinr is None else sinr + (i % 4) * 0.6,
        "network_type": "LTE",
    }
    s.update(over)
    return s


def telemetry_msg(i: int = 0, **over) -> dict:
    return {"type": "telemetry", "sample": sample(i), **over}


@pytest.fixture(scope="module")
def sync_client():
    with TestClient(app) as c:
        yield c


def drain_connect(ws) -> dict:
    frame = ws.receive_json()
    assert frame["type"] == "connected"
    return frame


# --- 1. connection ---------------------------------------------------------
def test_websocket_connection_succeeds(sync_client):
    with sync_client.websocket_connect("/ws/ws-conn") as ws:
        frame = drain_connect(ws)
        assert frame["session_id"] == "ws-conn"
        assert frame["required_window_steps"] == WINDOW
        assert "telemetry" in frame["protocol"]["client"]


# --- 2, 6. valid telemetry accepted ---------------------------------------
def test_valid_telemetry_accepted_and_acknowledged(sync_client):
    with sync_client.websocket_connect("/ws/ws-valid") as ws:
        drain_connect(ws)
        ws.send_json(telemetry_msg(0))
        ack = ws.receive_json()
        assert ack["type"] == "telemetry_ack"
        assert ack["buffer_size"] == 1
        assert ack["sinr_source"] == "measured"
        assert ack["prediction_ready"] is False
        status = ws.receive_json()
        assert status["type"] == "status"
        assert status["prediction_ready"] is False


# --- 3. malformed JSON -----------------------------------------------------
def test_invalid_json_returns_structured_error(sync_client):
    with sync_client.websocket_connect("/ws/ws-badjson") as ws:
        drain_connect(ws)
        ws.send_text("{not json at all")
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "invalid_json"
        assert "Traceback" not in json.dumps(err)
        # Connection survives: the next valid message still works.
        ws.send_json(telemetry_msg(0))
        assert ws.receive_json()["type"] == "telemetry_ack"


def test_non_object_json_rejected(sync_client):
    with sync_client.websocket_connect("/ws/ws-array") as ws:
        drain_connect(ws)
        ws.send_text("[1, 2, 3]")
        assert ws.receive_json()["code"] == "invalid_json"


# --- 4. invalid message type ----------------------------------------------
def test_invalid_message_type_returns_error(sync_client):
    with sync_client.websocket_connect("/ws/ws-type") as ws:
        drain_connect(ws)
        ws.send_json({"type": "launch_satellite"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == "invalid_message_type"


def test_ping_pong(sync_client):
    with sync_client.websocket_connect("/ws/ws-ping") as ws:
        drain_connect(ws)
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


# --- 5. missing / invalid telemetry fields --------------------------------
def test_missing_sample_object_rejected(sync_client):
    with sync_client.websocket_connect("/ws/ws-nosample") as ws:
        drain_connect(ws)
        ws.send_json({"type": "telemetry"})
        err = ws.receive_json()
        assert err["code"] == "validation_error"


def test_missing_required_field_rejected(sync_client):
    with sync_client.websocket_connect("/ws/ws-missing") as ws:
        drain_connect(ws)
        bad = sample(0)
        del bad["rsrp_dbm"]
        ws.send_json({"type": "telemetry", "sample": bad})
        err = ws.receive_json()
        assert err["code"] == "validation_error"
        assert "rsrp_dbm" in err["message"]


def test_implausible_value_rejected(sync_client):
    with sync_client.websocket_connect("/ws/ws-implausible") as ws:
        drain_connect(ws)
        ws.send_json({"type": "telemetry", "sample": sample(0, rsrp_dbm=-400.0)})
        err = ws.receive_json()
        assert err["code"] == "validation_error"
        assert "outside plausible" in err["message"]


def test_invalid_mode_rejected(sync_client):
    with sync_client.websocket_connect("/ws/ws-mode") as ws:
        drain_connect(ws)
        ws.send_json(telemetry_msg(0, mode="clairvoyant"))
        assert ws.receive_json()["code"] == "validation_error"


# --- 7. no fabricated prediction before the window is full ----------------
def test_fewer_than_window_never_produces_a_decision(sync_client):
    with sync_client.websocket_connect("/ws/ws-partial") as ws:
        drain_connect(ws)
        for i in range(WINDOW - 1):
            ws.send_json(telemetry_msg(i))
            ack = ws.receive_json()
            assert ack["type"] == "telemetry_ack"
            status = ws.receive_json()
            assert status["type"] == "status", "a decision must not appear before the window fills"
            assert status["prediction_ready"] is False
        assert ack["buffer_size"] == WINDOW - 1


# --- 8, 9, 10. full window produces a real decision -----------------------
def _fill_and_decide(ws, n=WINDOW, **over):
    last = None
    for i in range(n):
        ws.send_json(telemetry_msg(i, **over))
        ws.receive_json()          # ack
        last = ws.receive_json()   # status or decision
    return last


def test_full_window_produces_decision_with_all_candidate_risks(sync_client):
    with sync_client.websocket_connect("/ws/ws-full") as ws:
        drain_connect(ws)
        final = _fill_and_decide(ws)
        assert final["type"] == "decision"
        d = final["decision"]
        assert d["decision"] in ("STAY", "SWITCH")
        assert d["decision_reason"]
        assert d["model_version"] == "phase1-v1.0.0"
        assert d["prediction_horizon_seconds"] == 5
        assert set(d["risk_probabilities"]) == {"LEO-1", "MEO-1", "GEO-1"}
        assert all(0.0 <= v <= 1.0 for v in d["risk_probabilities"].values())
        assert len(d["candidates"]) == 3
        assert final["field_provenance"]["rsrp_dbm"] == "real_cellular"
        assert final["field_provenance"]["latency_ms"] == "simulated"


def test_prediction_ready_true_after_full_window(sync_client):
    with sync_client.websocket_connect("/ws/ws-ready") as ws:
        drain_connect(ws)
        for i in range(WINDOW):
            ws.send_json(telemetry_msg(i))
            ack = ws.receive_json()
            ws.receive_json()
        assert ack["window_full"] is True
        assert ack["prediction_ready"] is True


def test_missing_sinr_blocks_decision_without_fabricating(sync_client):
    with sync_client.websocket_connect("/ws/ws-nosinr") as ws:
        drain_connect(ws)
        last = None
        for i in range(WINDOW):
            msg = {"type": "telemetry", "sample": sample(i, sinr=None)}
            msg["sample"]["sinr_db"] = None
            ws.send_json(msg)
            ack = ws.receive_json()
            last = ws.receive_json()
        assert ack["sinr_source"] == "unavailable"
        assert ack["prediction_ready"] is False
        assert last["type"] == "status"
        assert "SINR" in last["reason"]


# --- 11. session state persists across messages ---------------------------
def test_session_state_persists_across_messages(sync_client):
    with sync_client.websocket_connect("/ws/ws-persist") as ws:
        drain_connect(ws)
        sizes = []
        for i in range(5):
            ws.send_json(telemetry_msg(i))
            sizes.append(ws.receive_json()["buffer_size"])
            ws.receive_json()
        assert sizes == [1, 2, 3, 4, 5], "buffer reset between messages"


def test_state_survives_reconnect_for_same_session(sync_client):
    with sync_client.websocket_connect("/ws/ws-reconnect") as ws:
        drain_connect(ws)
        for i in range(3):
            ws.send_json(telemetry_msg(i))
            ws.receive_json(); ws.receive_json()
    with sync_client.websocket_connect("/ws/ws-reconnect") as ws:
        drain_connect(ws)
        ws.send_json(telemetry_msg(3))
        assert ws.receive_json()["buffer_size"] == 4


# --- 12. disconnect --------------------------------------------------------
def test_disconnect_is_clean_and_deregisters(sync_client):
    from app.main import _state
    manager = _state["ws_manager"]
    before = manager.connection_count()
    with sync_client.websocket_connect("/ws/ws-disc") as ws:
        drain_connect(ws)
        assert manager.connection_count("ws-disc") == 1
    assert manager.connection_count() == before
    assert manager.connection_count("ws-disc") == 0


# --- 13. session isolation -------------------------------------------------
def test_sessions_do_not_share_state(sync_client):
    with sync_client.websocket_connect("/ws/ws-iso-a") as a:
        drain_connect(a)
        for i in range(4):
            a.send_json(telemetry_msg(i))
            ack_a = a.receive_json(); a.receive_json()
        with sync_client.websocket_connect("/ws/ws-iso-b") as b:
            drain_connect(b)
            b.send_json(telemetry_msg(0))
            ack_b = b.receive_json(); b.receive_json()
    assert ack_a["buffer_size"] == 4
    assert ack_b["buffer_size"] == 1, "second session inherited the first session's buffer"


def test_concurrent_connections_tracked_independently(sync_client):
    from app.main import _state
    manager = _state["ws_manager"]
    with sync_client.websocket_connect("/ws/ws-c1") as w1:
        drain_connect(w1)
        with sync_client.websocket_connect("/ws/ws-c2") as w2:
            drain_connect(w2)
            assert manager.connection_count("ws-c1") == 1
            assert manager.connection_count("ws-c2") == 1


# --- 14. reactive must not touch the model --------------------------------
def test_reactive_mode_does_not_invoke_predictor(sync_client):
    """Fill the window first, THEN spy: buffering itself never calls the model."""
    with sync_client.websocket_connect("/ws/ws-reactive") as ws:
        drain_connect(ws)
        for i in range(WINDOW):
            ws.send_json(telemetry_msg(i, mode="reactive"))
            ws.receive_json(); ws.receive_json()

        with patch.object(Predictor, "predict", side_effect=AssertionError(
                "reactive mode must not invoke the Predictor")) as spy:
            ws.send_json(telemetry_msg(WINDOW, mode="reactive"))
            ws.receive_json()
            final = ws.receive_json()
            assert spy.call_count == 0
        assert final["type"] == "decision"
        assert final["decision"]["risk_probabilities"] is None
        assert final["decision"]["model_version"] is None


# --- 15. HTTP endpoints unaffected ----------------------------------------
def test_existing_http_endpoints_still_work(sync_client):
    assert sync_client.get("/health").json()["status"] == "ok"
    assert sync_client.get("/model/info").status_code == 200
    assert sync_client.get("/docs").status_code == 200
    g = json.load(open("artifacts/golden_fixtures.json"))["cases"][0]
    r = sync_client.post("/predict", json={"telemetry_window": g["telemetry_window"]})
    assert abs(r.json()["degradation_probability"] - g["expected_probability"]) < 1e-6


def test_http_telemetry_decision_still_works_after_refactor(sync_client):
    sid = "ws-http-mix"
    for i in range(WINDOW):
        sync_client.post("/telemetry", json={"session_id": sid, "sample": sample(i)})
    r = sync_client.post("/telemetry/decision", json={"session_id": sid, "mode": "predictive"})
    assert r.status_code == 200
    assert set(r.json()["decision"]["risk_probabilities"]) == {"LEO-1", "MEO-1", "GEO-1"}
