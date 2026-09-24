"""M3 decision-engine unit tests. No FastAPI, no ML — the engine must stand alone."""
from __future__ import annotations

import math
import os

import pytest

from app.decision import (
    DecisionEngine, DecisionReason, DecisionType, EngineConfig, Mode,
    PathObservation, score_path,
)

_ARTIFACTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "artifacts")


@pytest.fixture(scope="module")
def cfg() -> EngineConfig:
    return EngineConfig.from_json(os.path.join(_ARTIFACTS, "decision_engine_config.json"))


def mk(path_id="LEO-1", orbit="LEO", *, sinr=18.0, loss=0.2, lat=60.0,
       thr=110.0, load=0.4, available=1.0, t=0.0) -> PathObservation:
    """Build a synthetic observation with all 15 required telemetry fields."""
    return PathObservation(
        path_id=path_id, timestamp=t, orbit=orbit, rsrp_dbm=-82.0, rsrq_db=-6.0,
        sinr_db=sinr, rssi_dbm=-64.0, elevation_deg=55.0, latency_ms=lat,
        jitter_ms=8.0, packet_loss_pct=loss, throughput_mbps=thr, load=load,
        traffic_demand=0.4, env_index=0.1, available=available,
    )


GOOD_LEO = dict(path_id="LEO-1", orbit="LEO", sinr=18.0, loss=0.2, lat=60.0, thr=115.0)
GOOD_MEO = dict(path_id="MEO-1", orbit="MEO", sinr=17.0, loss=0.3, lat=150.0, thr=85.0)
GOOD_GEO = dict(path_id="GEO-1", orbit="GEO", sinr=16.0, loss=0.3, lat=615.0, thr=75.0)
BAD = dict(sinr=1.0, loss=45.0, lat=400.0, thr=5.0)


def three(leo=None, meo=None, geo=None):
    return {
        "LEO-1": mk(**{**GOOD_LEO, **(leo or {})}),
        "MEO-1": mk(**{**GOOD_MEO, **(meo or {})}),
        "GEO-1": mk(**{**GOOD_GEO, **(geo or {})}),
    }


# --- 1. deterministic scoring -------------------------------------------------
def test_scoring_is_deterministic(cfg):
    o = mk()
    a = score_path(o, False, 0.3, cfg)
    b = score_path(o, False, 0.3, cfg)
    assert a.score == b.score
    assert a == b


def test_scoring_formula_matches_phase1(cfg):
    """Score must equal the documented Phase 1 weighted sum, exactly."""
    from app import features as F
    o = mk(**GOOD_MEO)
    s = score_path(o, is_incumbent=False, risk=0.5, cfg=cfg)
    q = F.quality_from_metrics(o.sinr_db, o.packet_loss_pct, o.latency_ms,
                               o.throughput_mbps, o.orbit)
    expected = (0.40 * q
                + 0.20 * (1.0 / (1.0 + o.latency_ms / 200.0))
                + 0.20 * (o.throughput_mbps / 140.0)
                - 0.10 * o.load
                - cfg.switching_penalty
                - cfg.risk_weight * 0.5)
    assert s.score == pytest.approx(expected, abs=1e-12)


# --- 2. best candidate / 10. unavailable --------------------------------------
def test_unavailable_path_scores_neg_inf_and_is_never_selected(cfg):
    obs = three(leo=dict(available=0.0))
    eng = DecisionEngine(cfg, Mode.REACTIVE)
    eng.reset(obs)
    assert eng.current_path != "LEO-1"
    s = score_path(obs["LEO-1"], False, None, cfg)
    assert s.score == -math.inf


def test_reset_picks_best_available(cfg):
    eng = DecisionEngine(cfg, Mode.REACTIVE)
    assert eng.reset(three()) == "LEO-1"      # lowest latency, highest throughput


# --- 3/4. reactive vs predictive ---------------------------------------------
def test_reactive_rejects_risk_information(cfg):
    """The fairness guarantee: reactive mode must never see a probability."""
    eng = DecisionEngine(cfg, Mode.REACTIVE)
    obs = three()
    eng.reset(obs)
    with pytest.raises(ValueError, match="must not receive"):
        eng.step(0, 0.0, obs, risks={"LEO-1": 0.9, "MEO-1": 0.1, "GEO-1": 0.1})


def test_predictive_switches_on_risk_while_quality_still_good(cfg):
    """The whole point of predictive mode: act BEFORE quality drops."""
    eng = DecisionEngine(cfg, Mode.PREDICTIVE)
    obs = three()
    eng.reset(obs)
    assert eng.current_path == "LEO-1"
    rec = eng.step(0, 0.0, obs, risks={"LEO-1": 0.95, "MEO-1": 0.01, "GEO-1": 0.01})
    assert rec.decision is DecisionType.SWITCH
    assert rec.reason is DecisionReason.PREDICTED_DEGRADATION
    assert rec.degradation_probability == 0.95


def test_reactive_stays_when_quality_good(cfg):
    eng = DecisionEngine(cfg, Mode.REACTIVE)
    obs = three()
    eng.reset(obs)
    rec = eng.step(0, 0.0, obs)
    assert rec.decision is DecisionType.STAY
    assert rec.reason is DecisionReason.STAY_CURRENT_PATH


# --- 5. minimum dwell ---------------------------------------------------------
def test_min_dwell_blocks_immediate_second_switch(cfg):
    eng = DecisionEngine(cfg, Mode.PREDICTIVE)
    obs = three()
    eng.reset(obs)
    r1 = eng.step(0, 0.0, obs, risks={"LEO-1": 0.99, "MEO-1": 0.0, "GEO-1": 0.0})
    assert r1.decision is DecisionType.SWITCH
    reasons = []
    for t in range(1, 6):
        r = eng.step(t, float(t), obs, risks={"LEO-1": 0.0, "MEO-1": 0.99, "GEO-1": 0.0})
        reasons.append(r.reason)
        assert r.decision is DecisionType.STAY
    assert DecisionReason.HANDOVER_IN_PROGRESS in reasons
    assert {DecisionReason.MIN_DWELL_ACTIVE, DecisionReason.COOLDOWN_ACTIVE} & set(reasons)


# --- 6. cooldown --------------------------------------------------------------
def test_cooldown_active_after_dwell_satisfied(cfg):
    c = EngineConfig.from_json(os.path.join(_ARTIFACTS, "decision_engine_config.json"),
                               min_dwell_steps=0)
    eng = DecisionEngine(c, Mode.PREDICTIVE)
    obs = three()
    eng.reset(obs)
    eng.step(0, 0.0, obs, risks={"LEO-1": 0.99, "MEO-1": 0.0, "GEO-1": 0.0})
    assert eng.current_path == "MEO-1"
    # Keep the incumbent alerting so a trigger fires every step; only the cooldown
    # should be standing in the way now that min_dwell is 0.
    seen = set()
    for t in range(1, 5):
        seen.add(eng.step(t, float(t), obs,
                          risks={"LEO-1": 0.0, "MEO-1": 0.99, "GEO-1": 0.0}).reason)
    assert DecisionReason.COOLDOWN_ACTIVE in seen


# --- 7/11/12. improvement margin ---------------------------------------------
def test_insufficient_improvement_blocks_switch(cfg):
    """Incumbent degraded but the alternative is barely better -> no switch."""
    obs = three(leo=dict(sinr=6.0, loss=6.0, lat=110.0, thr=40.0),
                meo=dict(sinr=6.2, loss=5.8, lat=340.0, thr=32.0),
                geo=dict(sinr=5.9, loss=6.1, lat=760.0, thr=26.0))
    eng = DecisionEngine(cfg, Mode.REACTIVE)
    eng.reset(obs)
    last = None
    for t in range(cfg.sustained_poor_steps + 2):
        last = eng.step(t, float(t), obs)
    assert last.decision is DecisionType.STAY
    assert last.reason is DecisionReason.INSUFFICIENT_IMPROVEMENT


def test_sufficient_improvement_allows_switch(cfg):
    obs = three(leo=dict(**BAD))
    eng = DecisionEngine(cfg, Mode.REACTIVE)
    assert eng.reset(three()) == "LEO-1"    # start healthy on LEO
    # The switch fires as soon as the poor streak is satisfied, which may be before
    # the final step, so inspect the whole trace rather than only the last record.
    recs = [eng.step(t, float(t), obs) for t in range(cfg.sustained_poor_steps + 2)]
    switches = [r for r in recs if r.decision is DecisionType.SWITCH]
    assert len(switches) == 1, f"expected exactly one switch, got {len(switches)}"
    assert switches[0].reason is DecisionReason.CURRENT_PATH_DEGRADED
    assert switches[0].selected_path == "MEO-1"
    assert eng.current_path == "MEO-1"


# --- 8. switching penalty -----------------------------------------------------
def test_switching_penalty_applies_only_to_challengers(cfg):
    o = mk()
    inc = score_path(o, is_incumbent=True, risk=None, cfg=cfg)
    ch = score_path(o, is_incumbent=False, risk=None, cfg=cfg)
    assert inc.switching_penalty == 0.0
    assert ch.switching_penalty == cfg.switching_penalty
    assert inc.score - ch.score == pytest.approx(cfg.switching_penalty)


# --- 9. hysteresis ------------------------------------------------------------
def test_hysteresis_prevents_ping_pong(cfg):
    """Alternating risk every step must not produce a switch every step."""
    obs = three()
    eng = DecisionEngine(cfg, Mode.PREDICTIVE)
    eng.reset(obs)
    switches = 0
    for t in range(40):
        hot = "LEO-1" if t % 2 == 0 else "MEO-1"
        risks = {p: (0.99 if p == hot else 0.0) for p in obs}
        if eng.step(t, float(t), obs, risks).decision is DecisionType.SWITCH:
            switches += 1
    assert switches <= 5, f"ping-pong not contained: {switches} switches in 40 steps"


def test_greedy_ablation_switches_far_more_than_guarded(cfg):
    """If disabling the guards does not raise handovers, they were never binding
    and the hysteresis claim would be unsupported (a Phase 1 lesson)."""
    obs = three()
    ablated = EngineConfig.from_json(
        os.path.join(_ARTIFACTS, "decision_engine_config.json"),
        min_dwell_steps=0, cooldown_steps=0, improvement_margin=0.0,
        switching_penalty=0.0, greedy_reevaluate=True,
    )

    def count(c):
        eng = DecisionEngine(c, Mode.PREDICTIVE)
        eng.reset(obs)
        n = 0
        for t in range(40):
            hot = "LEO-1" if t % 2 == 0 else "MEO-1"
            risks = {p: (0.99 if p == hot else 0.0) for p in obs}
            if eng.step(t, float(t), obs, risks).decision is DecisionType.SWITCH:
                n += 1
        return n

    guarded, greedy = count(cfg), count(ablated)
    assert greedy > guarded * 2, f"guards not binding: guarded={guarded}, greedy={greedy}"


# --- 13. recovery -------------------------------------------------------------
def test_engine_can_return_to_a_recovered_path(cfg):
    eng = DecisionEngine(cfg, Mode.REACTIVE)
    eng.reset(three())
    for t in range(cfg.sustained_poor_steps + 1):          # LEO fails -> leave it
        eng.step(t, float(t), three(leo=dict(**BAD)))
    assert eng.current_path != "LEO-1"
    last = None
    for t in range(10, 60):                                 # LEO healthy again
        last = eng.step(t, float(t), three(meo=dict(**BAD)))
    assert eng.current_path == "LEO-1"
    assert last is not None


# --- 14. every decision is explainable ---------------------------------------
def test_decision_reason_always_populated(cfg):
    eng = DecisionEngine(cfg, Mode.PREDICTIVE)
    obs = three()
    eng.reset(obs)
    for t in range(30):
        rec = eng.step(t, float(t), obs, risks={p: 0.5 for p in obs})
        assert isinstance(rec.reason, DecisionReason)
        assert rec.selected_path in obs
        assert len(rec.scores) == 3
        d = rec.to_dict()
        assert d["decision_reason"] and d["candidate_paths"]
