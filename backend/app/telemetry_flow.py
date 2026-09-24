"""Shared telemetry -> decision flow, used by BOTH the HTTP and WebSocket layers.

Extracted from the M5 `/telemetry/decision` endpoint so M7 can reuse it verbatim
instead of duplicating decision logic. Neither transport owns any policy: this
module only sequences existing, already-tested components.

    TelemetryStore (M5)  ->  simulated candidates (M3)  ->  orchestrator (M4)

It raises FlowError with an HTTP-style status; HTTP maps that to HTTPException and
WebSocket maps it to a structured error frame. Same rules, two transports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.decision import Mode
from app.orchestration import HandoverOrchestrator, SessionModeMismatch
from app.simulation import SCENARIOS, generate_scenario
from app.telemetry import (
    TelemetryError,
    TelemetryStore,
    field_provenance,
    merge_real_radio,
    session_seed,
)


class FlowError(Exception):
    """Client-attributable problem, carrying the status the HTTP layer would use."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass
class TelemetryDecisionResult:
    outcome: Any                      # orchestration.DecisionOutcome
    merged_path: Optional[str]
    provenance: Dict[str, str]
    simulation_step: int
    notes: List[str]


def run_telemetry_decision(state: Dict[str, Any], session_id: str, mode: str,
                           merge_into: Optional[str]) -> TelemetryDecisionResult:
    """Drive one decision from buffered REAL telemetry.

    `state` is the app's lifespan dict: telemetry_store, orchestrator, predictor,
    sim_scenarios. Passing it in keeps this module free of FastAPI imports.
    """
    store: TelemetryStore = state.get("telemetry_store")
    orchestrator: HandoverOrchestrator = state.get("orchestrator")
    predictor = state.get("predictor")
    # Identity checks, never truthiness: TelemetryStore defines __len__, so an empty
    # store is falsy. This project has hit that bug twice; do not reintroduce it.
    if store is None or orchestrator is None or predictor is None:
        raise FlowError(503, "not_ready", "Service not ready")

    session = store.get(session_id)
    if session is None:
        raise FlowError(404, "unknown_session", f"unknown session {session_id!r}")
    if not session.window_full:
        raise FlowError(
            400, "insufficient_telemetry",
            f"need {store.window_size} buffered samples before a decision; "
            f"have {session.buffer_size}",
        )

    notes: List[str] = []
    latest_sample = session.latest
    window = int(predictor.cfg["required_window_steps"])

    # SIMULATED candidate-path context, reproducible per session.
    scenarios = state.setdefault("sim_scenarios", {})
    record = scenarios.get(session_id)
    if record is None:
        record = generate_scenario(
            "multi_path", session_seed(session_id), 600,
            scenario_fn=SCENARIOS["multi_path"],
        )
        scenarios[session_id] = record
    step = min(window + (session.received % (record.n_steps - window - 1)),
               record.n_steps - 1)

    observations = record.observations_at(step)
    windows = {pid: record.window(pid, step, window) for pid in record.path_ids}

    if merge_into:
        if merge_into not in observations:
            raise FlowError(
                400, "unknown_path",
                f"merge_real_radio_into={merge_into!r} is not a candidate path "
                f"({sorted(observations)})",
            )
        if not session.prediction_ready():
            raise FlowError(
                400, "sinr_unavailable",
                "buffered telemetry is missing SINR for at least one sample, so it "
                "cannot satisfy the model contract. Re-send with "
                "allow_sinr_estimate=true to use a labelled estimate.",
            )
        try:
            buffered = list(session.samples)
            observations[merge_into] = merge_real_radio(observations[merge_into], buffered[-1])
            windows[merge_into] = [
                merge_real_radio(sim_obs, real).to_telemetry_dict()
                for sim_obs, real in zip(
                    record.telemetry[merge_into][step - window + 1: step + 1],
                    buffered[-window:],
                )
            ]
        except TelemetryError as e:
            raise FlowError(400, "merge_failed", str(e))
        notes.append(
            f"REAL cellular radio fields overlaid onto {merge_into}; all other fields simulated."
        )
        if any(s.sinr_source != "measured" for s in buffered[-window:]):
            notes.append("At least one buffered sample used an ESTIMATED SINR, not a measurement.")
        if any(s.rssi_dbm is None for s in buffered[-window:]):
            notes.append(
                "This device does not report RSSI; that field came from the simulated "
                "path and is labelled simulated in field_provenance."
            )
    else:
        notes.append("Fully simulated run; buffered cellular telemetry not merged.")

    try:
        outcome = orchestrator.decide(
            mode=Mode(mode), observations=observations, windows=windows,
            session_id=f"telemetry::{session_id}",
        )
    except SessionModeMismatch as e:
        raise FlowError(409, "mode_mismatch", str(e))
    except ValueError as e:
        raise FlowError(400, "decision_rejected", str(e))

    return TelemetryDecisionResult(
        outcome=outcome, merged_path=merge_into,
        provenance=field_provenance(merge_into, latest_sample),
        simulation_step=step, notes=notes,
    )


def decision_payload(result: TelemetryDecisionResult) -> Dict[str, Any]:
    """Serialise a decision to plain JSON-compatible types (shared by both transports)."""
    rec = result.outcome.record
    return {
        "session_id": result.outcome.session_id,
        "mode": rec.mode.value,
        "current_path": rec.current_path,
        "selected_path": rec.selected_path,
        "decision": rec.decision.value,
        "decision_reason": rec.reason.value,
        "handover": rec.handover,
        "degradation_probability": rec.degradation_probability,
        "risk_probabilities": result.outcome.risks,
        "prediction_horizon_seconds": result.outcome.horizon_seconds,
        "model_version": result.outcome.model_version,
        "candidates": [
            {
                "path_id": s.path_id, "orbit": s.orbit, "available": s.available,
                "score": s.score, "quality": s.quality, "latency_score": s.latency_score,
                "tput_score": s.tput_score, "load_penalty": s.load_penalty,
                "switching_penalty": s.switching_penalty, "risk_penalty": s.risk_penalty,
                "risk": s.risk,
                "warming_up": result.outcome.warming_up.get(s.path_id, False),
            }
            for s in rec.scores
        ],
    }
