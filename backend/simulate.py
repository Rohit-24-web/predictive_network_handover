#!/usr/bin/env python3
"""Command-line demo for the M3 reactive vs predictive experiment.

  python simulate.py --mode predictive --scenario leo_degradation
  python simulate.py --compare --scenario multi_path --seeds 30
  python simulate.py --compare --all-scenarios --seeds 30

All network paths are SIMULATED. No real satellite measurements are involved.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List

import numpy as np

from app.config import ARTIFACT_DIR
from app.decision import EngineConfig, Mode
from app.inference import Predictor
from app.simulation import generate_scenario, SCENARIOS
from app.simulation.scenarios import SCENARIO_LABELS
from app.simulation.metrics import compare, compute_metrics
from app.simulation.runner import compute_risk_grid, run_experiment, run_mode

BAR = "=" * 56


def _fmt(v, nd=2, dash="n/a"):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return dash
    return f"{v:.{nd}f}"


def run_single(scenario: str, mode: str, seed: int, steps: int,
               cfg: EngineConfig, predictor: Predictor) -> None:
    record = generate_scenario(scenario, seed, steps, scenario_fn=SCENARIOS[scenario])
    m = Mode(mode)
    risk = compute_risk_grid(record, predictor) if m is Mode.PREDICTIVE else None
    result = run_mode(record, m, cfg, risk_grid=risk)
    met = compute_metrics(result, record, cfg.degradation_tau, cfg.poor_quality)

    print(BAR)
    print("Predictive Network Handover Simulation")
    print(BAR)
    print(f"\nMode:     {mode.capitalize()}")
    print(f"Scenario: {SCENARIO_LABELS[scenario]}")
    print(f"Seed:     {seed}   (network paths are SIMULATED)\n")
    print(f"Total steps:            {met.steps}  (warm-up {record.warmup_steps} excluded)")
    print(f"Total handovers:        {met.total_handovers}")
    print(f"Unnecessary handovers:  {met.unnecessary_handovers}")
    print(f"Degraded time:          {_fmt(met.degraded_time_pct)}%")
    print(f"Mean outage:            {_fmt(met.mean_outage)} s")
    print(f"Max outage:             {met.max_outage} s")
    print(f"Average latency:        {_fmt(met.avg_latency_ms, 1)} ms")
    print(f"Average throughput:     {_fmt(met.avg_throughput_mbps, 1)} Mbps")
    print(f"Average packet loss:    {_fmt(met.avg_packet_loss_pct)}%")
    if m is Mode.PREDICTIVE:
        print(f"Prediction lead time:   {_fmt(met.prediction_lead_time)} s"
              f"  ({met.anticipatory_alerts} anticipatory alerts)")
    else:
        print("Prediction lead time:   n/a (reactive mode uses no prediction)")
    print(f"\nFinal selected path:    {met.final_path}")


def run_compare(scenarios: List[str], seeds: int, steps: int,
                cfg: EngineConfig, predictor: Predictor, as_json: bool) -> None:
    all_out = {}
    for scenario in scenarios:
        react, pred = [], []
        for s in range(seeds):
            record = generate_scenario(scenario, 1000 + s, steps,
                                       scenario_fn=SCENARIOS[scenario])
            res = run_experiment(record, cfg, predictor)
            assert res["reactive"].telemetry_digest == res["predictive"].telemetry_digest
            react.append(compute_metrics(res["reactive"], record,
                                         cfg.degradation_tau, cfg.poor_quality))
            pred.append(compute_metrics(res["predictive"], record,
                                        cfg.degradation_tau, cfg.poor_quality))
        cmp_ = compare(react, pred)
        all_out[scenario] = cmp_

        if not as_json:
            print(f"\n{BAR}\n{SCENARIO_LABELS[scenario]}  "
                  f"({seeds} seeds x {steps} steps)\n{BAR}")
            print(f"{'metric':<24}{'reactive':>12}{'predictive':>13}{'delta':>10}")
            for f, v in cmp_["per_metric"].items():
                print(f"{f:<24}{v['reactive_mean']:>12.2f}"
                      f"{v['predictive_mean']:>13.2f}{v['delta_mean']:>+10.2f}")
            print(f"\ndegraded-time outcome: predictive better on "
                  f"{cmp_['degraded_time_wins']}, tied {cmp_['degraded_time_ties']}, "
                  f"WORSE on {cmp_['degraded_time_losses']} of {seeds} seeds")
            lt = cmp_["mean_prediction_lead_time"]
            print(f"mean prediction lead time: {_fmt(lt)} s")

    if as_json:
        print(json.dumps(all_out, indent=2, default=float))


def main() -> int:
    ap = argparse.ArgumentParser(description="M3 network handover simulation (SIMULATED paths)")
    ap.add_argument("--mode", choices=["reactive", "predictive"], default="predictive")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="leo_degradation")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--compare", action="store_true",
                    help="run both modes over identical scenarios and compare")
    ap.add_argument("--all-scenarios", action="store_true")
    ap.add_argument("--seeds", type=int, default=30,
                    help="number of independent seeds for --compare")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = EngineConfig.from_json(f"{ARTIFACT_DIR}/decision_engine_config.json")
    predictor = Predictor(ARTIFACT_DIR)

    if args.compare:
        scenarios = sorted(SCENARIOS) if args.all_scenarios else [args.scenario]
        run_compare(scenarios, args.seeds, args.steps, cfg, predictor, args.json)
    else:
        run_single(args.scenario, args.mode, args.seed, args.steps, cfg, predictor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
