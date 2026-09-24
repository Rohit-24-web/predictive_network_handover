import { describe, expect, it } from "vitest";
import {
  RISK_THRESHOLDS, emptyState, fmt, fmtPct, parseMessage, reduce,
  scoreBreakdown, severityOf, sinrSourceLabel, MAX_TELEMETRY_POINTS, MAX_EVENTS,
} from "../services/protocol";
import { demoSample } from "../services/demoStream";
import type { CandidateScore, DecisionMsg, TelemetryAckMsg } from "../types/protocol";

const ack = (over: Partial<TelemetryAckMsg> = {}): TelemetryAckMsg => ({
  type: "telemetry_ack", session_id: "s", samples_received: 1, buffer_size: 1,
  required_window_steps: 26, window_full: false, prediction_ready: false,
  sinr_source: "measured", warnings: [], ...over,
});

const candidate = (over: Partial<CandidateScore> = {}): CandidateScore => ({
  path_id: "LEO-1", orbit: "LEO", available: true, score: 0.62, quality: 0.97,
  latency_score: 0.79, tput_score: 0.59, load_penalty: 0.04,
  switching_penalty: 0, risk_penalty: 0.004, risk: 0.0128, warming_up: false, ...over,
});

const decisionMsg = (): DecisionMsg => ({
  type: "decision", session_id: "s", simulation_step: 52, merged_path: "LEO-1",
  field_provenance: { rsrp_dbm: "real_cellular", latency_ms: "simulated" },
  notes: ["REAL cellular radio fields overlaid onto LEO-1"],
  decision: {
    session_id: "telemetry::s", mode: "predictive", current_path: "LEO-1",
    selected_path: "LEO-1", decision: "STAY", decision_reason: "STAY_CURRENT_PATH",
    handover: false, degradation_probability: 0.1365,
    risk_probabilities: { "LEO-1": 0.1365, "MEO-1": 0.0407, "GEO-1": 0.007 },
    prediction_horizon_seconds: 5, model_version: "phase1-v1.0.0",
    candidates: [candidate()],
  },
});

describe("message parsing", () => {
  it("parses a valid frame", () => {
    expect(parseMessage(JSON.stringify(ack())).type).toBe("telemetry_ack");
  });

  it("turns malformed JSON into an error message rather than throwing", () => {
    const m = parseMessage("{not json");
    expect(m.type).toBe("error");
    expect(m).toMatchObject({ code: "client_parse_error" });
  });

  it("rejects frames with no type", () => {
    expect(parseMessage(JSON.stringify({ hello: 1 })).type).toBe("error");
  });

  it("rejects non-object payloads", () => {
    expect(parseMessage("[1,2,3]").type).toBe("error");
    expect(parseMessage("null").type).toBe("error");
  });
});

describe("risk mapping (UI-only thresholds)", () => {
  it("bands risk by the documented thresholds", () => {
    expect(severityOf(0.05)).toBe("low");
    expect(severityOf(RISK_THRESHOLDS.medium)).toBe("medium");
    expect(severityOf(RISK_THRESHOLDS.high)).toBe("high");
    expect(severityOf(0.99)).toBe("high");
  });

  it("returns null when the backend gave no risk, rather than defaulting to low", () => {
    expect(severityOf(null)).toBeNull();
    expect(severityOf(undefined)).toBeNull();
    expect(severityOf(NaN)).toBeNull();
  });
});

describe("missing value formatting", () => {
  it("never fabricates a number", () => {
    expect(fmt(null)).toBe("N/A");
    expect(fmt(undefined)).toBe("N/A");
    expect(fmt(NaN)).toBe("N/A");
    expect(fmtPct(null)).toBe("N/A");
    expect(fmt(-99.7, 1, "dBm")).toBe("-99.7 dBm");
    expect(fmtPct(0.1365)).toBe("13.7%");
  });

  it("labels SINR provenance", () => {
    expect(sinrSourceLabel("measured")).toBe("measured");
    expect(sinrSourceLabel("estimated_from_rsrq")).toBe("estimated");
    expect(sinrSourceLabel("unavailable")).toBe("unavailable");
    expect(sinrSourceLabel(null)).toBe("unknown");
  });
});

describe("telemetry state updates", () => {
  it("records buffer progress from an ack", () => {
    const s = reduce(emptyState("s"), ack({ buffer_size: 7, samples_received: 7 }));
    expect(s.bufferSize).toBe(7);
    expect(s.predictionReady).toBe(false);
  });

  it("appends a charted point only for the sample that was actually sent", () => {
    const base = { ...emptyState("s"), pendingSample: demoSample(0) };
    const s = reduce(base, ack());
    expect(s.history).toHaveLength(1);
    expect(s.latestSample).not.toBeNull();
    expect(s.pendingSample).toBeUndefined();
  });

  it("keeps SINR null when the device did not report it", () => {
    const base = { ...emptyState("s"), pendingSample: demoSample(0, { withSinr: false }) };
    const s = reduce(base, ack({ sinr_source: "unavailable" }));
    expect(s.history[0].sinr).toBeNull();
    expect(s.sinrSource).toBe("unavailable");
  });

  it("bounds telemetry history", () => {
    let s = emptyState("s");
    for (let i = 0; i < MAX_TELEMETRY_POINTS + 40; i++) {
      s = reduce({ ...s, pendingSample: demoSample(i) }, ack({ buffer_size: i + 1 }));
    }
    expect(s.history).toHaveLength(MAX_TELEMETRY_POINTS);
  });

  it("bounds the event log", () => {
    let s = emptyState("s");
    for (let i = 0; i < MAX_EVENTS + 25; i++) s = reduce(s, ack());
    expect(s.events).toHaveLength(MAX_EVENTS);
  });
});

describe("connection transitions", () => {
  it("moves to connected and adopts the backend session id", () => {
    const s = reduce(emptyState("local"), {
      type: "connected", session_id: "srv-1", required_window_steps: 26,
      protocol: { client: ["telemetry"], server: ["decision"] },
    });
    expect(s.connection).toBe("connected");
    expect(s.sessionId).toBe("srv-1");
    expect(s.requiredWindow).toBe(26);
  });

  it("records errors without losing prior state", () => {
    const withData = reduce(emptyState("s"), ack({ buffer_size: 4 }));
    const s = reduce(withData, { type: "error", code: "invalid_json", message: "bad frame" });
    expect(s.lastError).toBe("bad frame");
    expect(s.bufferSize).toBe(4);
    expect(s.events[0].kind).toBe("error");
  });

  it("ignores pong frames", () => {
    const before = reduce(emptyState("s"), ack());
    expect(reduce(before, { type: "pong", session_id: "s" })).toEqual(before);
  });

  it("stores the not-ready reason from a status frame", () => {
    const s = reduce(emptyState("s"), {
      type: "status", session_id: "s", prediction_ready: false,
      buffer_size: 18, required_window_steps: 26, reason: "waiting for a full telemetry window",
    });
    expect(s.predictionReady).toBe(false);
    expect(s.statusReason).toContain("waiting");
  });
});

describe("decision handling", () => {
  it("stores the decision and marks prediction ready", () => {
    const s = reduce(emptyState("s"), decisionMsg());
    expect(s.decision?.decision).toBe("STAY");
    expect(s.decision?.model_version).toBe("phase1-v1.0.0");
    expect(Object.keys(s.decision!.risk_probabilities!)).toHaveLength(3);
    expect(s.predictionReady).toBe(true);
    expect(s.provenance?.rsrp_dbm).toBe("real_cellular");
  });

  it("logs a handover event when the engine switches", () => {
    const msg = decisionMsg();
    msg.decision.decision = "SWITCH";
    msg.decision.selected_path = "MEO-1";
    const s = reduce(emptyState("s"), msg);
    expect(s.events[0].kind).toBe("handover");
    expect(s.events[0].text).toContain("LEO-1 → MEO-1");
  });

  it("exposes only backend-provided score components", () => {
    const rows = scoreBreakdown(candidate());
    expect(rows.map((r) => r.label)).toEqual([
      "Quality", "Latency", "Throughput", "Load penalty", "Switch penalty", "Risk penalty",
    ]);
  });
});

describe("demo stream is clearly synthetic but backend-valid", () => {
  it("is deterministic for a given index", () => {
    expect(demoSample(5)).toEqual(demoSample(5));
  });

  it("stays inside the backend's plausible cellular ranges", () => {
    for (let i = 0; i < 60; i++) {
      const s = demoSample(i);
      expect(s.rsrp_dbm).toBeGreaterThanOrEqual(-140);
      expect(s.rsrp_dbm).toBeLessThanOrEqual(-40);
      expect(s.rsrq_db).toBeGreaterThanOrEqual(-25);
      expect(s.rsrq_db).toBeLessThanOrEqual(-3);
      expect(s.rssi_dbm).toBeGreaterThanOrEqual(-120);
      expect(s.rssi_dbm).toBeLessThanOrEqual(-30);
      expect(s.sinr_db!).toBeGreaterThanOrEqual(-20);
      expect(s.sinr_db!).toBeLessThanOrEqual(40);
    }
  });

  it("can omit SINR to exercise the unavailable path", () => {
    expect(demoSample(3, { withSinr: false }).sinr_db).toBeNull();
  });
});

describe("session handling", () => {
  it("starts from a clean bounded state", () => {
    const s = emptyState("abc");
    expect(s.sessionId).toBe("abc");
    expect(s.connection).toBe("disconnected");
    expect(s.history).toEqual([]);
    expect(s.decision).toBeNull();
  });
});
