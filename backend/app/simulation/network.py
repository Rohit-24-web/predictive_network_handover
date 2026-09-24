"""SIMULATED multi-orbit telemetry generator.

Ports the Phase 1 causal chain using the parameters already published in
artifacts/feature_config.json -> orbit_params. Reproducing that generator matters:
the model was trained on this distribution, and plausible-looking numbers from a
different distribution put it out of domain, where it still returns confident
probabilities that mean nothing.

    orbit geometry ---+
    weather / rain    +--> RSRP --> SINR --> packet loss --> latency
    multipath fading -+                 |                      |
    network load ---------------------- +--> throughput      jitter

ASSUMPTION: path conditions are INDEPENDENT of which path the engine selects.
There is no feedback from selection to link state. That is what lets the same
scenario be replayed for both modes (see runner.run_experiment).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.decision.models import PathObservation

NOISE_FLOOR_DBM = -110.0
INTERF_RISE_DB = 9.0
ORBITS = ("LEO", "MEO", "GEO")


def _ar1(n: int, phi: float, sigma: float, rng: np.random.Generator) -> np.ndarray:
    eps = rng.normal(0.0, sigma, size=n)
    out = np.empty(n)
    out[0] = eps[0]
    for t in range(1, n):
        out[t] = phi * out[t - 1] + eps[t]
    return out


def _ou_bounded(n: int, rng: np.random.Generator,
                theta: float = 0.02, sigma: float = 0.06) -> np.ndarray:
    """Mean-reverting process squashed into [0, 1]."""
    return 1.0 / (1.0 + np.exp(-_ar1(n, 1.0 - theta, sigma, rng)))


def _elevation(orbit: str, n: int, params: Dict[str, float],
               rng: np.random.Generator) -> np.ndarray:
    """Rise-set pass profile for LEO/MEO; fixed geometry for GEO."""
    if orbit == "GEO":
        return np.full(n, rng.uniform(25.0, 62.0))
    t = np.arange(n, dtype=float)
    phase = rng.uniform(0.0, 1.0)
    frac = np.mod(t / params["pass_period"] + phase, 1.0)
    return params["elev_max"] * np.sin(np.pi * frac)


@dataclass(frozen=True)
class ScenarioRecord:
    """An immutable, fully pre-generated scenario.

    Pre-generation is what guarantees fairness: both modes replay the SAME record,
    so RNG consumption cannot cause the two runs to diverge.
    """
    name: str
    seed: int
    n_steps: int
    warmup_steps: int
    path_ids: List[str]
    telemetry: Dict[str, List[PathObservation]]

    def observations_at(self, t: int) -> Dict[str, PathObservation]:
        return {pid: self.telemetry[pid][t] for pid in self.path_ids}

    def window(self, path_id: str, t: int, size: int) -> List[Dict[str, Any]]:
        """Backward-looking telemetry window ending at t (inclusive).

        Short at the start of a run; features.features_from_window edge-pads it,
        exactly as Phase 1 training handled a cold buffer.
        """
        lo = max(0, t - size + 1)
        return [o.to_telemetry_dict() for o in self.telemetry[path_id][lo:t + 1]]

    def digest(self) -> str:
        """Stable hash of all telemetry — used to assert both modes saw identical data."""
        h = hashlib.sha256()
        for pid in self.path_ids:
            for o in self.telemetry[pid]:
                h.update(json.dumps(o.to_telemetry_dict(), sort_keys=True,
                                    default=float).encode())
        return h.hexdigest()


def generate_scenario(name: str, seed: int, n_steps: int = 300,
                      warmup_steps: int = 26,
                      orbit_params: Optional[Dict[str, Dict[str, float]]] = None,
                      scenario_fn=None) -> ScenarioRecord:
    """Generate one SIMULATED scenario. Deterministic for a given (name, seed, n_steps)."""
    if orbit_params is None:
        from app.config import ARTIFACT_DIR
        import os
        with open(os.path.join(ARTIFACT_DIR, "feature_config.json")) as f:
            orbit_params = json.load(f)["orbit_params"]

    rng = np.random.default_rng(seed)
    total = n_steps

    # --- exogenous drivers shared across this user's paths ---
    env_index = _ou_bounded(total, rng, theta=0.012, sigma=0.055)
    traffic_demand = _ou_bounded(total, rng, theta=0.02, sigma=0.08)

    # Per-orbit latent state, before scenario perturbation.
    latent: Dict[str, Dict[str, np.ndarray]] = {}
    for orbit in ORBITS:
        p = orbit_params[orbit]
        elev = _elevation(orbit, total, p, rng)
        load = np.clip(0.30 + 0.55 * _ou_bounded(total, rng, theta=0.02, sigma=0.07)
                       + 0.12 * np.sin(2 * np.pi * np.arange(total) / 420.0
                                       + rng.uniform(0, 2 * np.pi)), 0.02, 0.99)
        fading = _ar1(total, p["fade_phi"], p["fade_sigma"], rng)
        latent[orbit] = {
            "elev": elev, "load": load, "fading": fading,
            "extra_att": np.zeros(total),     # scenario-injected attenuation, dB
            "load_boost": np.zeros(total),    # scenario-injected congestion
        }

    # --- scenario hook mutates extra_att / load_boost / env_index only ---
    if scenario_fn is not None:
        scenario_fn(rng, total, latent, env_index)

    telemetry: Dict[str, List[PathObservation]] = {}
    path_ids: List[str] = []
    for orbit in ORBITS:
        p = orbit_params[orbit]
        st = latent[orbit]
        path_id = f"{orbit}-1"
        path_ids.append(path_id)

        elev = st["elev"]
        sin_elev = np.clip(np.sin(np.radians(np.clip(elev, 0.5, 90.0))), 0.02, 1.0)
        load = np.clip(st["load"] + st["load_boost"], 0.02, 0.995)

        rain_att = np.clip(p["rain_coeff"] * (env_index ** 1.2)
                           / np.clip(sin_elev, 0.25, 1.0), 0.0, 18.0)
        elev_gain = np.clip(15.0 * np.log10(sin_elev), -14.0, 0.0)
        rsrp = np.clip(p["base_rsrp"] + elev_gain - rain_att + st["fading"]
                       - st["extra_att"], -130.0, -60.0)

        sinr = rsrp - (NOISE_FLOOR_DBM + INTERF_RISE_DB * load) + rng.normal(0, 0.5, total)
        available = (elev >= p["elev_mask"]).astype(float)

        loss = 100.0 / (1.0 + np.exp((sinr - 6.0) / 1.4))
        loss = loss * (0.35 + 0.65 * load) + rng.gamma(1.2, 0.12, total)
        loss = np.where(available >= 0.5, np.clip(loss, 0.0, 100.0), 100.0)

        latency = (p["base_latency"] + 45.0 * (load ** 2.2) + 2.2 * loss
                   + rng.gamma(2.0, 1.6, total))
        latency = np.where(available >= 0.5, latency, p["base_latency"] * 3.0 + 400.0)

        jitter = 1.5 + 14.0 * load + 0.35 * loss + rng.gamma(1.5, 0.7, total)

        se = np.log2(1.0 + np.power(10.0, np.clip(sinr, -5, 30) / 10.0))
        se_norm = np.clip(se / np.log2(1.0 + 10 ** 2.0), 0.0, 1.0)
        thr = np.clip(p["capacity"] * se_norm * (1.0 - 0.55 * load)
                      * (1.0 - loss / 100.0), 0.0, None) * available

        rsrq = np.clip(-3.0 - 0.55 * np.clip(20.0 - sinr, 0, None) * 0.5, -20.0, -3.0) \
            + rng.normal(0, 0.4, total)
        rssi = rsrp + 15.0 + 6.0 * load + rng.normal(0, 0.6, total)

        telemetry[path_id] = [
            PathObservation(
                path_id=path_id, timestamp=float(t), orbit=orbit,
                rsrp_dbm=float(rsrp[t]), rsrq_db=float(rsrq[t]),
                sinr_db=float(sinr[t]), rssi_dbm=float(rssi[t]),
                elevation_deg=float(max(elev[t], 0.0)),
                latency_ms=float(latency[t]), jitter_ms=float(jitter[t]),
                packet_loss_pct=float(loss[t]), throughput_mbps=float(thr[t]),
                load=float(load[t]), traffic_demand=float(traffic_demand[t]),
                env_index=float(env_index[t]), available=float(available[t]),
            )
            for t in range(total)
        ]

    return ScenarioRecord(name=name, seed=seed, n_steps=total,
                          warmup_steps=warmup_steps, path_ids=path_ids,
                          telemetry=telemetry)
