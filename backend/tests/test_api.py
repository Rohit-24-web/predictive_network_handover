"""
test_api.py — FastAPI endpoint tests for M2.

Tests /health, /model/info, and /predict endpoints including:
- valid requests
- invalid/malformed requests
- insufficient telemetry window (still accepted — edge-padded)
- response schema validation
- error handling

Uses httpx AsyncClient with the FastAPI test client — no real server needed.
"""
from __future__ import annotations

import json
import math
import os
import copy

import pytest
# The `client` fixture lives in conftest.py, which runs the app lifespan
# so the ML model is actually loaded. See conftest.py for why.

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ARTIFACTS_DIR = os.path.join(_BACKEND_DIR, "artifacts")
_FIXTURES_PATH = os.path.join(_ARTIFACTS_DIR, "golden_fixtures.json")

with open(_FIXTURES_PATH, "r") as _f:
    GOLDEN = json.load(_f)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
class TestHealth:
    @pytest.mark.anyio
    async def test_health_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["model_loaded"] is True
        assert "model_version" in data

    @pytest.mark.anyio
    async def test_health_response_schema(self, client):
        resp = await client.get("/health")
        data = resp.json()
        required_keys = {"status", "model_loaded", "model_version", "artifact_dir"}
        assert required_keys.issubset(data.keys())


# ---------------------------------------------------------------------------
# GET /model/info
# ---------------------------------------------------------------------------
class TestModelInfo:
    @pytest.mark.anyio
    async def test_model_info_ok(self, client):
        resp = await client.get("/model/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model_version"] == "phase1-v1.0.0"
        assert data["model_name"] == "HistGradientBoosting"
        assert data["prediction_horizon_seconds"] == 5
        assert data["decision_threshold"] == 0.4
        assert data["n_features"] == 60
        assert data["required_window_steps"] == 26
        assert data["is_sequence_model"] is False
        assert set(data["orbits"]) == {"LEO", "MEO", "GEO"}

    @pytest.mark.anyio
    async def test_model_info_has_test_metrics(self, client):
        resp = await client.get("/model/info")
        data = resp.json()
        metrics = data["test_metrics"]
        assert "roc_auc" in metrics
        assert "pr_auc" in metrics
        assert "onset_pr_auc" in metrics


# ---------------------------------------------------------------------------
# POST /predict — valid requests
# ---------------------------------------------------------------------------
class TestPredictValid:
    @pytest.mark.anyio
    async def test_predict_golden_case_0(self, client):
        """Full 26-step golden fixture should produce the expected probability."""
        case = GOLDEN["cases"][0]
        resp = await client.post(
            "/predict",
            json={"telemetry_window": case["telemetry_window"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert abs(data["degradation_probability"] - case["expected_probability"]) < GOLDEN["tolerance"]
        assert data["prediction"] == case["expected_prediction"]

    @pytest.mark.anyio
    async def test_predict_response_schema(self, client):
        """Response must include all required fields with correct types."""
        case = GOLDEN["cases"][0]
        resp = await client.post(
            "/predict",
            json={"telemetry_window": case["telemetry_window"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["model_version"], str)
        assert isinstance(data["degradation_probability"], float)
        assert data["prediction"] in (0, 1)
        assert isinstance(data["prediction_horizon_seconds"], int)
        assert isinstance(data["threshold"], float)
        assert data["prediction_horizon_seconds"] == 5
        assert data["threshold"] == 0.4

    @pytest.mark.anyio
    async def test_predict_short_window_accepted(self, client):
        """A window shorter than 26 steps should still work (edge-padded)."""
        case = GOLDEN["cases"][0]
        # Take only the last 5 observations
        short_window = case["telemetry_window"][-5:]
        resp = await client.post(
            "/predict",
            json={"telemetry_window": short_window},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert 0.0 <= data["degradation_probability"] <= 1.0
        assert data["prediction"] in (0, 1)

    @pytest.mark.anyio
    async def test_predict_single_observation(self, client):
        """Even a single observation should be accepted (edge-padded)."""
        case = GOLDEN["cases"][0]
        single = [case["telemetry_window"][-1]]
        resp = await client.post(
            "/predict",
            json={"telemetry_window": single},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert 0.0 <= data["degradation_probability"] <= 1.0


# ---------------------------------------------------------------------------
# POST /predict — invalid requests
# ---------------------------------------------------------------------------
class TestPredictInvalid:
    @pytest.mark.anyio
    async def test_empty_window(self, client):
        """Empty telemetry window should be rejected."""
        resp = await client.post(
            "/predict",
            json={"telemetry_window": []},
        )
        assert resp.status_code == 422  # Pydantic validation error

    @pytest.mark.anyio
    async def test_missing_field(self, client):
        """Observation missing a required field should be rejected."""
        case = GOLDEN["cases"][0]
        bad_obs = copy.deepcopy(case["telemetry_window"][-1])
        del bad_obs["sinr_db"]
        resp = await client.post(
            "/predict",
            json={"telemetry_window": [bad_obs]},
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_invalid_orbit(self, client):
        """Unknown orbit type should be rejected."""
        case = GOLDEN["cases"][0]
        bad_window = copy.deepcopy(case["telemetry_window"])
        bad_window[-1]["orbit"] = "INVALID"
        resp = await client.post(
            "/predict",
            json={"telemetry_window": bad_window},
        )
        assert resp.status_code == 422  # Literal validation

    @pytest.mark.anyio
    async def test_nan_value_rejected(self, client):
        """NaN values should be rejected by Pydantic validation."""
        case = GOLDEN["cases"][0]
        bad_obs = copy.deepcopy(case["telemetry_window"][-1])
        bad_obs["sinr_db"] = float("nan")
        # httpx's json= encoder refuses to serialise NaN, so the request would never
        # leave the client. Send a raw body instead: json.dumps emits a bare `NaN`
        # literal, which is what a non-conforming real-world client would send, and
        # which the server's json.loads will happily accept. That is exactly the case
        # the finite-value validator exists to catch.
        body = json.dumps({"telemetry_window": [bad_obs]})
        assert "NaN" in body
        resp = await client.post(
            "/predict",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_no_body(self, client):
        """Request with no body should be rejected."""
        resp = await client.post("/predict")
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_wrong_content_type(self, client):
        """Non-JSON content should be rejected."""
        resp = await client.post(
            "/predict",
            content="not json",
            headers={"content-type": "text/plain"},
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_mixed_orbits_rejected(self, client):
        """Window with mixed orbits should be rejected (400)."""
        case = GOLDEN["cases"][0]
        window = copy.deepcopy(case["telemetry_window"][:2])
        window[0]["orbit"] = "LEO"
        window[1]["orbit"] = "GEO"
        resp = await client.post(
            "/predict",
            json={"telemetry_window": window},
        )
        assert resp.status_code == 400
        assert "same orbit" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_negative_throughput_rejected(self, client):
        """Negative throughput should fail Pydantic validation."""
        case = GOLDEN["cases"][0]
        bad_obs = copy.deepcopy(case["telemetry_window"][-1])
        bad_obs["throughput_mbps"] = -10.0
        resp = await client.post(
            "/predict",
            json={"telemetry_window": [bad_obs]},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Swagger docs
# ---------------------------------------------------------------------------
class TestDocs:
    @pytest.mark.anyio
    async def test_openapi_schema(self, client):
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "/health" in schema["paths"]
        assert "/model/info" in schema["paths"]
        assert "/predict" in schema["paths"]

    @pytest.mark.anyio
    async def test_docs_page(self, client):
        resp = await client.get("/docs")
        assert resp.status_code == 200
