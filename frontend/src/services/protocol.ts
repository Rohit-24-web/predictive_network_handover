/**
 * Pure message-handling logic, deliberately separated from React and from the
 * WebSocket object so it can be unit-tested with no DOM and no server.
 *
 * The dashboard VISUALISES backend results. No scoring, no thresholds on the
 * decision itself, no prediction — those all live in the backend.
 */
import type {
  CandidateScore, DashboardState, ServerMessage, Severity, TimelineEvent,
} from "./state";

/** Parse a raw frame. Malformed input yields an error message, never a throw. */
export function parseMessage(raw: string): ServerMessage {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { type: "error", code: "client_parse_error", message: "Received malformed JSON from the backend." };
  }
  if (typeof data !== "object" || data === null || !("type" in data)) {
    return { type: "error", code: "client_parse_error", message: "Received a frame with no message type." };
  }
  const type = (data as { type: unknown }).type;
  if (typeof type !== "string") {
    return { type: "error", code: "client_parse_error", message: "Message type was not a string." };
  }
  return data as ServerMessage;
}

/**
 * UI-only risk bands.
 *
 * The backend returns a raw probability and does not classify severity, so these
 * thresholds exist purely for colour and labelling. They are documented here and
 * in the README so nobody mistakes them for model output.
 */
export const RISK_THRESHOLDS = { medium: 0.25, high: 0.6 } as const;

export function severityOf(risk: number | null | undefined): Severity | null {
  if (risk === null || risk === undefined || Number.isNaN(risk)) return null;
  if (risk >= RISK_THRESHOLDS.high) return "high";
  if (risk >= RISK_THRESHOLDS.medium) return "medium";
  return "low";
}

/** Format a value that may legitimately be absent. Never invents a number. */
export function fmt(value: number | null | undefined, digits = 1, unit = ""): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
  return `${value.toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

export function fmtPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "N/A";
  return `${(value * 100).toFixed(digits)}%`;
}

export function sinrSourceLabel(source: string | null): string {
  switch (source) {
    case "measured": return "measured";
    case "estimated_from_rsrq": return "estimated";
    case "unavailable": return "unavailable";
    default: return "unknown";
  }
}

/** Score contributions, in the order the backend's formula applies them. */
export function scoreBreakdown(c: CandidateScore) {
  return [
    { label: "Quality", value: c.quality, kind: "gain" as const },
    { label: "Latency", value: c.latency_score, kind: "gain" as const },
    { label: "Throughput", value: c.tput_score, kind: "gain" as const },
    { label: "Load penalty", value: c.load_penalty, kind: "cost" as const },
    { label: "Switch penalty", value: c.switching_penalty, kind: "cost" as const },
    { label: "Risk penalty", value: c.risk_penalty, kind: "cost" as const },
  ];
}

export const MAX_TELEMETRY_POINTS = 120;
export const MAX_EVENTS = 60;

function pushEvent(events: TimelineEvent[], event: TimelineEvent): TimelineEvent[] {
  return [event, ...events].slice(0, MAX_EVENTS);
}

export function emptyState(sessionId: string): DashboardState {
  return {
    sessionId,
    connection: "disconnected",
    requiredWindow: null,
    bufferSize: 0,
    samplesReceived: 0,
    predictionReady: false,
    statusReason: null,
    latestSample: null,
    sinrSource: null,
    warnings: [],
    history: [],
    decision: null,
    provenance: null,
    notes: [],
    events: [],
    lastError: null,
  };
}

/**
 * Fold one server frame into dashboard state.
 *
 * A reducer rather than scattered setState calls: it keeps every transition in one
 * testable place, and guarantees the telemetry and event histories stay bounded so
 * a long-running demo cannot grow without limit.
 */
export function reduce(
  state: DashboardState,
  msg: ServerMessage,
  now: number = Date.now(),
): DashboardState {
  switch (msg.type) {
    case "connected":
      return {
        ...state,
        connection: "connected",
        sessionId: msg.session_id,
        requiredWindow: msg.required_window_steps,
        events: pushEvent(state.events, {
          at: now, kind: "connection", text: `Connected to session ${msg.session_id}`,
        }),
      };

    case "telemetry_ack": {
      // An observer socket did not send the sample, so the backend includes the
      // normalised values in the frame. Prefer those; fall back to the locally
      // stashed sample for the producer (demo) path.
      const sample = msg.normalized ?? state.pendingSample ?? null;
      const history = sample
        ? [...state.history, {
            t: sample.timestamp,
            rsrp: sample.rsrp_dbm,
            sinr: sample.sinr_db,
          }].slice(-MAX_TELEMETRY_POINTS)
        : state.history;

      return {
        ...state,
        bufferSize: msg.buffer_size,
        samplesReceived: msg.samples_received,
        requiredWindow: msg.required_window_steps,
        predictionReady: msg.prediction_ready,
        sinrSource: msg.sinr_source,
        warnings: msg.warnings,
        latestSample: sample ?? state.latestSample,
        history,
        pendingSample: undefined,
        events: pushEvent(state.events, {
          at: now,
          kind: "telemetry",
          text: `Telemetry accepted — buffer ${msg.buffer_size}/${msg.required_window_steps}`,
        }),
      };
    }

    case "status":
      return {
        ...state,
        predictionReady: msg.prediction_ready,
        bufferSize: msg.buffer_size,
        requiredWindow: msg.required_window_steps,
        statusReason: msg.reason,
      };

    case "decision": {
      const d = msg.decision;
      const switched = d.decision === "SWITCH";
      return {
        ...state,
        predictionReady: true,
        statusReason: null,
        decision: d,
        provenance: msg.field_provenance,
        notes: msg.notes,
        events: pushEvent(state.events, {
          at: now,
          kind: switched ? "handover" : "decision",
          text: switched
            ? `Handover ${d.current_path} → ${d.selected_path}`
            : `Stay on ${d.current_path} — ${d.decision_reason}`,
        }),
      };
    }

    case "error":
      return {
        ...state,
        lastError: msg.message,
        events: pushEvent(state.events, {
          at: now, kind: "error", text: `${msg.code}: ${msg.message}`,
        }),
      };

    case "pong":
    default:
      return state;
  }
}
