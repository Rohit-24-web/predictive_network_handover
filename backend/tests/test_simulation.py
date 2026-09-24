"""M3 simulator tests: reproducibility, scenario behaviour, and fairness.

The fairness tests matter most. A reactive/predictive comparison is only meaningful
if both modes saw identical conditions and reactive never touched the ML model.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pytest

from app import features as F
from app.decision import EngineConfig, Mode
from app.inference import Predictor
from app.simulation import SCENARIOS, generate_scenario
from app.simulation.metrics import compute_metrics
from app.simulation.runner import compute_risk_grid, is_degraded, run_experiment, run_mode

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ARTIFACTS = os.path.join(_BACKEND, "artifacts")

STEPS = 80          # small: these tests run the real model, ~1.8 ms per call


@pytest.fixture(scope="module")
def cfg() -> EngineConfig:
    return EngineConfig.from_json(os.path.join(_ARTIFACTS, "decision_engine_config.json"))


@pytest.fixture(scope="module")
def predictor() -> Predictor:
    return Predictor(_ARTIFACTS)


def make(scenario: str, seed: int = 7, steps: int = STEPS):
    return generate_scenario(scenario, seed, steps, scenario_fn=SCENARIOS[scenario])


# --- 15. reproducibility ------------------------------------------------------
def test_same_seed_gives_identical_telemetry():
    a, b = make("multi_path"), make("multi_path")
    assert a.digest() == b.digest()


def test_different_seed_gives_different_telemetry():
    assert make("multi_path", seed=1).digest() != make("multi_path", seed=2).digest()


def test_same_seed_gives_identical_decisions_and_metrics(cfg, predictor):
    outs = []
    for _ in range(2):
        rec = make("leo_degradation")
        res = run_experiment(rec, cfg, predictor)
        m = compute_metrics(res["predictive"], rec, cfg.degradation_tau, cfg.poor_quality)
        outs.append(([d.selected_path for d in res["predictive"].decisions], m.to_dict()))
    assert outs[0][0] == outs[1][0]
    assert outs[0][1] == outs[1][1]


# --- telemetry contract -------------------------------------------------------
def test_simulator_emits_all_required_fields_and_predictor_accepts_them(predictor):
    rec = make("normal")
    obs = rec.telemetry[rec.path_ids[0]][0]
    d = obs.to_telemetry_dict()
    assert set(d) == set(F.REQUIRED_RAW_FIELDS)
    assert all(np.isfinite(v) for v in d.values() if isinstance(v, float))
    # A generated window must be accepted by the REAL Predictor without raising.
    out = predictor.predict(rec.window(rec.path_ids[0], 40, predictor.cfg["required_window_steps"]))
    assert 0.0 <= out["probability"] <= 1.0
    assert out["model_version"] == "phase1-v1.0.0"


def test_generator_medians_are_in_the_phase1_distribution():
    """GEO must behave as a stable fallback, not a permanently broken path."""
    rec = make("normal", seed=3, steps=300)
    med = {p: np.median([o.packet_loss_pct for o in rec.telemetry[p]]) for p in rec.path_ids}
    lat = {p: np.median([o.latency_ms for o in rec.telemetry[p]]) for p in rec.path_ids}
    assert med["GEO-1"] < 5.0, f"GEO loss too high ({med['GEO-1']:.1f}%) — link budget wrong"
    assert lat["LEO-1"] < lat["MEO-1"] < lat["GEO-1"]


# --- 23. fairness -------------------------------------------------------------
def test_both_modes_see_byte_identical_telemetry(cfg, predictor):
    rec = make("multi_path")
    res = run_experiment(rec, cfg, predictor)
    assert res["reactive"].telemetry_digest == res["predictive"].telemetry_digest
    assert res["reactive"].telemetry_digest == rec.digest()


def test_reactive_never_calls_the_predictor(cfg, predictor):
    """THE fairness test: if reactive touched the model it would not be a baseline."""
    rec = make("leo_degradation")
    with patch.object(Predictor, "predict", side_effect=AssertionError(
            "reactive mode must not call the Predictor")) as spy:
        run_mode(rec, Mode.REACTIVE, cfg, risk_grid=None)
        assert spy.call_count == 0


def test_predictive_requires_a_risk_grid(cfg):
    with pytest.raises(ValueError, match="requires a risk grid"):
        run_mode(make("normal"), Mode.PREDICTIVE, cfg, risk_grid=None)


def test_risk_grid_covers_every_path_and_step(cfg, predictor):
    rec = make("normal")
    grid = compute_risk_grid(rec, predictor)
    assert set(grid) == set(rec.path_ids)
    for pid in rec.path_ids:
        assert len(grid[pid]) == rec.n_steps
        assert all(0.0 <= v <= 1.0 for v in grid[pid])


# --- handover cost ------------------------------------------------------------
def test_handover_interruption_is_applied_and_counted_as_degraded(cfg, predictor):
    rec = make("multi_path")
    res = run_experiment(rec, cfg, predictor)
    for run in res.values():
        switch_steps = [d.step for d in run.decisions if d.decision.value == "SWITCH"]
        for s in switch_steps:
            for k in range(s, min(s + cfg.ho_interrupt_steps, len(run.experienced))):
                e = run.experienced[k]
                assert e.interrupted and e.degraded
                assert e.throughput_mbps == 0.0 and e.packet_loss_pct == 100.0


def test_unavailable_path_is_never_selected(cfg, predictor):
    rec = make("multi_path")
    res = run_experiment(rec, cfg, predictor)
    for run in res.values():
        for e in run.experienced:
            if e.interrupted:
                continue
            assert rec.telemetry[e.path_id][e.step].available >= 0.5


# --- 16-22. scenarios ---------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_runs_and_produces_metrics(name, cfg, predictor):
    rec = make(name)
    res = run_experiment(rec, cfg, predictor)
    for mode, run in res.items():
        m = compute_metrics(run, rec, cfg.degradation_tau, cfg.poor_quality)
        assert m.steps == rec.n_steps - rec.warmup_steps
        assert m.total_handovers >= 0
        assert 0.0 <= m.degraded_time_pct <= 100.0
        assert m.final_path in rec.path_ids


def test_normal_scenario_is_stable(cfg, predictor):
    rec = make("normal", seed=11, steps=200)
    res = run_experiment(rec, cfg, predictor)
    m = compute_metrics(res["reactive"], rec, cfg.degradation_tau, cfg.poor_quality)
    assert m.total_handovers <= 3, "a stable scenario should not churn paths"


def _mean_quality(rec, path_id, sl):
    return float(np.mean([
        F.quality_from_metrics(o.sinr_db, o.packet_loss_pct, o.latency_ms,
                               o.throughput_mbps, o.orbit)
        for o in rec.telemetry[path_id][sl]
    ]))


@pytest.mark.parametrize("scenario,path", [
    ("leo_degradation", "LEO-1"),
    ("meo_degradation", "MEO-1"),
])
def test_degradation_scenario_lowers_that_path_versus_unperturbed(scenario, path):
    """Compare against the SAME seed with no perturbation, isolating the injection.

    An early-window baseline would be unsound: orbital geometry alone can leave a
    path below its elevation mask for a long stretch (MEO's 600 s pass period vs a
    300-step run), so 'start of run' is not a neutral reference. The scenario hooks
    consume no RNG, so `normal` and the perturbed scenario share identical latent
    state apart from the injected attenuation.
    """
    base = make("normal", steps=300)
    pert = make(scenario, steps=300)
    late = slice(-80, None)
    assert _mean_quality(pert, path, late) < _mean_quality(base, path, late), (
        f"{scenario} did not measurably degrade {path}"
    )
    # Other paths must be left alone, or the comparison would be confounded.
    for other in base.path_ids:
        if other != path:
            assert _mean_quality(pert, other, late) == pytest.approx(
                _mean_quality(base, other, late), abs=1e-9)


def test_geo_fallback_keeps_geo_available_but_slower():
    """'Available' does not mean 'optimal' — GEO stays up but at much higher RTT."""
    rec = make("geo_fallback", steps=300)
    assert np.mean([o.available for o in rec.telemetry["GEO-1"]]) == 1.0
    assert (np.median([o.latency_ms for o in rec.telemetry["GEO-1"]])
            > 3 * np.median([o.latency_ms for o in rec.telemetry["LEO-1"]]))


def test_congestion_raises_load_and_cuts_throughput():
    rec = make("congestion", steps=300)
    load = np.array([o.load for o in rec.telemetry["LEO-1"]])
    thr = np.array([o.throughput_mbps for o in rec.telemetry["LEO-1"]])
    mid = slice(len(load) // 3, 2 * len(load) // 3)
    assert load[mid].mean() > load[:len(load) // 6].mean()
    assert thr[mid].mean() < thr[:len(thr) // 6].mean()


def test_recovery_scenario_returns_to_health():
    rec = make("recovery", steps=300)
    q = [F.quality_from_metrics(o.sinr_db, o.packet_loss_pct, o.latency_ms,
                                o.throughput_mbps, o.orbit)
         for o in rec.telemetry["LEO-1"]]
    n = len(q)
    worst = min(q[n // 4: 2 * n // 3])
    assert np.mean(q[-40:]) > worst, "LEO should recover by the end"


def test_multi_path_has_more_than_one_best_path_over_time():
    """Genuine traffic steering needs the best path to change more than once."""
    rec = make("multi_path", steps=300)
    best = []
    for t in range(rec.n_steps):
        qs = {p: F.quality_from_metrics(o.sinr_db, o.packet_loss_pct, o.latency_ms,
                                        o.throughput_mbps, o.orbit)
              for p, o in rec.observations_at(t).items()}
        best.append(max(qs, key=qs.get))
    assert len(set(best)) >= 2, "multi_path scenario is not actually multi-path"


# --- warm-up ------------------------------------------------------------------
def test_warmup_is_excluded_from_metrics(cfg, predictor):
    rec = make("normal")
    res = run_experiment(rec, cfg, predictor)
    m = compute_metrics(res["reactive"], rec, cfg.degradation_tau, cfg.poor_quality)
    assert rec.warmup_steps >= 26
    assert m.steps == rec.n_steps - rec.warmup_steps
