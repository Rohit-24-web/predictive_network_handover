"""
main.py — FastAPI application for the Predictive Network Handover system.

Loads the Phase 1 ML model once at startup via the lifespan context manager.
Exposes /health, /model/info, and /predict endpoints.

Key constraints (PHASE2_BRIEF.md §0):
- features.py is verbatim from Phase 1 — do not modify.
- Never refit the scaler — only .transform().
- Feature order from feature_config.json → feature_names.
- The ML model outputs a probability only; path selection is separate.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import ARTIFACT_DIR
from app.decision import EngineConfig, Mode, PathObservation
from app.inference import Predictor
from app.orchestration import HandoverOrchestrator, SessionModeMismatch
from app.telemetry_flow import FlowError, decision_payload, run_telemetry_decision
from app.events import SessionHub
from app.websocket import ConnectionManager, observer_websocket, telemetry_websocket
from app.simulation import SCENARIOS, generate_scenario
from app.telemetry import (
    TelemetryError,
    TelemetryStore,
    field_provenance,
    merge_real_radio,
    normalize_android_sample,
    session_seed,
)
from app.schemas import (
    CandidateScore,
    NormalizedTelemetry,
    TelemetryDecisionRequest,
    TelemetryDecisionResponse,
    TelemetryIngestRequest,
    TelemetryIngestResponse,
    TelemetryStatusResponse,
    DecisionRequest,
    DecisionResponse,
    HealthResponse,
    ModelInfoResponse,
    PredictRequest,
    PredictResponse,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("handover.api")

# ---------------------------------------------------------------------------
# Application state — populated during lifespan
# ---------------------------------------------------------------------------
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Load ML artifacts once at startup, release on shutdown."""
    logger.info("Starting Predictive Network Handover API")
    logger.info("Artifact directory: %s", ARTIFACT_DIR)

    t0 = time.perf_counter()
    try:
        predictor = Predictor(ARTIFACT_DIR)
        elapsed = time.perf_counter() - t0
        logger.info(
            "Model loaded in %.2f s — version=%s, threshold=%.3f, features=%d",
            elapsed,
            predictor.cfg["model_version"],
            predictor.threshold,
            predictor.cfg["n_features"],
        )
    except Exception:
        logger.exception("FATAL: failed to load ML artifacts")
        raise

    # Load model metadata for /model/info
    metadata_path = os.path.join(ARTIFACT_DIR, "model_metadata.json")
    with open(metadata_path, "r") as f:
        model_metadata = json.load(f)

    # M4: decision-engine config + orchestrator, built once alongside the model.
    engine_cfg = EngineConfig.from_json(
        os.path.join(ARTIFACT_DIR, "decision_engine_config.json")
    )
    orchestrator = HandoverOrchestrator(predictor, engine_cfg)
    logger.info(
        "Decision engine ready — min_dwell=%d, cooldown=%d, margin=%.2f, risk_weight=%.2f",
        engine_cfg.min_dwell_steps, engine_cfg.cooldown_steps,
        engine_cfg.improvement_margin, engine_cfg.risk_weight,
    )

    _state["predictor"] = predictor
    _state["model_metadata"] = model_metadata
    # M5: REAL Android cellular telemetry buffers, sized to the model's window.
    window = int(predictor.cfg["required_window_steps"])
    telemetry_store = TelemetryStore(window_size=window)
    logger.info("Telemetry ingestion ready — buffering %d samples per device session", window)

    _state["engine_cfg"] = engine_cfg
    _state["orchestrator"] = orchestrator
    _state["telemetry_store"] = telemetry_store
    _state["sim_scenarios"] = {}
    # M7: live WebSocket connections. Session state itself lives in the existing
    # TelemetryStore / SessionStore, not here.
    _state["ws_manager"] = ConnectionManager()
    # M8 live bridge: fans HTTP-ingested frames out to read-only dashboard sockets.
    # Publishing is a no-op when nobody is observing, so behaviour with no dashboard
    # attached is unchanged from M5/M7.
    _state["hub"] = SessionHub()
    _state["start_time"] = time.time()

    yield

    logger.info("Shutting down Predictive Network Handover API")
    _state.clear()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Predictive Network Handover API",
    description=(
        "ML-powered link degradation prediction for satellite network handover. "
        "Phase 2 backend serving the Phase 1 HistGradientBoosting model."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["monitoring"],
    summary="Service health check",
)
async def health() -> HealthResponse:
    """Returns service status and model loading state."""
    predictor: Predictor = _state.get("predictor")
    return HealthResponse(
        status="ok" if predictor else "degraded",
        model_loaded=predictor is not None,
        model_version=predictor.cfg["model_version"] if predictor else "not loaded",
        artifact_dir=ARTIFACT_DIR,
    )


@app.get(
    "/model/info",
    response_model=ModelInfoResponse,
    tags=["model"],
    summary="Model metadata and configuration",
)
async def model_info() -> ModelInfoResponse:
    """Returns model metadata including version, metrics, and configuration."""
    predictor: Predictor = _state.get("predictor")
    if predictor is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    metadata = _state["model_metadata"]

    return ModelInfoResponse(
        model_version=predictor.cfg["model_version"],
        model_name=predictor.cfg.get("model_name", metadata.get("selected_model", "unknown")),
        prediction_horizon_seconds=predictor.cfg["prediction_horizon_s"],
        decision_threshold=predictor.threshold,
        n_features=predictor.cfg["n_features"],
        required_window_steps=predictor.cfg["required_window_steps"],
        orbits=predictor.cfg["orbits"],
        is_sequence_model=predictor.cfg["is_sequence_model"],
        test_metrics=metadata.get("test_metrics", {}),
    )


@app.post(
    "/predict",
    response_model=PredictResponse,
    tags=["inference"],
    summary="Predict link degradation probability",
    responses={
        400: {"description": "Invalid telemetry input"},
        503: {"description": "Model not loaded"},
    },
)
async def predict(request: PredictRequest) -> PredictResponse:
    """Run degradation prediction on a telemetry window.

    Accepts a chronologically ordered window of telemetry observations.
    Returns the probability of link degradation within the prediction horizon.

    The window should ideally contain 26 observations (1 Hz sampling, 26 s
    of history). Shorter windows are edge-padded automatically, matching
    the Phase 1 training pipeline.
    """
    predictor: Predictor = _state.get("predictor")
    if predictor is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    # Convert Pydantic models to dicts for the inference pipeline
    telemetry_dicts = [obs.model_dump() for obs in request.telemetry_window]

    # Validate orbit consistency
    orbits_in_window = {obs.orbit for obs in request.telemetry_window}
    if len(orbits_in_window) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"All observations in a window must have the same orbit, got: {orbits_in_window}",
        )

    logger.info(
        "Prediction request: orbit=%s, window_size=%d",
        telemetry_dicts[-1]["orbit"],
        len(telemetry_dicts),
    )

    try:
        result = predictor.predict(telemetry_dicts)
    except ValueError as e:
        logger.warning("Validation error in predict: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Inference failed")
        raise HTTPException(status_code=500, detail="Internal inference error")

    return PredictResponse(
        model_version=result["model_version"],
        degradation_probability=result["probability"],
        prediction=result["prediction"],
        prediction_horizon_seconds=result["horizon_seconds"],
        threshold=result["threshold"],
    )


# ---------------------------------------------------------------------------
# M4 — integrated decision endpoint
# ---------------------------------------------------------------------------
@app.post(
    "/decision",
    response_model=DecisionResponse,
    tags=["decision"],
    summary="Telemetry -> (optional ML risk) -> deterministic handover decision",
    responses={
        400: {"description": "Invalid telemetry, unknown current_path, or mode mismatch"},
        503: {"description": "Service not ready"},
    },
)
async def decision(request: DecisionRequest) -> DecisionResponse:
    """Run one integrated decision step.

    **reactive** — the Predictor is never invoked; the engine sees only currently
    observed conditions. `risk_probabilities` and `model_version` come back null.

    **predictive** — one prediction per candidate path (not just the incumbent, which
    is what makes this traffic steering), then the deterministic engine scores every
    candidate with its risk term.

    The engine is stateful: reuse the same `session_id` across consecutive steps or
    hysteresis (dwell, cooldown, handover interruption) cannot function.

    Candidate path conditions are SIMULATED. Only the underlying cellular telemetry is
    intended to be real in a later milestone.
    """
    orchestrator: HandoverOrchestrator = _state.get("orchestrator")
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    mode = Mode(request.mode)

    observations: dict = {}
    windows: dict = {}
    for cand in request.candidates:
        last = cand.telemetry_window[-1]
        observations[cand.path_id] = PathObservation(
            path_id=cand.path_id, **last.model_dump()
        )
        windows[cand.path_id] = [o.model_dump() for o in cand.telemetry_window]

    logger.info(
        "Decision request: mode=%s session=%s candidates=%d current=%s",
        request.mode, request.session_id, len(observations), request.current_path,
    )

    try:
        outcome = orchestrator.decide(
            mode=mode, observations=observations, windows=windows,
            session_id=request.session_id, current_path=request.current_path,
        )
    except SessionModeMismatch as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        logger.warning("Decision rejected: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Decision failed")
        raise HTTPException(status_code=500, detail="Internal decision error")

    rec = outcome.record
    return DecisionResponse(
        session_id=outcome.session_id,
        mode=rec.mode.value,
        current_path=rec.current_path,
        selected_path=rec.selected_path,
        decision=rec.decision.value,
        decision_reason=rec.reason.value,
        handover=rec.handover,
        candidates=[
            CandidateScore(
                path_id=s.path_id, orbit=s.orbit, available=s.available,
                score=s.score, quality=s.quality, latency_score=s.latency_score,
                tput_score=s.tput_score, load_penalty=s.load_penalty,
                switching_penalty=s.switching_penalty, risk_penalty=s.risk_penalty,
                risk=s.risk, warming_up=outcome.warming_up.get(s.path_id, False),
            )
            for s in rec.scores
        ],
        degradation_probability=rec.degradation_probability,
        risk_probabilities=outcome.risks,
        prediction_horizon_seconds=outcome.horizon_seconds,
        model_version=outcome.model_version,
    )


# ---------------------------------------------------------------------------
# M5 — REAL Android cellular telemetry ingestion
# ---------------------------------------------------------------------------
def _normalized_model(sample) -> NormalizedTelemetry:
    return NormalizedTelemetry(**sample.to_dict())


@app.post(
    "/telemetry",
    response_model=TelemetryIngestResponse,
    tags=["telemetry"],
    summary="Ingest one REAL Android cellular measurement",
    responses={400: {"description": "Implausible, out-of-order or unusable sample"}},
)
async def ingest_telemetry(request: TelemetryIngestRequest) -> TelemetryIngestResponse:
    """Validate, normalise and buffer one cellular sample from a handset.

    This is TERRESTRIAL cellular telemetry. It is not a satellite measurement, and
    the phone cannot command a carrier handover.

    A missing SINR is never silently defaulted: the sample is marked
    `sinr_source="unavailable"` and `prediction_ready=false`, unless the caller opts
    in to a clearly-labelled RSRQ-derived estimate.
    """
    store: TelemetryStore = _state.get("telemetry_store")
    # `if not store` would be WRONG: TelemetryStore defines __len__, so an EMPTY
    # store is falsy and every first request would wrongly 503. Check identity.
    if store is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    try:
        sample, warnings = normalize_android_sample(
            request.sample.model_dump(), allow_sinr_estimate=request.allow_sinr_estimate
        )
        session = store.ingest(request.session_id, sample)
    except TelemetryError as e:
        logger.warning("Telemetry rejected: %s", e)
        raise HTTPException(status_code=400, detail=str(e))

    response = TelemetryIngestResponse(
        session_id=request.session_id,
        accepted=True,
        samples_received=session.received,
        buffer_size=session.buffer_size,
        required_window_steps=store.window_size,
        window_full=session.window_full,
        prediction_ready=session.prediction_ready(),
        normalized=_normalized_model(sample),
        warnings=warnings,
    )

    # --- M8: fan out to any dashboard observing this session -----------------
    hub: SessionHub = _state.get("hub")
    if hub is not None and hub.observer_count(request.session_id) > 0:
        # `normalized` is included because an observer did not send the sample and
        # therefore has no local copy of the measured values.
        hub.publish(request.session_id, {
            "type": "telemetry_ack",
            **response.model_dump(exclude={"accepted"}),
        })
        if session.prediction_ready():
            try:
                result = run_telemetry_decision(
                    _state, request.session_id, "predictive", "LEO-1"
                )
                hub.publish(request.session_id, {
                    "type": "decision",
                    "session_id": request.session_id,
                    "simulation_step": result.simulation_step,
                    "merged_path": result.merged_path,
                    "field_provenance": result.provenance,
                    "notes": result.notes,
                    "decision": decision_payload(result),
                })
            except FlowError as e:
                hub.publish(request.session_id,
                            {"type": "error", "code": e.code, "message": e.detail})
        else:
            hub.publish(request.session_id, {
                "type": "status",
                "session_id": request.session_id,
                "prediction_ready": False,
                "buffer_size": session.buffer_size,
                "required_window_steps": store.window_size,
                "reason": ("waiting for a full telemetry window"
                           if not session.window_full
                           else "at least one buffered sample has no SINR"),
            })

    return response


@app.get(
    "/telemetry/{session_id}",
    response_model=TelemetryStatusResponse,
    tags=["telemetry"],
    summary="Buffer status for a device session",
    responses={404: {"description": "Unknown session"}},
)
async def telemetry_status(session_id: str) -> TelemetryStatusResponse:
    """How much REAL telemetry has been buffered, and whether it can drive a prediction."""
    store: TelemetryStore = _state.get("telemetry_store")
    # `if not store` would be WRONG: TelemetryStore defines __len__, so an EMPTY
    # store is falsy and every first request would wrongly 503. Check identity.
    if store is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
    return TelemetryStatusResponse(
        session_id=session_id,
        samples_received=session.received,
        buffer_size=session.buffer_size,
        required_window_steps=store.window_size,
        window_full=session.window_full,
        prediction_ready=session.prediction_ready(),
        latest=_normalized_model(session.latest) if session.latest else None,
    )


@app.post(
    "/telemetry/decision",
    response_model=TelemetryDecisionResponse,
    tags=["telemetry"],
    summary="Drive the existing decision pipeline from buffered REAL telemetry",
    responses={
        400: {"description": "Buffer not ready, or SINR unavailable"},
        404: {"description": "Unknown session"},
        409: {"description": "Session mode mismatch"},
    },
)
async def telemetry_decision(request: TelemetryDecisionRequest) -> TelemetryDecisionResponse:
    """Feed buffered REAL cellular telemetry into the EXISTING M4 orchestration.

    The flow itself lives in app/telemetry_flow.py so the WebSocket transport (M7)
    reuses it verbatim rather than duplicating decision logic.

    LIMITATION: real terrestrial RSRP/RSRQ distributions differ from the simulated
    distribution the model was trained on, so a merged-path prediction demonstrates
    the pipeline working end to end -- it is not a validated satellite-link forecast.
    """
    try:
        result = run_telemetry_decision(
            _state, request.session_id, request.mode, request.merge_real_radio_into
        )
    except FlowError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    return TelemetryDecisionResponse(
        decision=DecisionResponse(**decision_payload(result)),
        merged_path=result.merged_path,
        field_provenance=result.provenance,
        simulation_step=result.simulation_step,
        notes=result.notes,
    )


# ---------------------------------------------------------------------------
# M7 — WebSocket live telemetry -> decision
# ---------------------------------------------------------------------------
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """Live telemetry stream for one session.

    The connection binds to a single session_id and reuses the existing stateful
    TelemetryStore and DecisionEngine session, so hysteresis is preserved across
    messages and sessions never share state.
    """
    manager: ConnectionManager = _state.get("ws_manager")
    if manager is None:
        await websocket.close(code=1011)
        return
    await telemetry_websocket(websocket, session_id, _state, manager)


@app.get(
    "/sessions",
    tags=["telemetry"],
    summary="List active telemetry producer sessions",
)
async def list_sessions() -> dict:
    """Active producer sessions a dashboard can observe.

    This is how a dashboard finds the phone without hardcoding an id: the handset
    generates its own session id, the dashboard lists what is producing and
    subscribes to one. The two never need to agree on an id in advance.
    """
    store: TelemetryStore = _state.get("telemetry_store")
    hub: SessionHub = _state.get("hub")
    if store is None or hub is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    sessions = []
    for sid, session in store._sessions.items():  # noqa: SLF001 - same package, read-only
        latest = session.latest
        sessions.append({
            "session_id": sid,
            "samples_received": session.received,
            "buffer_size": session.buffer_size,
            "required_window_steps": store.window_size,
            "window_full": session.window_full,
            "prediction_ready": session.prediction_ready(),
            "observers": hub.observer_count(sid),
            "last_sample_timestamp": latest.timestamp if latest else None,
            "network_type": latest.network_type if latest else None,
            "sinr_source": latest.sinr_source if latest else None,
        })
    sessions.sort(key=lambda s: s["last_sample_timestamp"] or 0, reverse=True)
    return {"sessions": sessions, "count": len(sessions)}


@app.websocket("/ws/observe/{session_id}")
async def observer_endpoint(websocket: WebSocket, session_id: str) -> None:
    """Read-only live view of a producer session (used by the React dashboard)."""
    hub: SessionHub = _state.get("hub")
    if hub is None:
        await websocket.close(code=1011)
        return
    await observer_websocket(websocket, session_id, _state, hub)


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------
def _sanitise(value):
    """Recursively replace non-finite floats and non-serialisable objects.

    Pydantic echoes the offending input back in its error detail. When that input
    is NaN or Infinity, the default JSON encoder raises `ValueError: Out of range
    float values are not JSON compliant`, and the client receives an opaque 500
    instead of the 422 the validator correctly produced. Since rejecting non-finite
    telemetry is a documented requirement, the error path must survive it.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {k: _sanitise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitise(v) for v in value]
    if isinstance(value, BaseException):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        # Malformed-body errors echo the raw request bytes back.
        return value.decode("utf-8", errors="replace")
    return value


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return 422 with a JSON-safe error body, even for NaN/Inf inputs."""
    errors = []
    for err in exc.errors():
        item = dict(err)
        item.pop("ctx", None)          # may hold a raw exception object
        errors.append(_sanitise(item))
    logger.warning("Request validation failed: %d error(s)", len(errors))
    return JSONResponse(status_code=422, content={"detail": errors})


# ---------------------------------------------------------------------------
# Global exception handler for unhandled errors
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
