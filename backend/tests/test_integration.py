"""M4 integration tests: FastAPI -> Predictor -> DecisionEngine -> decision.

Focus is the wiring, not the components: M1/M2/M3 already cover feature engineering,
inference, engine policy and the simulator. What is new here is that the pieces are
connected correctly and that the reactive/predictive boundary survives the API layer.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.decision import EngineConfig, Mode, PathObservation
from app.inference import Predictor
from app.orchestration import (
    HandoverOrchestrator,
    SessionModeMismatch,
    SessionStore,
    scenario_step_inputs,
)
from app.simulation import SCENARIOS, generate_scenario

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ARTIFACTS = os.path.join(_BACKEND, "artifacts")


@pytest.fixture(scope="module")
def cfg() -> EngineConfig:
    return EngineConfig.from_json(os.path.join(_ARTIFACTS, "decision_engine_config.json"))


@pytest.fixture(scope="module")
def predictor() -> Predictor:
    return Predictor(_ARTIFACTS)


@pytest.fixture(scope="module")
def record():
    """A real M3 scenario — the simulator drives the integration, unmodified."""
    return generate_scenario("multi_path", 5, 140, scenario_fn=SCENARIOS["multi_path"])


@pytest.fixture(scope="module")
def step_inputs(record, predictor):
    return scenario_step_inputs(record, 100, predictor.cfg["required_window_steps"])


def _payload(record, predictor, t: int, mode: str, session_id: str,
             current_path: str | None = None) -> dict:
    size = predictor.cfg["required_window_steps"]
    body = {
        "session_id": session_id,
        "mode": mode,
        "candidates": [
            {"path_id": pid, "telemetry_window": record.window(pid, t, size)}
            for pid in record.path_ids
        ],
    }
    if current_path:
        body["current_path"] = current_path
    return body


# ===========================================================================
# Orchestrator (no HTTP)
# ===========================================================================
def test_reactive_does_not_call_the_predictor(cfg, predictor, step_inputs):
    """The reactive/predictive boundary must survive the integration layer."""
    obs, win = step_inputs
    orc = HandoverOrchestrator(predictor, cfg)
    with patch.object(Predictor, "predict", side_effect=AssertionError(
            "reactive mode must not invoke the Predictor")) as spy:
        out = orc.decide(Mode.REACTIVE, obs, win, session_id="react-1")
    assert spy.call_count == 0
    assert out.risks is None
    assert out.model_version is None
    assert out.horizon_seconds is None


def test_predictive_calls_predictor_once_per_candidate(cfg, predictor, step_inputs):
    """Risk for EVERY candidate, not just the incumbent — that is traffic steering."""
    obs, win = step_inputs
    orc = HandoverOrchestrator(predictor, cfg)
    real = Predictor.predict
    with patch.object(Predictor, "predict", autospec=True,
                      side_effect=lambda self, w: real(self, w)) as spy:
        out = orc.decide(Mode.PREDICTIVE, obs, win, session_id="pred-1")
    assert spy.call_count == len(obs) == 3
    assert set(out.risks) == set(obs)
    assert all(0.0 <= v <= 1.0 for v in out.risks.values())
    assert out.model_version == "phase1-v1.0.0"
    assert out.horizon_seconds == 5


def test_real_predictor_end_to_end_from_simulator(cfg, predictor, record):
    """No mocks: scenario -> telemetry -> real model -> engine -> decision."""
    orc = HandoverOrchestrator(predictor, cfg)
    size = predictor.cfg["required_window_steps"]
    reasons = set()
    for t in range(60, 90):
        obs, win = scenario_step_inputs(record, t, size)
        out = orc.decide(Mode.PREDICTIVE, obs, win, session_id="e2e")
        reasons.add(out.record.reason.value)
        assert out.record.selected_path in obs
        assert len(out.record.scores) == 3
    assert reasons, "no decisions produced"


def test_session_state_persists_so_hysteresis_survives_http(cfg, predictor, record):
    """Without session reuse every call would reset dwell/cooldown and disable hysteresis."""
    orc = HandoverOrchestrator(predictor, cfg)
    size = predictor.cfg["required_window_steps"]
    for t in range(40, 50):
        obs, win = scenario_step_inputs(record, t, size)
        orc.decide(Mode.REACTIVE, obs, win, session_id="sticky")
    assert len(orc.sessions) == 1
    session = orc.sessions._sessions["sticky"]
    assert session.step == 10, "session did not accumulate steps"


def test_session_mode_mismatch_is_rejected(cfg, predictor, step_inputs):
    obs, win = step_inputs
    orc = HandoverOrchestrator(predictor, cfg)
    orc.decide(Mode.REACTIVE, obs, win, session_id="mixed")
    with pytest.raises(SessionModeMismatch):
        orc.decide(Mode.PREDICTIVE, obs, win, session_id="mixed")


def test_unknown_current_path_is_rejected(cfg, predictor, step_inputs):
    obs, win = step_inputs
    orc = HandoverOrchestrator(predictor, cfg)
    with pytest.raises(ValueError, match="not among the candidates"):
        orc.decide(Mode.REACTIVE, obs, win, session_id="bad", current_path="NOPE-9")


def test_predictive_requires_a_window_for_every_candidate(cfg, predictor, step_inputs):
    obs, win = step_inputs
    partial = {k: v for k, v in list(win.items())[:1]}
    orc = HandoverOrchestrator(predictor, cfg)
    with pytest.raises(ValueError, match="window for every candidate"):
        orc.decide(Mode.PREDICTIVE, obs, partial, session_id="partial")


def test_session_store_evicts_stale_sessions(cfg, predictor, step_inputs):
    obs, win = step_inputs
    orc = HandoverOrchestrator(predictor, cfg, store=SessionStore(ttl_s=-1.0))
    orc.decide(Mode.REACTIVE, obs, win, session_id="ephemeral")
    orc.decide(Mode.REACTIVE, obs, win, session_id="other")
    assert len(orc.sessions) <= 1, "stale sessions were not evicted"


# ===========================================================================
# HTTP layer
# ===========================================================================
class TestDecisionEndpoint:
    @pytest.mark.anyio
    async def test_reactive_request_succeeds(self, client, record, predictor):
        resp = await client.post("/decision",
                                 json=_payload(record, predictor, 100, "reactive", "http-r"))
        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["mode"] == "reactive"
        assert d["decision"] in ("STAY", "SWITCH")
        assert d["decision_reason"]
        assert d["risk_probabilities"] is None
        assert d["model_version"] is None
        assert d["degradation_probability"] is None
        assert len(d["candidates"]) == 3

    @pytest.mark.anyio
    async def test_predictive_request_succeeds_with_all_candidate_risks(
            self, client, record, predictor):
        resp = await client.post("/decision",
                                 json=_payload(record, predictor, 100, "predictive", "http-p"))
        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["mode"] == "predictive"
        assert set(d["risk_probabilities"]) == set(record.path_ids)
        assert all(0.0 <= v <= 1.0 for v in d["risk_probabilities"].values())
        assert d["model_version"] == "phase1-v1.0.0"
        assert d["prediction_horizon_seconds"] == 5
        assert d["degradation_probability"] is not None
        for c in d["candidates"]:
            assert c["risk"] is not None

    @pytest.mark.anyio
    async def test_decision_reason_is_always_valid(self, client, record, predictor):
        from app.decision import DecisionReason
        valid = {r.value for r in DecisionReason}
        for t in (60, 80, 100):
            resp = await client.post(
                "/decision", json=_payload(record, predictor, t, "predictive", "http-seq"))
            assert resp.status_code == 200
            assert resp.json()["decision_reason"] in valid

    @pytest.mark.anyio
    async def test_candidate_scores_are_explainable(self, client, record, predictor):
        resp = await client.post("/decision",
                                 json=_payload(record, predictor, 100, "predictive", "http-x"))
        for c in resp.json()["candidates"]:
            for key in ("score", "quality", "latency_score", "tput_score",
                        "load_penalty", "switching_penalty", "risk_penalty"):
                assert key in c

    @pytest.mark.anyio
    async def test_unknown_current_path_returns_400(self, client, record, predictor):
        body = _payload(record, predictor, 100, "reactive", "http-bad", current_path="NOPE-9")
        resp = await client.post("/decision", json=body)
        assert resp.status_code == 400
        assert "not among the candidates" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_mode_mismatch_returns_409(self, client, record, predictor):
        await client.post("/decision",
                          json=_payload(record, predictor, 100, "reactive", "http-mix"))
        resp = await client.post("/decision",
                                 json=_payload(record, predictor, 101, "predictive", "http-mix"))
        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_duplicate_path_ids_rejected(self, client, record, predictor):
        body = _payload(record, predictor, 100, "reactive", "http-dup")
        body["candidates"].append(body["candidates"][0])
        resp = await client.post("/decision", json=body)
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_empty_candidates_rejected(self, client, record, predictor):
        resp = await client.post("/decision",
                                 json={"session_id": "e", "mode": "reactive", "candidates": []})
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_invalid_mode_rejected(self, client, record, predictor):
        body = _payload(record, predictor, 100, "reactive", "http-mode")
        body["mode"] = "clairvoyant"
        resp = await client.post("/decision", json=body)
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_short_window_flagged_as_warming_up(self, client, record, predictor):
        body = _payload(record, predictor, 100, "predictive", "http-warm")
        for c in body["candidates"]:
            c["telemetry_window"] = c["telemetry_window"][-4:]
        resp = await client.post("/decision", json=body)
        assert resp.status_code == 200
        assert all(c["warming_up"] for c in resp.json()["candidates"])

    @pytest.mark.anyio
    async def test_existing_endpoints_still_work(self, client):
        assert (await client.get("/health")).json()["status"] == "ok"
        assert (await client.get("/model/info")).status_code == 200
        assert (await client.get("/docs")).status_code == 200
        schema = (await client.get("/openapi.json")).json()
        for p in ("/health", "/model/info", "/predict", "/decision"):
            assert p in schema["paths"]
