import { describe, expect, it } from "vitest";
import { emptyState, reduce } from "../services/protocol";
import { httpBase, isDeviceSession } from "../services/sessions";
import type { NormalizedTelemetry, SessionSummary, TelemetryAckMsg } from "../types/protocol";

/** Exact values reported by the physical Vivo I2302. */
const VIVO: NormalizedTelemetry = {
  timestamp: 1789110484.0,
  rsrp_dbm: -75.0,
  rsrq_db: -11.0,
  rssi_dbm: null,
  rssi_source: "unavailable",
  sinr_db: 25.0,
  sinr_source: "measured",
  network_type: "UNKNOWN",
};

const observerAck = (over: Partial<TelemetryAckMsg> = {}): TelemetryAckMsg => ({
  type: "telemetry_ack", session_id: "android-6567b8f8-b1a",
  normalized: VIVO, samples_received: 1, buffer_size: 1,
  required_window_steps: 26, window_full: false, prediction_ready: false,
  sinr_source: "measured", warnings: [], ...over,
});

describe("observer frames carry real values", () => {
  it("uses the frame's normalized sample when nothing was sent locally", () => {
    const s = reduce(emptyState("dashboard-x"), observerAck());
    expect(s.latestSample).not.toBeNull();
    expect(s.latestSample!.rsrp_dbm).toBe(-75.0);
    expect(s.latestSample!.rsrq_db).toBe(-11.0);
    expect(s.latestSample!.sinr_db).toBe(25.0);
  });

  it("charts the real values rather than leaving the history empty", () => {
    const s = reduce(emptyState("dashboard-x"), observerAck());
    expect(s.history).toHaveLength(1);
    expect(s.history[0].rsrp).toBe(-75.0);
    expect(s.history[0].sinr).toBe(25.0);
  });

  it("keeps RSSI null and never substitutes a number", () => {
    const s = reduce(emptyState("dashboard-x"), observerAck());
    expect(s.latestSample!.rssi_dbm).toBeNull();
    expect((s.latestSample as NormalizedTelemetry).rssi_source).toBe("unavailable");
  });

  it("preserves the device's network type rather than guessing", () => {
    const s = reduce(emptyState("dashboard-x"), observerAck());
    expect((s.latestSample as NormalizedTelemetry).network_type).toBe("UNKNOWN");
  });

  it("still works for the producer path, where the sample is stashed locally", () => {
    const local = { ...emptyState("d"), pendingSample: { ...VIVO, rsrp_dbm: -92.0 } };
    const ackWithoutNormalized = { ...observerAck(), normalized: undefined };
    const s = reduce(local, ackWithoutNormalized);
    expect(s.latestSample!.rsrp_dbm).toBe(-92.0);
  });
});

describe("session association", () => {
  const own = "dashboard-028k50j2";
  const summary = (id: string): SessionSummary => ({
    session_id: id, samples_received: 26, buffer_size: 26, required_window_steps: 26,
    window_full: true, prediction_ready: true, observers: 0,
    last_sample_timestamp: 1789110484, network_type: "UNKNOWN", sinr_source: "measured",
  });

  it("treats the dashboard's own session as not a device", () => {
    expect(isDeviceSession(summary(own), own)).toBe(false);
  });

  it("treats the handset's session as a device", () => {
    expect(isDeviceSession(summary("android-6567b8f8-b1a"), own)).toBe(true);
  });

  it("derives the HTTP origin from the websocket URL", () => {
    expect(httpBase("ws://localhost:8000")).toBe("http://localhost:8000");
    expect(httpBase("ws://10.135.47.73:8000/")).toBe("http://10.135.47.73:8000");
    expect(httpBase("wss://host:8000")).toBe("https://host:8000");
  });
});

describe("provenance is surfaced, not invented", () => {
  it("records backend provenance verbatim, including simulated RSSI", () => {
    const s = reduce(emptyState("d"), {
      type: "decision", session_id: "android-6567b8f8-b1a", simulation_step: 40,
      merged_path: "LEO-1",
      field_provenance: {
        rsrp_dbm: "real_cellular", rsrq_db: "real_cellular", sinr_db: "real_cellular",
        rssi_dbm: "simulated", latency_ms: "simulated", orbit: "simulated",
      },
      notes: ["This device does not report RSSI"],
      decision: {
        session_id: "telemetry::android-6567b8f8-b1a", mode: "predictive",
        current_path: "GEO-1", selected_path: "GEO-1", decision: "STAY",
        decision_reason: "STAY_CURRENT_PATH", handover: false,
        degradation_probability: 0.1708,
        risk_probabilities: { "LEO-1": 0.9979, "MEO-1": 0.9989, "GEO-1": 0.1708 },
        prediction_horizon_seconds: 5, model_version: "phase1-v1.0.0", candidates: [],
      },
    });
    expect(s.provenance!.rsrp_dbm).toBe("real_cellular");
    expect(s.provenance!.rssi_dbm).toBe("simulated");
    expect(s.provenance!.rssi_dbm).not.toBe("real_cellular");
    expect(s.decision!.current_path).toBe("GEO-1");
  });
});
