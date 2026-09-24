"""SIMULATED scenario definitions.

Each scenario is a hook that perturbs the generator's LATENT state (extra
attenuation in dB, congestion boost, or the shared environment index) before the
observable telemetry is derived. Perturbing latent state rather than the final
metrics keeps the causal chain intact: extra attenuation lowers RSRP, which lowers
SINR, which raises loss, which raises latency and lowers throughput -- exactly as a
real impairment would propagate.

All scenarios are SIMULATED. None represents a measured satellite link.
"""
from __future__ import annotations

from typing import Callable, Dict, List

import numpy as end_np  # noqa: F401  (kept explicit for clarity)
import numpy as np


def _ramp(n: int, lo: float = 0.0, hi: float = 1.0) -> np.ndarray:
    return np.linspace(lo, hi, max(n, 1))


def _bell(n: int) -> np.ndarray:
    return np.sin(np.linspace(0, np.pi, max(n, 1)))


def scenario_normal(rng, n, latent, env_index) -> None:
    """A: all paths broadly stable. Expect few handovers and a stable selection."""
    return None


def scenario_leo_degradation(rng, n, latent, env_index) -> None:
    """B: LEO deteriorates gradually, giving a predictive engine room to act early."""
    start = n // 3
    dur = max(n // 3, 20)
    end = min(n, start + dur)
    latent["LEO"]["extra_att"][start:end] += _ramp(end - start, 0.0, 16.0)
    latent["LEO"]["extra_att"][end:] += 16.0


def scenario_meo_degradation(rng, n, latent, env_index) -> None:
    """C: MEO deteriorates while other paths stay viable."""
    start = n // 3
    dur = max(n // 3, 20)
    end = min(n, start + dur)
    latent["MEO"]["extra_att"][start:end] += _ramp(end - start, 0.0, 15.0)
    latent["MEO"]["extra_att"][end:] += 15.0


def scenario_geo_fallback(rng, n, latent, env_index) -> None:
    """D: LEO and MEO both impaired; GEO stays AVAILABLE but is far slower.

    Demonstrates that 'available' does not mean 'optimal' -- the engine should fall
    back to GEO only when the faster paths are genuinely worse.
    """
    start = n // 4
    end = min(n, start + max(n // 2, 30))
    latent["LEO"]["extra_att"][start:end] += _bell(end - start) * 18.0
    latent["MEO"]["extra_att"][start:end] += _bell(end - start) * 14.0


def scenario_congestion(rng, n, latent, env_index) -> None:
    """E: a congestion surge on LEO -- load rises, throughput falls, latency climbs."""
    start = n // 3
    end = min(n, start + max(n // 3, 25))
    latent["LEO"]["load_boost"][start:end] += _bell(end - start) * 0.55


def scenario_recovery(rng, n, latent, env_index) -> None:
    """F: LEO degrades, then fully recovers. The engine should be able to return
    once dwell/cooldown permit."""
    start = n // 4
    mid = start + max(n // 6, 15)
    end = min(n, mid + max(n // 6, 15))
    latent["LEO"]["extra_att"][start:mid] += _ramp(mid - start, 0.0, 17.0)
    latent["LEO"]["extra_att"][mid:end] += _ramp(end - mid, 17.0, 0.0)


def scenario_multi_path(rng, n, latent, env_index) -> None:
    """G: staggered, overlapping impairments across all three paths.

    This is the real traffic-steering test: the best path changes more than once, so
    the engine must keep re-evaluating rather than surviving a single failure.
    """
    a0, a1 = n // 5, n // 5 + max(n // 5, 20)
    latent["LEO"]["extra_att"][a0:a1] += _bell(a1 - a0) * 17.0
    b0, b1 = n // 2, n // 2 + max(n // 5, 20)
    latent["MEO"]["extra_att"][b0:min(b1, n)] += _bell(min(b1, n) - b0) * 15.0
    c0, c1 = int(n * 0.7), min(n, int(n * 0.7) + max(n // 6, 15))
    latent["GEO"]["load_boost"][c0:c1] += _bell(c1 - c0) * 0.5
    latent["LEO"]["load_boost"][b0:min(b1, n)] += _bell(min(b1, n) - b0) * 0.35
    # A rain event raises the shared environment index, hitting every path at once.
    r0 = int(n * 0.55)
    r1 = min(n, r0 + max(n // 8, 12))
    env_index[r0:r1] = np.clip(env_index[r0:r1] + 0.5 * _bell(r1 - r0), 0.0, 1.0)


SCENARIOS: Dict[str, Callable] = {
    "normal": scenario_normal,
    "leo_degradation": scenario_leo_degradation,
    "meo_degradation": scenario_meo_degradation,
    "geo_fallback": scenario_geo_fallback,
    "congestion": scenario_congestion,
    "recovery": scenario_recovery,
    "multi_path": scenario_multi_path,
}

SCENARIO_LABELS = {
    "normal": "Normal (all paths stable)",
    "leo_degradation": "LEO Degradation",
    "meo_degradation": "MEO Degradation",
    "geo_fallback": "GEO Fallback",
    "congestion": "Congestion",
    "recovery": "Recovery",
    "multi_path": "Multi-path",
}


def list_scenarios() -> List[str]:
    return list(SCENARIOS.keys())
