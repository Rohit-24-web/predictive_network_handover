"""Replay harness: run reactive and predictive over an IDENTICAL scenario record."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app import features as F
from app.decision import DecisionEngine, DecisionRecord, EngineConfig, Mode
from app.simulation.network import ScenarioRecord


@dataclass
class ExperiencedStep:
    """Service actually delivered to the user at one step.

    During a handover the session is down: throughput 0, loss 100%, latency
    undefined, and the step counts as degraded. Without this cost switching would be
    free and the whole handover/degradation trade-off would vanish.
    """
    step: int
    path_id: str
    interrupted: bool
    degraded: bool
    latency_ms: Optional[float]
    packet_loss_pct: float
    throughput_mbps: float


@dataclass
class RunResult:
    mode: Mode
    scenario: str
    seed: int
    decisions: List[DecisionRecord]
    experienced: List[ExperiencedStep]
    risks: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    telemetry_digest: str = ""

    @property
    def final_path(self) -> str:
        return self.experienced[-1].path_id if self.experienced else ""


def is_degraded(obs, tau: float) -> bool:
    """Ground-truth degradation for one path at one step.

    Reuses features.degradation_score_from_metrics so the simulator, the engine and
    the Phase 1 label all share ONE definition.
    """
    if obs.available < 0.5:
        return True
    score = F.degradation_score_from_metrics(
        obs.sinr_db, obs.packet_loss_pct, obs.latency_ms, obs.throughput_mbps, obs.orbit
    )
    return score > tau


def compute_risk_grid(record: ScenarioRecord, predictor) -> Dict[str, List[float]]:
    """P(degradation) for EVERY candidate path at every step.

    Scoring all candidates -- not just the incumbent -- is what makes this traffic
    steering rather than plain handover. Each window is strictly backward-looking, so
    precomputing the whole grid introduces no lookahead.
    """
    size = predictor.cfg["required_window_steps"]
    grid: Dict[str, List[float]] = {}
    for pid in record.path_ids:
        out = []
        for t in range(record.n_steps):
            win = record.window(pid, t, size)
            out.append(predictor.predict(win)["probability"])
        grid[pid] = out
    return grid


def run_mode(record: ScenarioRecord, mode: Mode, cfg: EngineConfig,
             risk_grid: Optional[Dict[str, List[float]]] = None) -> RunResult:
    """Replay one scenario under one policy.

    Reactive mode receives NO risk information: risks are never passed to the engine,
    and DecisionEngine.step raises if they are. That is the fairness guarantee.
    """
    if mode is Mode.PREDICTIVE and risk_grid is None:
        raise ValueError("predictive mode requires a risk grid")

    engine = DecisionEngine(cfg, mode)
    engine.reset(record.observations_at(0))

    decisions: List[DecisionRecord] = []
    experienced: List[ExperiencedStep] = []

    for t in range(record.n_steps):
        obs = record.observations_at(t)
        risks = ({pid: risk_grid[pid][t] for pid in record.path_ids}
                 if mode is Mode.PREDICTIVE else None)

        rec = engine.step(t, float(t), obs, risks)
        decisions.append(rec)

        # Service experienced this step, on whichever path is now in use.
        active = engine.current_path
        if rec.handover or engine.is_interrupted:
            experienced.append(ExperiencedStep(
                step=t, path_id=active, interrupted=True, degraded=True,
                latency_ms=None, packet_loss_pct=100.0, throughput_mbps=0.0,
            ))
        else:
            o = obs[active]
            experienced.append(ExperiencedStep(
                step=t, path_id=active, interrupted=False,
                degraded=is_degraded(o, cfg.degradation_tau),
                latency_ms=o.latency_ms, packet_loss_pct=o.packet_loss_pct,
                throughput_mbps=o.throughput_mbps,
            ))

    return RunResult(
        mode=mode, scenario=record.name, seed=record.seed,
        decisions=decisions, experienced=experienced,
        risks=(risk_grid or {}), telemetry_digest=record.digest(),
    )


def run_experiment(record: ScenarioRecord, cfg: EngineConfig,
                   predictor) -> Dict[str, RunResult]:
    """Run BOTH policies over the same immutable record.

    The risk grid is computed once and used only by the predictive run.
    """
    risk_grid = compute_risk_grid(record, predictor)
    return {
        "reactive": run_mode(record, Mode.REACTIVE, cfg, risk_grid=None),
        "predictive": run_mode(record, Mode.PREDICTIVE, cfg, risk_grid=risk_grid),
    }
