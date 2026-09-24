"""
inference.py — Phase 2 model inference.

Loads the trained HistGradientBoosting model and StandardScaler from Phase 1
artifacts, and exposes a Predictor class that reproduces Phase 1 predictions
exactly.

Key constraints (see PHASE2_BRIEF.md §0):
- Uses features.py verbatim — never reimplements feature engineering.
- Never refits the scaler — only calls .transform(), never .fit().
- Feature order comes from feature_config.json → feature_names.
- The model outputs one probability; path selection is a separate module.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import joblib
import numpy as np

from app import features as F


class Predictor:
    """Stateless inference wrapper. Load once at startup, call predict() per request."""

    def __init__(self, artifact_dir: str) -> None:
        cfg_path = os.path.join(artifact_dir, "feature_config.json")
        scaler_path = os.path.join(artifact_dir, "preprocessor_scaler.joblib")
        model_path = os.path.join(artifact_dir, "final_model.joblib")

        with open(cfg_path, "r") as f:
            self.cfg: Dict[str, Any] = json.load(f)

        self.scaler = joblib.load(scaler_path)
        self.model = joblib.load(model_path)

        assert not self.cfg["is_sequence_model"], "tabular model expected"
        assert self.cfg["n_features"] == len(self.cfg["feature_names"]), (
            f"feature count mismatch: n_features={self.cfg['n_features']} "
            f"vs len(feature_names)={len(self.cfg['feature_names'])}"
        )

        self.threshold: float = float(self.cfg["decision_threshold"])

    def predict(self, telemetry_window: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run inference on a telemetry window.

        Args:
            telemetry_window: List of raw telemetry observation dicts,
                              ordered chronologically. Each dict must contain
                              all fields listed in features.REQUIRED_RAW_FIELDS.

        Returns:
            Dict with keys: probability, prediction, horizon_seconds,
            threshold, model_version.

        Raises:
            ValueError: If telemetry_window is malformed (missing fields,
                        NaN/Inf values, unknown orbit, etc.).
        """
        # Validate orbit
        orbit = telemetry_window[-1]["orbit"]
        if orbit not in self.cfg["orbits"]:
            raise ValueError(f"unknown orbit {orbit!r}")

        # Convert raw dicts → numpy array (validates fields + finiteness)
        arr = F.window_to_array(telemetry_window, self.cfg)

        # Build feature vector: (1, n_features) for tabular model
        x = F.features_from_window(arr, orbit, self.cfg, n_steps=1)

        # Scale — NEVER .fit(), only .transform()
        x = self.scaler.transform(x)

        # Predict probability of degradation within horizon
        p = float(self.model.predict_proba(x)[0, 1])

        return {
            "probability": round(p, 6),
            "prediction": int(p >= self.threshold),
            "horizon_seconds": self.cfg["prediction_horizon_s"],
            "threshold": self.threshold,
            "model_version": self.cfg["model_version"],
        }
