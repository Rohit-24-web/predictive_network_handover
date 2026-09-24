"""Typed models for candidate paths and decisions.

IMPORTANT — REAL vs SIMULATED
-----------------------------
Every PathObservation in M3 is SIMULATED. These are not satellite measurements.
The Phase 2 Android client supplies REAL cellular telemetry; the GEO/MEO/LEO
candidate-path context around it is a simulation. Do not conflate the two.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Dict, List, Optional

# The exact field set required by app/features.py REQUIRED_RAW_FIELDS.
# The Predictor rejects any window missing one of these.
TELEMETRY_FIELDS = (
    "timestamp", "orbit", "rsrp_dbm", "rsrq_db", "sinr_db", "rssi_dbm",
    "elevation_deg", "latency_ms", "jitter_ms", "packet_loss_pct",
    "throughput_mbps", "load", "traffic_demand", "env_index", "available",
)


class Mode(str, Enum):
    REACTIVE = "reactive"
    PREDICTIVE = "predictive"


class DecisionType(str, Enum):
    STAY = "STAY"
    SWITCH = "SWITCH"


class DecisionReason(str, Enum):
    STAY_CURRENT_PATH = "STAY_CURRENT_PATH"
    PREDICTED_DEGRADATION = "PREDICTED_DEGRADATION"
    CURRENT_PATH_DEGRADED = "CURRENT_PATH_DEGRADED"
    INSUFFICIENT_IMPROVEMENT = "INSUFFICIENT_IMPROVEMENT"
    MIN_DWELL_ACTIVE = "MIN_DWELL_ACTIVE"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    CANDIDATE_UNAVAILABLE = "CANDIDATE_UNAVAILABLE"
    SWITCH_TO_BETTER_PATH = "SWITCH_TO_BETTER_PATH"
    HANDOVER_IN_PROGRESS = "HANDOVER_IN_PROGRESS"


@dataclass(frozen=True)
class PathObservation:
    """One SIMULATED candidate path at one timestep.

    Carries the full 15-field telemetry contract so it can be handed to the
    Predictor without translation. `signal_quality` is deliberately absent: it is
    not a model input. Derive quality with features.quality_from_metrics().
    """
    path_id: str
    timestamp: float
    orbit: str
    rsrp_dbm: float
    rsrq_db: float
    sinr_db: float
    rssi_dbm: float
    elevation_deg: float
    latency_ms: float
    jitter_ms: float
    packet_loss_pct: float
    throughput_mbps: float
    load: float
    traffic_demand: float
    env_index: float
    available: float

    def to_telemetry_dict(self) -> Dict[str, Any]:
        """Exactly the fields features.window_to_array expects — path_id excluded."""
        d = asdict(self)
        d.pop("path_id")
        return d

    @property
    def is_available(self) -> bool:
        return self.available >= 0.5


@dataclass(frozen=True)
class PathScore:
    """Full breakdown of one path's score, so any decision can be explained."""
    path_id: str
    orbit: str
    available: bool
    score: float
    quality: float
    latency_score: float
    tput_score: float
    load_penalty: float
    switching_penalty: float
    risk_penalty: float
    risk: Optional[float]


@dataclass(frozen=True)
class DecisionRecord:
    """One engine decision, fully explainable."""
    step: int
    timestamp: float
    mode: Mode
    current_path: str
    selected_path: str
    decision: DecisionType
    reason: DecisionReason
    degradation_probability: Optional[float]   # incumbent's risk; None in reactive mode
    handover: bool
    scores: List[PathScore]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "timestamp": self.timestamp,
            "mode": self.mode.value,
            "current_path": self.current_path,
            "selected_path": self.selected_path,
            "decision": self.decision.value,
            "decision_reason": self.reason.value,
            "degradation_probability": self.degradation_probability,
            "handover": self.handover,
            "candidate_paths": [asdict(s) for s in self.scores],
        }
