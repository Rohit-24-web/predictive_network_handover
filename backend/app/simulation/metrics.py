"""Metric definitions for the reactive vs predictive experiment.

Definitions are pinned here so they cannot drift between runs or milestones.
Warm-up steps are excluded from every metric, for both modes equally: the first
`warmup_steps` windows are edge-padded and their predictions are unreliable.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import numpy as np

from app.decision.models import DecisionType
from app.simulation.network import ScenarioRecord
from app.simulation.runner import RunResult, is_degraded

HORIZON = 5   # prediction horizon, seconds/steps — matches Phase 1


@dataclass
class RunMetrics:
    mode: str
    scenario: str
    seed: int
    steps: int
    total_handovers: int
    unnecessary_handovers: int
    degraded_time_pct: float
    mean_outage: float
    max_outage: int
    n_outages: int
    avg_latency_ms: float
    avg_throughput_mbps: float
    avg_packet_loss_pct: float
    recovery_time: Optional[float]
    prediction_lead_time: Optional[float]
    anticipatory_alerts: int
    final_path: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _outage_runs(flags: List[bool]) -> List[int]:
    runs, cur = [], 0
    for f in flags:
        if f:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


def compute_metrics(result: RunResult, record: ScenarioRecord,
                    tau: float, poor_quality: float) -> RunMetrics:
    w = record.warmup_steps
    exp = result.experienced[w:]
    dec = result.decisions[w:]

    degraded = [e.degraded for e in exp]
    runs = _outage_runs(degraded)

    lat = [e.latency_ms for e in exp if e.latency_ms is not None]

    # --- handovers ---
    switches = [d for d in dec if d.decision is DecisionType.SWITCH]
    unnecessary = 0
    for d in switches:
        left = d.current_path
        window = record.telemetry[left][d.step: min(d.step + HORIZON + 1, record.n_steps)]
        if window and not any(is_degraded(o, tau) for o in window):
            unnecessary += 1

    # --- prediction lead time (predictive only) ---
    lead_times: List[int] = []
    anticipatory = 0
    if result.mode.value == "predictive" and result.risks:
        for d in dec:
            pid = d.current_path
            p = result.risks[pid][d.step]
            if p is None or p < 0.40:
                continue
            now = record.telemetry[pid][d.step]
            if is_degraded(now, tau):
                continue                      # already degraded: not anticipatory
            fut = record.telemetry[pid][d.step + 1: min(d.step + 1 + HORIZON,
                                                        record.n_steps)]
            hit = [i for i, o in enumerate(fut) if is_degraded(o, tau)]
            anticipatory += 1
            if hit:
                lead_times.append(hit[0] + 1)

    # --- recovery time: steps from a non-incumbent path becoming healthy again
    #     until the engine actually selects it. Often dominated by dwell/cooldown.
    recovery: Optional[float] = None
    rec_samples: List[int] = []
    for d in switches:
        pid = d.selected_path
        healthy_since = None
        for k in range(d.step, -1, -1):
            if is_degraded(record.telemetry[pid][k], tau):
                healthy_since = k + 1
                break
        if healthy_since is not None and healthy_since <= d.step:
            rec_samples.append(d.step - healthy_since)
    if rec_samples:
        recovery = float(np.mean(rec_samples))

    return RunMetrics(
        mode=result.mode.value, scenario=result.scenario, seed=result.seed,
        steps=len(exp),
        total_handovers=len(switches),
        unnecessary_handovers=unnecessary,
        degraded_time_pct=100.0 * float(np.mean(degraded)) if degraded else 0.0,
        mean_outage=float(np.mean(runs)) if runs else 0.0,
        max_outage=int(np.max(runs)) if runs else 0,
        n_outages=len(runs),
        avg_latency_ms=float(np.mean(lat)) if lat else float("nan"),
        avg_throughput_mbps=float(np.mean([e.throughput_mbps for e in exp])),
        avg_packet_loss_pct=float(np.mean([e.packet_loss_pct for e in exp])),
        recovery_time=recovery,
        prediction_lead_time=float(np.mean(lead_times)) if lead_times else None,
        anticipatory_alerts=anticipatory,
        final_path=result.final_path,
    )


def compare(reactive: List[RunMetrics], predictive: List[RunMetrics]) -> Dict[str, Any]:
    """Paired per-seed comparison. Means alone hide that seeds differ in difficulty."""
    assert len(reactive) == len(predictive), "paired comparison needs equal run counts"
    fields = ["degraded_time_pct", "total_handovers", "unnecessary_handovers",
              "mean_outage", "max_outage", "avg_latency_ms",
              "avg_throughput_mbps", "avg_packet_loss_pct"]
    out: Dict[str, Any] = {"n_seeds": len(reactive), "per_metric": {}}
    for f in fields:
        r = np.array([getattr(m, f) for m in reactive], dtype=float)
        p = np.array([getattr(m, f) for m in predictive], dtype=float)
        d = p - r
        out["per_metric"][f] = {
            "reactive_mean": float(np.nanmean(r)),
            "reactive_std": float(np.nanstd(r)),
            "predictive_mean": float(np.nanmean(p)),
            "predictive_std": float(np.nanstd(p)),
            "delta_mean": float(np.nanmean(d)),
            "delta_std": float(np.nanstd(d)),
        }
    dt = np.array([m.degraded_time_pct for m in predictive]) - \
        np.array([m.degraded_time_pct for m in reactive])
    out["degraded_time_wins"] = int((dt < 0).sum())
    out["degraded_time_ties"] = int((dt == 0).sum())
    out["degraded_time_losses"] = int((dt > 0).sum())
    leads = [m.prediction_lead_time for m in predictive if m.prediction_lead_time]
    out["mean_prediction_lead_time"] = float(np.mean(leads)) if leads else None
    return out
