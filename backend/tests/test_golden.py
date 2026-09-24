"""
test_golden.py — Golden fixture integration test.

Asserts the backend reproduces 12 known (telemetry_window → probability) pairs
from Phase 1 within the stated tolerance (1e-6). This is the single most
valuable test in the project: it proves the inference pipeline reproduces
Phase 1 rather than merely running.

See PHASE2_BRIEF.md §4.2.
"""
from __future__ import annotations

import json
import os

import pytest

from app.inference import Predictor

# Resolve paths relative to the backend/ directory
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ARTIFACTS_DIR = os.path.join(_BACKEND_DIR, "artifacts")
_FIXTURES_PATH = os.path.join(_ARTIFACTS_DIR, "golden_fixtures.json")

with open(_FIXTURES_PATH, "r") as _f:
    FIX = json.load(_f)


@pytest.fixture(scope="module")
def predictor() -> Predictor:
    """Load the model once for the entire module — mirrors startup behaviour."""
    return Predictor(_ARTIFACTS_DIR)


@pytest.mark.parametrize(
    "case",
    FIX["cases"],
    ids=[f"case_{i}" for i in range(len(FIX["cases"]))],
)
def test_reproduces_phase1(predictor: Predictor, case: dict) -> None:
    """Each golden fixture must reproduce the expected probability within 1e-6."""
    got = predictor.predict(case["telemetry_window"])

    tolerance = FIX["tolerance"]
    expected_prob = case["expected_probability"]
    actual_prob = got["probability"]

    assert abs(actual_prob - expected_prob) < tolerance, (
        f"Probability mismatch: expected {expected_prob}, got {actual_prob}, "
        f"diff={abs(actual_prob - expected_prob):.2e} (tolerance={tolerance})"
    )
    assert got["prediction"] == case["expected_prediction"], (
        f"Prediction mismatch: expected {case['expected_prediction']}, "
        f"got {got['prediction']} (prob={actual_prob}, threshold={got['threshold']})"
    )
