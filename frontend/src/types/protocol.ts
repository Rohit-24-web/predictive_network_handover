/**
 * Wire types for the existing M7 WebSocket protocol.
 *
 * Mirrors the backend exactly; the dashboard never invents a field. Anything the
 * backend does not send is rendered as N/A rather than guessed.
 */

export type SinrSource = "measured" | "unavailable" | "estimated_from_rsrq";

export interface CellularSample {
  timestamp: number;
  rsrp_dbm: number;
  rsrq_db: number;
  rssi_dbm: number | null;
  sinr_db: number | null;
  network_type: string;
  /** Anonymous label from the handset; may be absent or explicitly null. */
  device_id?: string | null;
}

export interface CandidateScore {
  path_id: string;
  orbit: string;
  available: boolean;
  score: number;
  quality: number;
  latency_score: number;
  tput_score: number;
  load_penalty: number;
  switching_penalty: number;
  risk_penalty: number;
  risk: number | null;
  warming_up: boolean;
}

export interface DecisionBody {
  session_id: string;
  mode: string;
  current_path: string;
  selected_path: string;
  decision: "STAY" | "SWITCH" | string;
  decision_reason: string;
  handover: boolean;
  degradation_probability: number | null;
  risk_probabilities: Record<string, number> | null;
  prediction_horizon_seconds: number | null;
  model_version: string | null;
  candidates: CandidateScore[];
}

export interface ConnectedMsg {
  type: "connected";
  session_id: string;
  required_window_steps: number | null;
  protocol: { client: string[]; server: string[] };
  note?: string;
}

export interface NormalizedTelemetry {
  timestamp: number;
  rsrp_dbm: number;
  rsrq_db: number;
  rssi_dbm: number | null;
  rssi_source: string;
  sinr_db: number | null;
  sinr_source: SinrSource;
  network_type: string;
  device_id?: string | null;
}

export interface SessionSummary {
  session_id: string;
  samples_received: number;
  buffer_size: number;
  required_window_steps: number;
  window_full: boolean;
  prediction_ready: boolean;
  observers: number;
  last_sample_timestamp: number | null;
  network_type: string | null;
  sinr_source: string | null;
}

export interface TelemetryAckMsg {
  type: "telemetry_ack";
  session_id: string;
  /** Present on observer sockets: the observer has no local copy of the sample. */
  normalized?: NormalizedTelemetry;
  samples_received: number;
  buffer_size: number;
  required_window_steps: number;
  window_full: boolean;
  prediction_ready: boolean;
  sinr_source: SinrSource;
  warnings: string[];
}

export interface StatusMsg {
  type: "status";
  session_id: string;
  prediction_ready: boolean;
  buffer_size: number;
  required_window_steps: number;
  reason: string;
}

export interface DecisionMsg {
  type: "decision";
  session_id: string;
  simulation_step: number;
  merged_path: string | null;
  field_provenance: Record<string, string>;
  notes: string[];
  decision: DecisionBody;
}

export interface PongMsg { type: "pong"; session_id: string }
export interface ErrorMsg { type: "error"; code: string; message: string }

export type ServerMessage =
  | ConnectedMsg | TelemetryAckMsg | StatusMsg | DecisionMsg | PongMsg | ErrorMsg;

export type ConnectionState =
  | "disconnected" | "connecting" | "connected" | "reconnecting" | "error";

/** UI-only severity bands. The backend returns a raw probability and no severity. */
export type Severity = "low" | "medium" | "high";
