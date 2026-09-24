"""M5 ingestion layer for REAL Android cellular telemetry.

WHAT IS REAL AND WHAT IS NOT
----------------------------
An Android handset reports a handful of *cellular* radio measurements:
timestamp, RSRP, RSRQ, RSSI, sometimes SINR, and the network type. That is REAL
cellular telemetry from a terrestrial network. It is NOT satellite telemetry, and
the phone cannot command a carrier handover -- this project never claims otherwise.

The model, however, was trained on a 15-field contract that also includes network
performance (latency, jitter, loss, throughput, load) and orbital geometry
(elevation, availability, orbit). Those cannot come from a handset.

So this module does the only honest thing available: it keeps the two sources
separate and labels every field with its provenance.

    REAL (from the handset)    rsrp_dbm, rsrq_db, rssi_dbm, sinr_db*, timestamp
    SIMULATED (from M3)        elevation, availability, orbit, latency, jitter,
                               packet loss, throughput, load, traffic demand, env

    * SINR is frequently unavailable on real devices -- see below.

Nothing here duplicates ML or decision logic. Inference stays in inference.py and
policy stays in decision/; this module only validates, normalises and buffers.
"""
from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.decision.models import PathObservation

# Plausibility bounds for terrestrial cellular measurements. Values outside these
# are rejected rather than clipped: a handset reporting -400 dBm is a broken client,
# not a weak signal, and silently clamping would hide the bug.
CELLULAR_BOUNDS = {
    "rsrp_dbm": (-140.0, -40.0),
    "rsrq_db": (-25.0, -3.0),
    "rssi_dbm": (-120.0, -30.0),
    "sinr_db": (-20.0, 40.0),
}

NETWORK_TYPES = ("NR", "LTE", "UMTS", "GSM", "UNKNOWN")

SINR_MEASURED = "measured"
SINR_ESTIMATED = "estimated_from_rsrq"
SINR_UNAVAILABLE = "unavailable"

# RSSI, like SINR, is not universally reported. 5G NR has no RSSI concept at all
# (CellSignalStrengthNr exposes ssRsrp/ssRsrq/ssSinr only), and several LTE modems
# return CellInfo.UNAVAILABLE for it. Requiring it rejected otherwise-valid real
# measurements -- see the M6 Vivo I2302 finding.
RSSI_MEASURED = "measured"
RSSI_UNAVAILABLE = "unavailable"


class TelemetryError(ValueError):
    """Client-side problem with an ingested sample -> HTTP 4xx, never 500."""


def estimate_sinr_from_rsrq(rsrq_db: float) -> float:
    """CRUDE SINR estimate used ONLY when the caller explicitly opts in.

    RSRQ and SINR are related but not interchangeable; this linear approximation is
    a rough engineering heuristic, not a validated conversion. Any sample using it
    is tagged `sinr_source="estimated_from_rsrq"` so an estimate is never mistaken
    for a measurement downstream or in the UI.
    """
    return max(-10.0, min(30.0, 2.0 * (rsrq_db + 10.0)))


@dataclass(frozen=True)
class NormalizedSample:
    """Backend-internal representation of one REAL cellular measurement.

    Android naming/typing quirks are converted here and nowhere else, so handset
    details do not leak through the rest of the backend.
    """
    timestamp: float
    rsrp_dbm: float
    rsrq_db: float
    rssi_dbm: Optional[float]
    rssi_source: str
    sinr_db: Optional[float]
    sinr_source: str
    network_type: str
    device_id: Optional[str] = None
    cell_id: Optional[str] = None

    @property
    def has_sinr(self) -> bool:
        return self.sinr_db is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_android_sample(raw: Dict[str, Any], *,
                             allow_sinr_estimate: bool = False
                             ) -> Tuple[NormalizedSample, List[str]]:
    """Validate and normalise one Android-style payload.

    Returns (sample, warnings). Raises TelemetryError on anything a client must fix.

    SINR policy (M5 requirement): a missing SINR is NEVER silently replaced with a
    default. It is either reported as unavailable, or -- only when the caller sets
    allow_sinr_estimate -- estimated and explicitly labelled as an estimate.
    """
    warnings: List[str] = []

    for key in ("timestamp", "rsrp_dbm", "rsrq_db"):
        if raw.get(key) is None:
            raise TelemetryError(f"missing required cellular field: {key}")

    ts = float(raw["timestamp"])
    if not math.isfinite(ts) or ts <= 0:
        raise TelemetryError(f"timestamp must be a positive finite value, got {ts!r}")
    if ts > time.time() + 86400.0:
        raise TelemetryError("timestamp is more than a day in the future")

    values: Dict[str, Optional[float]] = {}
    for key in ("rsrp_dbm", "rsrq_db"):
        v = float(raw[key])
        lo, hi = CELLULAR_BOUNDS[key]
        if not math.isfinite(v):
            raise TelemetryError(f"{key} must be finite, got {v!r}")
        if not (lo <= v <= hi):
            raise TelemetryError(
                f"{key}={v} outside plausible cellular range [{lo}, {hi}]"
            )
        values[key] = v

    # --- RSSI: optional, because NR does not define it and many modems omit it --
    raw_rssi = raw.get("rssi_dbm")
    if raw_rssi is not None and (
        not math.isfinite(float(raw_rssi)) or abs(float(raw_rssi)) >= 2147483647
    ):
        raw_rssi = None
        warnings.append("device reported an RSSI sentinel value; treated as unavailable")

    if raw_rssi is None:
        rssi = None
        rssi_source = RSSI_UNAVAILABLE
        warnings.append(
            "RSSI not reported by device; recorded as unavailable. The simulated "
            "candidate path supplies that field instead, labelled as simulated."
        )
    else:
        rssi = float(raw_rssi)
        lo, hi = CELLULAR_BOUNDS["rssi_dbm"]
        if not (lo <= rssi <= hi):
            raise TelemetryError(
                f"rssi_dbm={rssi} outside plausible cellular range [{lo}, {hi}]"
            )
        rssi_source = RSSI_MEASURED

    # --- SINR: the field real handsets most often withhold -------------------
    raw_sinr = raw.get("sinr_db")
    # Android sentinels for "not available".
    if raw_sinr is not None and (
        not math.isfinite(float(raw_sinr)) or abs(float(raw_sinr)) >= 2147483647
    ):
        raw_sinr = None
        warnings.append("device reported a SINR sentinel value; treated as unavailable")

    if raw_sinr is None:
        if allow_sinr_estimate:
            sinr = estimate_sinr_from_rsrq(values["rsrq_db"])
            source = SINR_ESTIMATED
            warnings.append(
                "SINR not reported by device; ESTIMATED from RSRQ. This is a crude "
                "approximation, not a measurement -- predictions using it are indicative only."
            )
        else:
            sinr = None
            source = SINR_UNAVAILABLE
            warnings.append(
                "SINR not reported by device; sample is not prediction-ready. Set "
                "allow_sinr_estimate=true to derive a labelled estimate from RSRQ."
            )
    else:
        sinr = float(raw_sinr)
        lo, hi = CELLULAR_BOUNDS["sinr_db"]
        if not (lo <= sinr <= hi):
            raise TelemetryError(
                f"sinr_db={sinr} outside plausible cellular range [{lo}, {hi}]"
            )
        source = SINR_MEASURED

    net = str(raw.get("network_type") or "UNKNOWN").upper()
    if net not in NETWORK_TYPES:
        warnings.append(f"unrecognised network_type {net!r}; recorded as UNKNOWN")
        net = "UNKNOWN"

    sample = NormalizedSample(
        timestamp=ts,
        rsrp_dbm=values["rsrp_dbm"],
        rsrq_db=values["rsrq_db"],
        rssi_dbm=rssi,
        rssi_source=rssi_source,
        sinr_db=sinr,
        sinr_source=source,
        network_type=net,
        device_id=raw.get("device_id"),
        cell_id=raw.get("cell_id"),
    )
    return sample, warnings


# ---------------------------------------------------------------------------
# Per-device buffering
# ---------------------------------------------------------------------------
@dataclass
class DeviceSession:
    """Buffered REAL telemetry for one device session, plus its SIMULATED context."""
    session_id: str
    window_size: int
    samples: Deque[NormalizedSample] = field(default_factory=deque)
    received: int = 0
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.samples = deque(maxlen=self.window_size)

    @property
    def buffer_size(self) -> int:
        return len(self.samples)

    @property
    def window_full(self) -> bool:
        return len(self.samples) >= self.window_size

    @property
    def latest(self) -> Optional[NormalizedSample]:
        return self.samples[-1] if self.samples else None

    def prediction_ready(self) -> bool:
        """Ready when the window is full and every sample carries a SINR.

        RSSI is deliberately NOT required: when a device does not report it, the
        simulated candidate path supplies that one field and provenance marks it
        simulated. SINR is different -- it is the model's second-strongest feature,
        so an absent SINR still blocks prediction rather than being papered over.
        """
        return self.window_full and all(s.has_sinr for s in self.samples)


class TelemetryStore:
    """In-memory device-session registry with TTL eviction.

    Mirrors M4's SessionStore deliberately: same lifecycle, same constraints, no
    database (M5 forbids one). NOTE: do not write `store or TelemetryStore()`
    anywhere -- __len__ makes an empty store falsy, which silently discards an
    injected instance. (That bug was found and fixed in M4.)
    """

    def __init__(self, window_size: int, ttl_s: float = 900.0,
                 max_sessions: int = 512) -> None:
        self.window_size = window_size
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: Dict[str, DeviceSession] = {}

    def _evict(self) -> None:
        now = time.time()
        for k in [k for k, s in self._sessions.items() if now - s.last_seen > self.ttl_s]:
            del self._sessions[k]
        if len(self._sessions) > self.max_sessions:
            for k, _ in sorted(self._sessions.items(),
                               key=lambda kv: kv[1].last_seen)[
                    : len(self._sessions) - self.max_sessions]:
                del self._sessions[k]

    def get_or_create(self, session_id: str) -> DeviceSession:
        self._evict()
        s = self._sessions.get(session_id)
        if s is None:
            s = DeviceSession(session_id=session_id, window_size=self.window_size)
            self._sessions[session_id] = s
        s.last_seen = time.time()
        return s

    def get(self, session_id: str) -> Optional[DeviceSession]:
        return self._sessions.get(session_id)

    def ingest(self, session_id: str, sample: NormalizedSample) -> DeviceSession:
        session = self.get_or_create(session_id)
        prev = session.latest
        if prev is not None and sample.timestamp < prev.timestamp:
            raise TelemetryError(
                f"out-of-order sample: timestamp {sample.timestamp} precedes "
                f"the previous sample at {prev.timestamp}"
            )
        session.samples.append(sample)
        session.received += 1
        return session

    def drop(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def __len__(self) -> int:
        return len(self._sessions)


# ---------------------------------------------------------------------------
# Bridging REAL cellular radio into the SIMULATED candidate-path contract
# ---------------------------------------------------------------------------
REAL_RADIO_FIELDS = ("rsrp_dbm", "rsrq_db", "sinr_db", "rssi_dbm")

SIMULATED_FIELDS = (
    "orbit", "elevation_deg", "latency_ms", "jitter_ms", "packet_loss_pct",
    "throughput_mbps", "load", "traffic_demand", "env_index", "available",
)


def merge_real_radio(simulated: PathObservation,
                     sample: NormalizedSample) -> PathObservation:
    """Overlay the handset's REAL radio measurements onto a SIMULATED path record.

    Only the four radio fields come from the device; geometry and network
    performance remain simulated, because a handset cannot measure them for a
    satellite link. Provenance is reported alongside (see field_provenance).

    LIMITATION worth stating plainly: real terrestrial RSRP/RSRQ distributions do
    not match the simulated training distribution, so a prediction built on merged
    data is indicative of the pipeline working end to end -- not a validated
    satellite-link forecast.
    """
    if sample.sinr_db is None:
        raise TelemetryError(
            "cannot merge a sample without SINR; the model contract requires it"
        )
    return PathObservation(
        path_id=simulated.path_id,
        timestamp=sample.timestamp,
        orbit=simulated.orbit,
        rsrp_dbm=sample.rsrp_dbm,
        rsrq_db=sample.rsrq_db,
        sinr_db=sample.sinr_db,
        # RSSI falls back to the SIMULATED value when the handset did not measure
        # it. Not fabrication: field_provenance() reports it as simulated.
        rssi_dbm=sample.rssi_dbm if sample.rssi_dbm is not None else simulated.rssi_dbm,
        elevation_deg=simulated.elevation_deg,
        latency_ms=simulated.latency_ms,
        jitter_ms=simulated.jitter_ms,
        packet_loss_pct=simulated.packet_loss_pct,
        throughput_mbps=simulated.throughput_mbps,
        load=simulated.load,
        traffic_demand=simulated.traffic_demand,
        env_index=simulated.env_index,
        available=simulated.available,
    )


def field_provenance(merged_path_id: Optional[str],
                     sample: Optional["NormalizedSample"]) -> Dict[str, str]:
    """Per-field origin, so a UI can never present simulated data as measured.

    `sample` is REQUIRED (pass None only for a fully-simulated run). It used to
    default to None, which meant a caller that forgot it silently labelled every
    radio field "real_cellular" -- including an RSSI the handset never measured.
    Making the argument explicit turns that mistake into a TypeError at the call
    site instead of a false claim in the response body.

    A field is "real_cellular" only when the handset actually measured it. RSSI and
    SINR are both optional on real devices (5G NR defines no RSSI at all), and when
    absent the simulated candidate path supplies the value -- so it is reported as
    "simulated", never as a measurement.
    """
    prov = {f: "simulated" for f in SIMULATED_FIELDS}

    # No merge -> nothing on this path came from a handset.
    if not merged_path_id:
        prov.update({f: "simulated" for f in REAL_RADIO_FIELDS})
        prov["timestamp"] = "simulated"
        return prov

    prov.update({f: "real_cellular" for f in REAL_RADIO_FIELDS})
    prov["timestamp"] = "real_cellular"

    if sample is None:
        # A merge was requested but we have no sample to attest to. Claim nothing.
        prov.update({f: "unavailable" for f in REAL_RADIO_FIELDS})
        return prov

    if sample.rssi_dbm is None:
        prov["rssi_dbm"] = "simulated"
    if sample.sinr_db is None:
        prov["sinr_db"] = "simulated"
    return prov


def session_seed(session_id: str) -> int:
    """Stable per-session seed so a device's simulated context is reproducible."""
    return int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16)
