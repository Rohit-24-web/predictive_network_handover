"""
schemas.py — Pydantic request/response models for the prediction API.

Validates incoming telemetry observations and structures API responses.
Field constraints match what features.py expects from REQUIRED_RAW_FIELDS.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class TelemetryObservation(BaseModel):
    """A single telemetry sample from one network path at one point in time.

    All fields are required. The orbit field is validated against known orbits.
    Numeric fields must be finite (no NaN/Inf).
    """
    timestamp: float = Field(..., description="Unix-style timestamp (seconds)")
    orbit: Literal["LEO", "MEO", "GEO"] = Field(
        ..., description="Satellite orbit type"
    )
    rsrp_dbm: float = Field(..., description="Reference Signal Received Power (dBm)")
    rsrq_db: float = Field(..., description="Reference Signal Received Quality (dB)")
    sinr_db: float = Field(..., description="Signal-to-Interference-plus-Noise Ratio (dB)")
    rssi_dbm: float = Field(..., description="Received Signal Strength Indicator (dBm)")
    elevation_deg: float = Field(..., description="Satellite elevation angle (degrees)")
    latency_ms: float = Field(..., description="Round-trip latency (ms)")
    jitter_ms: float = Field(..., description="Jitter (ms)")
    packet_loss_pct: float = Field(..., ge=0.0, description="Packet loss (%)")
    throughput_mbps: float = Field(..., ge=0.0, description="Throughput (Mbps)")
    load: float = Field(..., ge=0.0, le=1.0, description="Network load [0, 1]")
    traffic_demand: float = Field(
        ..., ge=0.0, le=1.0, description="Traffic demand [0, 1]"
    )
    env_index: float = Field(
        ..., ge=0.0, le=1.0, description="Environment index [0, 1]"
    )
    available: float = Field(
        ..., ge=0.0, le=1.0, description="Path availability [0, 1]"
    )

    @field_validator(
        "rsrp_dbm", "rsrq_db", "sinr_db", "rssi_dbm",
        "elevation_deg", "latency_ms", "jitter_ms", "packet_loss_pct",
        "throughput_mbps", "load", "traffic_demand", "env_index",
        "available", "timestamp",
        mode="after",
    )
    @classmethod
    def must_be_finite(cls, v: float) -> float:
        """Reject NaN and infinite values — they silently corrupt predictions."""
        import math
        if not math.isfinite(v):
            raise ValueError(f"value must be finite, got {v}")
        return v


class PredictRequest(BaseModel):
    """Request body for POST /predict.

    Contains a chronologically ordered window of telemetry observations.
    The window must have at least 1 observation; the feature pipeline
    handles short windows via edge-padding (matching Phase 1 training).
    The recommended window size is 26 steps for optimal predictions.
    """
    telemetry_window: List[TelemetryObservation] = Field(
        ...,
        min_length=1,
        description=(
            "Chronologically ordered telemetry observations. "
            "Recommended: 26 steps (required_window_steps from Phase 1). "
            "Shorter windows are edge-padded automatically."
        ),
    )


class PredictResponse(BaseModel):
    """Response body for POST /predict."""
    model_version: str = Field(..., description="Model version identifier")
    degradation_probability: float = Field(
        ..., ge=0.0, le=1.0,
        description="Probability of link degradation within the prediction horizon",
    )
    prediction: int = Field(
        ..., ge=0, le=1,
        description="Binary prediction: 1 = degradation predicted, 0 = healthy",
    )
    prediction_horizon_seconds: int = Field(
        ..., description="Prediction horizon in seconds"
    )
    threshold: float = Field(
        ..., ge=0.0, le=1.0,
        description="Decision threshold used for binary prediction",
    )


class HealthResponse(BaseModel):
    """Response body for GET /health."""
    status: str = Field(..., description="Service status")
    model_loaded: bool = Field(..., description="Whether the ML model is loaded")
    model_version: str = Field(..., description="Loaded model version")
    artifact_dir: str = Field(..., description="Path to artifact directory")


class ModelInfoResponse(BaseModel):
    """Response body for GET /model/info."""
    model_version: str
    model_name: str
    prediction_horizon_seconds: int
    decision_threshold: float
    n_features: int
    required_window_steps: int
    orbits: List[str]
    is_sequence_model: bool
    test_metrics: dict


# ===========================================================================
# M4 — integration endpoint schemas (POST /decision)
# ===========================================================================
class CandidatePathWindow(BaseModel):
    """One candidate path plus its backward-looking telemetry window.

    The window is the SAME contract the Predictor already uses: chronologically
    ordered observations, oldest first, with the last entry being 'now'. Short
    windows are edge-padded by the existing pipeline (M2 behaviour) and flagged
    via `warming_up` in the response rather than silently accepted.
    """
    path_id: str = Field(..., min_length=1, description="Candidate path id, e.g. 'LEO-1'")
    telemetry_window: List[TelemetryObservation] = Field(
        ..., min_length=1,
        description="Chronological telemetry for this path; last entry is current.",
    )


class DecisionRequest(BaseModel):
    """Request body for POST /decision."""
    session_id: str = Field(
        ..., min_length=1,
        description=(
            "Caller-supplied session key. The decision engine is stateful (dwell, "
            "cooldown, handover interruption), so reusing a session_id across "
            "consecutive steps is what keeps hysteresis working."
        ),
    )
    mode: Literal["reactive", "predictive"] = Field(
        ..., description="reactive = no ML; predictive = ML risk for every candidate."
    )
    candidates: List[CandidatePathWindow] = Field(
        ..., min_length=1, description="All candidate paths under consideration."
    )
    current_path: Optional[str] = Field(
        None,
        description=(
            "Incumbent path. Omit on the first call to let the engine choose the "
            "best available path with the risk term disabled (both modes start "
            "identically, which keeps a reactive/predictive comparison fair)."
        ),
    )

    @field_validator("candidates", mode="after")
    @classmethod
    def unique_path_ids(cls, v: List[CandidatePathWindow]) -> List[CandidatePathWindow]:
        ids = [c.path_id for c in v]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate path_id in candidates: {ids}")
        return v


class CandidateScore(BaseModel):
    """Score breakdown for one candidate, so any decision can be explained."""
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
    risk: Optional[float] = None
    warming_up: bool = Field(
        False, description="Window shorter than required_window_steps (edge-padded)."
    )


class DecisionResponse(BaseModel):
    """Response body for POST /decision."""
    session_id: str
    mode: str
    current_path: str
    selected_path: str
    decision: str = Field(..., description="STAY or SWITCH")
    decision_reason: str
    handover: bool
    candidates: List[CandidateScore]
    degradation_probability: Optional[float] = Field(
        None, description="Incumbent path's risk; null in reactive mode."
    )
    risk_probabilities: Optional[Dict[str, float]] = Field(
        None, description="P(degradation) per candidate; null in reactive mode."
    )
    prediction_horizon_seconds: Optional[int] = None
    model_version: Optional[str] = Field(
        None, description="Null in reactive mode: no model was consulted."
    )


# ===========================================================================
# M5 — REAL Android cellular telemetry ingestion
# ===========================================================================
class AndroidTelemetrySample(BaseModel):
    """One REAL cellular measurement from an Android handset.

    Terrestrial cellular only. No satellite fields appear here, because a handset
    cannot measure them. `sinr_db` is optional because real devices frequently do
    not expose it (getRssnr() may be absent or return a sentinel).
    """
    timestamp: float = Field(..., gt=0, description="Unix timestamp (seconds)")
    rsrp_dbm: float = Field(..., ge=-140, le=-40, description="Reference Signal Received Power")
    rsrq_db: float = Field(..., ge=-25, le=-3, description="Reference Signal Received Quality")
    rssi_dbm: Optional[float] = Field(
        None, ge=-120, le=-30,
        description=("Received Signal Strength Indicator. Optional: 5G NR does not define "
                     "RSSI and many LTE modems omit it, so a real device may not report it."))
    sinr_db: Optional[float] = Field(
        None, description="Signal-to-Interference-plus-Noise Ratio; omit when the device does not report it.")
    network_type: Optional[str] = Field(None, description="NR, LTE, UMTS, GSM or UNKNOWN")
    device_id: Optional[str] = Field(None, description="Opaque device label (optional)")
    cell_id: Optional[str] = Field(None, description="Serving cell identifier (optional)")

    @field_validator("timestamp", "rsrp_dbm", "rsrq_db", "rssi_dbm", "sinr_db", mode="after")
    @classmethod
    def finite(cls, v):
        import math
        if v is not None and not math.isfinite(v):
            raise ValueError(f"value must be finite, got {v}")
        return v


class TelemetryIngestRequest(BaseModel):
    """Request body for POST /telemetry."""
    session_id: str = Field(..., min_length=1, description="Device session key")
    sample: AndroidTelemetrySample
    allow_sinr_estimate: bool = Field(
        False,
        description=("When the device omits SINR, derive a crude estimate from RSRQ and "
                     "label it as estimated. Off by default: an estimate must never be "
                     "mistaken for a measurement."),
    )


class NormalizedTelemetry(BaseModel):
    """Backend-internal representation returned for transparency."""
    timestamp: float
    rsrp_dbm: float
    rsrq_db: float
    rssi_dbm: Optional[float]
    rssi_source: str = Field(..., description="measured | unavailable")
    sinr_db: Optional[float]
    sinr_source: str = Field(..., description="measured | estimated_from_rsrq | unavailable")
    network_type: str
    device_id: Optional[str] = None
    cell_id: Optional[str] = None


class TelemetryIngestResponse(BaseModel):
    """Response body for POST /telemetry."""
    session_id: str
    accepted: bool
    samples_received: int
    buffer_size: int
    required_window_steps: int
    window_full: bool
    prediction_ready: bool = Field(
        ..., description="True only when the window is full and every sample has a SINR.")
    normalized: NormalizedTelemetry
    warnings: List[str] = Field(default_factory=list)


class TelemetryStatusResponse(BaseModel):
    """Response body for GET /telemetry/{session_id}."""
    session_id: str
    samples_received: int
    buffer_size: int
    required_window_steps: int
    window_full: bool
    prediction_ready: bool
    latest: Optional[NormalizedTelemetry] = None


class TelemetryDecisionRequest(BaseModel):
    """Request body for POST /telemetry/decision.

    Drives the EXISTING M4 orchestration from buffered REAL telemetry. Candidate
    path conditions stay SIMULATED; only the radio fields of the merged path come
    from the handset.
    """
    session_id: str = Field(..., min_length=1)
    mode: Literal["reactive", "predictive"] = "predictive"
    merge_real_radio_into: Optional[str] = Field(
        "LEO-1",
        description=("Candidate path whose radio fields are overlaid with the device's "
                     "REAL measurements. Set null to run fully simulated."),
    )


class TelemetryDecisionResponse(BaseModel):
    """Decision plus explicit REAL/SIMULATED provenance."""
    decision: DecisionResponse
    merged_path: Optional[str]
    field_provenance: Dict[str, str] = Field(
        ..., description="Per-field origin: real_cellular or simulated.")
    simulation_step: int
    notes: List[str] = Field(default_factory=list)
