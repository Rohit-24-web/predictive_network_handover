"""Deterministic decision engine.

Design rule (PHASE2_BRIEF §0.4): the ML model emits a probability; THIS module
decides. The engine never imports the Predictor. Reactive mode is enforced to be
ML-free by raising if risks are supplied.

Scoring weights note
--------------------
decision_engine_config.json holds only the hysteresis and risk parameters. The
scoring weights below are NOT in that file; they are the Phase 1 values, pinned
here as named constants so they cannot drift silently.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Dict, List, Optional

from app import features as F
from app.decision.models import (
    DecisionReason,
    DecisionRecord,
    DecisionType,
    Mode,
    PathObservation,
    PathScore,
)


@dataclass(frozen=True)
class EngineConfig:
    # --- from artifacts/decision_engine_config.json ---
    ho_interrupt_steps: int = 1
    min_dwell_steps: int = 8
    cooldown_steps: int = 5
    improvement_margin: float = 0.08
    sustained_poor_steps: int = 3
    switching_penalty: float = 0.05
    risk_weight: float = 0.35
    degradation_tau: float = 0.55

    # --- Phase 1 scoring weights (NOT in the JSON; see module docstring) ---
    w_quality: float = 0.40
    w_latency: float = 0.20
    w_throughput: float = 0.20
    w_load: float = 0.10
    lat_pref_scale: float = 200.0          # ms
    risk_alert_threshold: float = 0.40     # == Phase 1 decision_threshold

    # --- ablation only ---
    greedy_reevaluate: bool = False        # re-score every step, ignoring triggers

    @property
    def poor_quality(self) -> float:
        """Quality below this counts as 'poor'. Kept consistent with the Phase 1
        degradation label: poor_quality == 1 - degradation_tau."""
        return 1.0 - self.degradation_tau

    @classmethod
    def from_json(cls, path: str, **overrides) -> "EngineConfig":
        with open(path, "r") as f:
            raw = json.load(f)
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        return replace(cfg, **overrides) if overrides else cfg


ORBIT_CAPACITY = {o: p["capacity"] for o, p in F.ORBIT_PARAMS.items()}


def score_path(obs: PathObservation, is_incumbent: bool,
               risk: Optional[float], cfg: EngineConfig) -> PathScore:
    """Deterministic score for one candidate path. Higher is better.

        score = 0.40*quality + 0.20*latency_score + 0.20*tput_score - 0.10*load
                - switching_penalty (non-incumbent)
                - risk_weight * P(degradation)   [predictive only]

    Unavailable paths score -inf. `quality` is ORBIT-RELATIVE, which is what stops
    GEO being permanently penalised for its ~600 ms propagation floor.
    """
    quality = F.quality_from_metrics(
        obs.sinr_db, obs.packet_loss_pct, obs.latency_ms,
        obs.throughput_mbps, obs.orbit,
    )
    latency_score = 1.0 / (1.0 + obs.latency_ms / cfg.lat_pref_scale)
    capacity = ORBIT_CAPACITY.get(obs.orbit, 1.0)
    tput_score = obs.throughput_mbps / capacity if capacity else 0.0

    load_penalty = cfg.w_load * obs.load
    switch_pen = 0.0 if is_incumbent else cfg.switching_penalty
    risk_pen = cfg.risk_weight * risk if risk is not None else 0.0

    if not obs.is_available:
        score = -math.inf
    else:
        score = (cfg.w_quality * quality
                 + cfg.w_latency * latency_score
                 + cfg.w_throughput * tput_score
                 - load_penalty
                 - switch_pen
                 - risk_pen)

    return PathScore(
        path_id=obs.path_id, orbit=obs.orbit, available=obs.is_available,
        score=score, quality=quality, latency_score=latency_score,
        tput_score=tput_score, load_penalty=load_penalty,
        switching_penalty=switch_pen, risk_penalty=risk_pen, risk=risk,
    )


class DecisionEngine:
    """Stateful handover policy. One instance per simulated user/session."""

    def __init__(self, cfg: EngineConfig, mode: Mode) -> None:
        self.cfg = cfg
        self.mode = mode
        self.current_path: Optional[str] = None
        self.dwell: int = cfg.min_dwell_steps    # allow an early switch if needed
        self.cooldown: int = 0
        self.poor_streak: int = 0
        self.interrupt_left: int = 0

    # -- initialisation ----------------------------------------------------
    def reset(self, observations: Dict[str, PathObservation]) -> str:
        """Choose the starting path WITHOUT the risk term.

        Both modes must start identically, or predictive gets a head start and the
        comparison is invalid.
        """
        scores = [score_path(o, is_incumbent=False, risk=None, cfg=self.cfg)
                  for o in observations.values()]
        best = max(scores, key=lambda s: s.score)
        if not math.isfinite(best.score):
            best = max(scores, key=lambda s: (s.available, s.quality))
        self.current_path = best.path_id
        self.dwell = self.cfg.min_dwell_steps
        self.cooldown = 0
        self.poor_streak = 0
        self.interrupt_left = 0
        return self.current_path

    @property
    def is_interrupted(self) -> bool:
        return self.interrupt_left > 0

    # -- one timestep ------------------------------------------------------
    def step(self, step_idx: int, timestamp: float,
             observations: Dict[str, PathObservation],
             risks: Optional[Dict[str, float]] = None) -> DecisionRecord:
        """Advance one timestep and return an explainable decision."""
        if self.mode is Mode.REACTIVE and risks is not None:
            raise ValueError(
                "reactive mode must not receive degradation probabilities; "
                "this guarantees the reactive baseline is ML-free"
            )
        if self.current_path is None:
            self.reset(observations)

        use_risk = self.mode is Mode.PREDICTIVE and risks is not None
        incumbent = observations[self.current_path]
        incumbent_risk = risks.get(self.current_path) if use_risk else None

        scores = [
            score_path(o, is_incumbent=(pid == self.current_path),
                       risk=(risks.get(pid) if use_risk else None), cfg=self.cfg)
            for pid, o in observations.items()
        ]
        by_id = {s.path_id: s for s in scores}

        def record(dec: DecisionType, reason: DecisionReason,
                   selected: str, handover: bool) -> DecisionRecord:
            return DecisionRecord(
                step=step_idx, timestamp=timestamp, mode=self.mode,
                current_path=self.current_path, selected_path=selected,
                decision=dec, reason=reason,
                degradation_probability=incumbent_risk,
                handover=handover, scores=scores,
            )

        # --- mid-handover: service is interrupted, no new decision ---
        if self.interrupt_left > 0:
            self.interrupt_left -= 1
            self.dwell += 1
            self.cooldown = max(0, self.cooldown - 1)
            return record(DecisionType.STAY, DecisionReason.HANDOVER_IN_PROGRESS,
                          self.current_path, False)

        # --- incumbent health ---
        inc_quality = by_id[self.current_path].quality
        if (not incumbent.is_available) or inc_quality < self.cfg.poor_quality:
            self.poor_streak += 1
        else:
            self.poor_streak = 0

        alert = bool(use_risk and incumbent_risk is not None
                     and incumbent_risk >= self.cfg.risk_alert_threshold)

        trigger = ((not incumbent.is_available)
                   or self.poor_streak >= self.cfg.sustained_poor_steps)
        if use_risk:
            trigger = trigger or alert            # anticipate, before quality drops
        if self.cfg.greedy_reevaluate:
            trigger = True                        # ablation only

        def advance(dec, reason, selected, handover):
            self.dwell += 1
            self.cooldown = max(0, self.cooldown - 1)
            return record(dec, reason, selected, handover)

        if not trigger:
            return advance(DecisionType.STAY, DecisionReason.STAY_CURRENT_PATH,
                           self.current_path, False)
        if self.dwell < self.cfg.min_dwell_steps:
            return advance(DecisionType.STAY, DecisionReason.MIN_DWELL_ACTIVE,
                           self.current_path, False)
        if self.cooldown > 0:
            return advance(DecisionType.STAY, DecisionReason.COOLDOWN_ACTIVE,
                           self.current_path, False)

        alternatives = [s for s in scores
                        if s.path_id != self.current_path and math.isfinite(s.score)]
        if not alternatives:
            return advance(DecisionType.STAY, DecisionReason.CANDIDATE_UNAVAILABLE,
                           self.current_path, False)

        best = max(alternatives, key=lambda s: s.score)
        incumbent_score = by_id[self.current_path].score
        # An unavailable incumbent scores -inf, so any finite candidate beats it.
        delta = best.score - incumbent_score
        if math.isfinite(incumbent_score) and delta < self.cfg.improvement_margin:
            return advance(DecisionType.STAY, DecisionReason.INSUFFICIENT_IMPROVEMENT,
                           self.current_path, False)

        # --- commit the switch ---
        if (not incumbent.is_available) or inc_quality < self.cfg.poor_quality:
            reason = DecisionReason.CURRENT_PATH_DEGRADED
        elif alert:
            reason = DecisionReason.PREDICTED_DEGRADATION
        else:
            reason = DecisionReason.SWITCH_TO_BETTER_PATH

        rec = record(DecisionType.SWITCH, reason, best.path_id, True)
        self.current_path = best.path_id
        self.dwell = 0
        self.cooldown = self.cfg.cooldown_steps
        self.interrupt_left = self.cfg.ho_interrupt_steps
        self.poor_streak = 0
        return rec
