"""
features.py — Phase 2 feature engineering.

PORTED VERBATIM FROM PHASE 1. Do not "improve" this file.

The scaler and model were fitted on features produced by exactly this logic. Any change to
ordering, window handling, or arithmetic here silently corrupts every prediction — the model
will still return a confident-looking number, it will just be wrong.

If you think something here is inefficient: benchmark first, and if you change it, the golden
fixture test (tests/test_golden.py) must still pass to the stated tolerance.

Dependencies: numpy only.
"""
from typing import Any, Dict, List

import numpy as np

# ---------------------------------------------------------------------------
# Orbit simulation parameters. These are SIMULATION ASSUMPTIONS from Phase 1,
# not measurements. They are needed here because the degradation score is
# orbit-relative (620 ms is nominal for GEO, catastrophic for LEO).
# ---------------------------------------------------------------------------
ORBIT_PARAMS: Dict[str, Dict[str, float]] = {
    "LEO": {"base_latency": 45.0, "capacity": 180.0},
    "MEO": {"base_latency": 130.0, "capacity": 140.0},
    "GEO": {"base_latency": 600.0, "capacity": 120.0},
}

REQUIRED_RAW_FIELDS = [
    "timestamp", "orbit", "rsrp_dbm", "rsrq_db", "sinr_db", "rssi_dbm",
    "elevation_deg", "latency_ms", "jitter_ms", "packet_loss_pct",
    "throughput_mbps", "load", "traffic_demand", "env_index", "available",
]


def degradation_score_from_metrics(sinr: float, loss: float, lat: float,
                                   thr: float, orbit: str) -> float:
    """Composite degradation score in [0, 1]. One definition, shared by the label,
    the decision engine and the backend."""
    p = ORBIT_PARAMS[orbit]
    sinr_bad = min(max((14.0 - sinr) / 12.0, 0.0), 1.0)
    loss_bad = min(max(loss / 5.0, 0.0), 1.0)
    lat_bad = min(max((lat - (p["base_latency"] + 25.0)) / 125.0, 0.0), 1.0)
    thr_ref = p["capacity"] * 0.45
    thr_bad = min(max((thr_ref - thr) / thr_ref, 0.0), 1.0)
    return 0.30 * sinr_bad + 0.30 * loss_bad + 0.20 * lat_bad + 0.20 * thr_bad


def quality_from_metrics(sinr: float, loss: float, lat: float,
                         thr: float, orbit: str) -> float:
    """Observed QoS in [0, 1]; 1.0 is perfect. Used by the decision engine."""
    return 1.0 - degradation_score_from_metrics(sinr, loss, lat, thr, orbit)


def window_to_array(telemetry_window: List[Dict[str, Any]], cfg: dict) -> np.ndarray:
    """Convert a list of raw telemetry dicts into the (W, n_base+1) array the
    feature builder expects. Raises ValueError on malformed input."""
    if not isinstance(telemetry_window, (list, tuple)) or len(telemetry_window) == 0:
        raise ValueError("telemetry_window must be a non-empty list of observations")
    for i, row in enumerate(telemetry_window):
        missing = [f for f in REQUIRED_RAW_FIELDS if f not in row]
        if missing:
            raise ValueError(f"observation {i} missing fields: {missing}")

    arr = np.array(
        [[row[name] for name in cfg["base_signals"]] + [row["available"]]
         for row in telemetry_window],
        dtype=np.float64,
    )
    if not np.isfinite(arr).all():
        raise ValueError("telemetry window contains NaN or infinite values")
    return arr


def _feature_row(window: np.ndarray, orbit: str, cfg: dict, end: int) -> Dict[str, float]:
    """Feature dict for the observation at index `end`. Uses rows [0..end] ONLY."""
    base = cfg["base_signals"]
    roll_w, slope_w = cfg["rolling_window"], cfg["slope_window"]

    cols = {name: window[: end + 1, i] for i, name in enumerate(base)}
    cols["available"] = window[: end + 1, len(base)]

    feats: Dict[str, float] = {name: float(cols[name][-1]) for name in base}
    feats["available"] = float(cols["available"][-1])

    for name in cfg["dynamic_signals"]:
        s = cols[name]
        feats[f"{name}__d1"] = float(s[-1] - s[-2]) if len(s) >= 2 else 0.0
        feats[f"{name}__d3"] = float(s[-1] - s[-4]) if len(s) >= 4 else 0.0
        w = s[-slope_w:]
        x = np.arange(len(w), dtype=float)
        xc = x - x.mean()
        feats[f"{name}__slope{slope_w}"] = (
            float((w - w.mean()) @ xc / (xc ** 2).sum()) if len(w) > 1 else 0.0
        )
        r = s[-roll_w:]
        feats[f"{name}__rmean{roll_w}"] = float(r.mean())
        feats[f"{name}__rstd{roll_w}"] = float(r.std())
        feats[f"{name}__rmin{roll_w}"] = float(r.min())
        feats[f"{name}__rmax{roll_w}"] = float(r.max())

    feats[f"available__rmean{roll_w}"] = float(cols["available"][-roll_w:].mean())

    flags = [
        int(
            degradation_score_from_metrics(
                cols["sinr_db"][i], cols["packet_loss_pct"][i],
                cols["latency_ms"][i], cols["throughput_mbps"][i], orbit,
            ) > cfg["degradation_tau"]
            or cols["available"][i] < 0.5
        )
        for i in range(end + 1)
    ]
    lagged = [0] + flags[:-1]
    feats[f"recent_degradation_count{roll_w}"] = float(sum(lagged[-roll_w:]))

    for o in cfg["orbits"]:
        feats[f"orbit_{o}"] = 1.0 if o == orbit else 0.0
    return feats


def features_from_window(window: np.ndarray, orbit: str, cfg: dict,
                         n_steps: int = 1) -> np.ndarray:
    """Feature matrix (n_steps, n_features) for the LAST n_steps observations.

    n_steps=1                     -> tabular model input
    n_steps=cfg['sequence_length'] -> recurrent model input

    Short windows are edge-padded at the FRONT, matching how training handled a cold buffer.
    """
    need = cfg["required_window_steps"]
    if window.shape[0] < need:
        pad = np.repeat(window[:1], need - window.shape[0], axis=0)
        window = np.vstack([pad, window])

    last = window.shape[0] - 1
    rows = [
        [_feature_row(window, orbit, cfg, end=last - k)[name] for name in cfg["feature_names"]]
        for k in range(n_steps - 1, -1, -1)
    ]
    return np.asarray(rows, dtype=np.float32)
