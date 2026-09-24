/**
 * DEMO INPUT — deterministic synthetic telemetry for developing the dashboard
 * without an Android handset.
 *
 * This is NOT Android data and must never be presented as a measurement. The UI
 * labels it "DEMO INPUT" whenever this generator is the source, and the backend
 * treats it identically to any other client payload — no backend change was needed.
 *
 * Values sit inside the backend's plausible cellular ranges so they are accepted;
 * they drift slowly and dip periodically so the charts and the risk gauge visibly
 * move during a demo.
 */
import type { CellularSample } from "../types/protocol";

export interface DemoOptions {
  /** Omit SINR to exercise the "unavailable" path end to end. */
  withSinr?: boolean;
}

export function demoSample(index: number, opts: DemoOptions = {}): CellularSample {
  const withSinr = opts.withSinr ?? true;
  // A slow sinusoid plus a periodic dip: deterministic, no randomness.
  const phase = index / 9;
  const dip = index % 40 >= 28 ? 9 : 0;
  const rsrp = -92 + Math.sin(phase) * 5 - dip;
  const sinr = 15 + Math.cos(phase) * 4 - dip;

  return {
    timestamp: Math.floor(Date.now() / 1000) + index / 1000,
    rsrp_dbm: round(clamp(rsrp, -139, -41)),
    rsrq_db: round(clamp(-10 + Math.sin(phase / 2) * 2, -24, -4)),
    rssi_dbm: round(clamp(-65 + Math.sin(phase) * 4, -119, -31)),
    sinr_db: withSinr ? round(clamp(sinr, -19, 39)) : null,
    network_type: "LTE",
    device_id: "demo-generator",
  };
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const round = (v: number) => Math.round(v * 10) / 10;
