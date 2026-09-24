"""M4 integration layer: telemetry -> Predictor -> DecisionEngine -> decision.

This module OWNS NO ALGORITHMS. It wires together components that already exist and
are already tested:

    features.py      feature engineering      (M1, unchanged)
    inference.py     Predictor                (M1, unchanged)
    decision/        DecisionEngine           (M3, unchanged)
    simulation/      ScenarioRecord           (M3, unchanged)

Two things make this layer non-trivial, and they are the whole reason it exists:

1. **The engine is stateful, HTTP is not.** DecisionEngine tracks dwell, cooldown,
   poor-streak and handover interruption across timesteps. A bare request/response
   endpoint would reset that state every call, silently disabling hysteresis. So
   sessions are kept here, keyed by caller-supplied session_id.

2. **Predictive mode needs a risk per CANDIDATE, not just the incumbent.** Scoring
   every candidate is what makes this traffic steering rather than plain handover,
   and it is the Phase 1 / M3 semantics. One Predictor call per candidate window.

Reactive mode never touches the Predictor. That is enforced twice: this layer does
not call it, and DecisionEngine.step raises if risks are supplied in reactive mode.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.decision import (
    DecisionEngine,
    DecisionRecord,
    EngineConfig,
    Mode,
    PathObservation,
)

DEFAULT_SESSION_TTL_S = 900.0
MAX_SESSIONS = 512


class SessionModeMismatch(ValueError):
    """Raised when a session created in one mode is reused in the other."""


@dataclass
class Session:
    """Per-caller engine state, so hysteresis survives across HTTP requests."""
    session_id: str
    mode: Mode
    engine: DecisionEngine
    step: int = 0
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    """In-memory session registry with TTL eviction.

    Deliberately a plain dict: M4 forbids Redis/DB, and a single backend process is
    all this milestone needs. Swapping in a shared store later touches only this class.
    """

    def __init__(self, ttl_s: float = DEFAULT_SESSION_TTL_S,
                 max_sessions: int = MAX_SESSIONS) -> None:
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: Dict[str, Session] = {}

    def _evict(self) -> None:
        now = time.time()
        stale = [k for k, s in self._sessions.items() if now - s.last_seen > self.ttl_s]
        for k in stale:
            del self._sessions[k]
        if len(self._sessions) > self.max_sessions:
            for k, _ in sorted(self._sessions.items(), key=lambda kv: kv[1].last_seen)[
                    : len(self._sessions) - self.max_sessions]:
                del self._sessions[k]

    def get_or_create(self, session_id: str, mode: Mode, cfg: EngineConfig,
                      observations: Dict[str, PathObservation],
                      current_path: Optional[str]) -> Session:
        self._evict()
        s = self._sessions.get(session_id)
        if s is not None:
            if s.mode is not mode:
                raise SessionModeMismatch(
                    f"session {session_id!r} was created in {s.mode.value} mode; "
                    f"start a new session to switch to {mode.value}"
                )
            s.last_seen = time.time()
            if current_path is not None and current_path != s.engine.current_path:
                s.engine.current_path = current_path   # caller is authoritative
            return s

        engine = DecisionEngine(cfg, mode)
        engine.reset(observations)                     # risk-free initial pick
        if current_path is not None:
            engine.current_path = current_path
        s = Session(session_id=session_id, mode=mode, engine=engine)
        self._sessions[session_id] = s
        return s

    def drop(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def __len__(self) -> int:
        return len(self._sessions)


@dataclass
class DecisionOutcome:
    """What the orchestrator produces: an engine decision plus predictive context."""
    record: DecisionRecord
    session_id: str
    risks: Optional[Dict[str, float]]
    model_version: Optional[str]
    horizon_seconds: Optional[int]
    warming_up: Dict[str, bool]


class HandoverOrchestrator:
    """Wires telemetry -> (optional) risk -> decision. Holds no algorithm of its own."""

    def __init__(self, predictor, cfg: EngineConfig,
                 store: Optional[SessionStore] = None) -> None:
        self.predictor = predictor
        self.cfg = cfg
        # NOTE: `store or SessionStore()` would be WRONG here. SessionStore defines
        # __len__, so an empty store is falsy and a caller-injected store would be
        # silently discarded and replaced by a default one -- breaking dependency
        # injection in exactly the case (a fresh store) that callers always pass.
        self.sessions = SessionStore() if store is None else store

    # -- risk ---------------------------------------------------------------
    def compute_risks(self, windows: Dict[str, List[Dict[str, Any]]]
                      ) -> Tuple[Dict[str, float], Dict[str, bool]]:
        """P(degradation) for EVERY candidate path.

        Returns (risks, warming_up). `warming_up` flags paths whose window is shorter
        than required_window_steps: features.py edge-pads those (matching M2 and Phase 1
        cold-start behaviour), so the prediction is valid but lower-confidence, and the
        caller is told rather than left to assume.
        """
        need = int(self.predictor.cfg["required_window_steps"])
        risks: Dict[str, float] = {}
        warming: Dict[str, bool] = {}
        for path_id, window in windows.items():
            risks[path_id] = float(self.predictor.predict(window)["probability"])
            warming[path_id] = len(window) < need
        return risks, warming

    # -- one decision -------------------------------------------------------
    def decide(self, mode: Mode,
               observations: Dict[str, PathObservation],
               windows: Dict[str, List[Dict[str, Any]]],
               session_id: str,
               current_path: Optional[str] = None,
               timestamp: Optional[float] = None) -> DecisionOutcome:
        """Run one integrated decision step.

        Reactive mode: the Predictor is never called and `risks` stays None.
        Predictive mode: one Predictor call per candidate path, then the engine scores
        every candidate with its own risk term.
        """
        if current_path is not None and current_path not in observations:
            raise ValueError(
                f"current_path {current_path!r} is not among the candidates "
                f"{sorted(observations)}"
            )

        session = self.sessions.get_or_create(
            session_id, mode, self.cfg, observations, current_path
        )

        risks: Optional[Dict[str, float]] = None
        warming: Dict[str, bool] = {p: False for p in observations}
        model_version = horizon = None

        if mode is Mode.PREDICTIVE:
            missing = [p for p in observations if p not in windows]
            if missing:
                raise ValueError(
                    f"predictive mode needs a telemetry window for every candidate; "
                    f"missing: {sorted(missing)}"
                )
            risks, warming = self.compute_risks(windows)
            model_version = self.predictor.cfg["model_version"]
            horizon = int(self.predictor.cfg["prediction_horizon_s"])

        ts = timestamp if timestamp is not None else float(session.step)
        record = session.engine.step(session.step, ts, observations, risks)
        session.step += 1
        session.last_seen = time.time()

        return DecisionOutcome(
            record=record, session_id=session_id, risks=risks,
            model_version=model_version, horizon_seconds=horizon,
            warming_up=warming,
        )


# ---------------------------------------------------------------------------
# Simulator bridge — a thin read-only helper, NOT a reimplementation.
# ---------------------------------------------------------------------------
def scenario_step_inputs(record, t: int, window_size: int
                         ) -> Tuple[Dict[str, PathObservation], Dict[str, List[Dict[str, Any]]]]:
    """Extract (observations, windows) for timestep t from an existing ScenarioRecord.

    Lets the M3 simulator drive the M4 orchestrator without either side knowing about
    the other. Uses only ScenarioRecord's public accessors, so the simulator's
    immutability and fairness properties are untouched.
    """
    observations = record.observations_at(t)
    windows = {pid: record.window(pid, t, window_size) for pid in record.path_ids}
    return observations, windows
