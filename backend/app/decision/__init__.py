"""Deterministic network-selection decision engine.

Deliberately independent of FastAPI and of the ML layer: the engine consumes a
degradation probability, it never computes one. That separation is what makes the
engine unit-testable and every decision auditable.
"""
from app.decision.models import (
    DecisionReason,
    DecisionRecord,
    DecisionType,
    Mode,
    PathObservation,
    PathScore,
)
from app.decision.engine import DecisionEngine, EngineConfig, score_path

__all__ = [
    "DecisionEngine", "EngineConfig", "score_path",
    "PathObservation", "PathScore", "DecisionRecord",
    "DecisionType", "DecisionReason", "Mode",
]
