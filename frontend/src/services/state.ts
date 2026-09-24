/** Dashboard state shape, kept separate so the reducer stays import-light. */
export type {
  CandidateScore, CellularSample, ConnectionState, DecisionBody,
  NormalizedTelemetry, ServerMessage, SessionSummary, Severity, SinrSource,
} from "../types/protocol";

import type {
  CellularSample, ConnectionState, DecisionBody, NormalizedTelemetry, SinrSource,
} from "../types/protocol";

export interface TelemetryPoint {
  t: number;
  rsrp: number;
  sinr: number | null;
}

export interface TimelineEvent {
  at: number;
  kind: "connection" | "telemetry" | "decision" | "handover" | "error";
  text: string;
}

export interface DashboardState {
  sessionId: string;
  connection: ConnectionState;
  requiredWindow: number | null;
  bufferSize: number;
  samplesReceived: number;
  predictionReady: boolean;
  statusReason: string | null;
  latestSample: CellularSample | NormalizedTelemetry | null;
  /** Set locally when a sample is sent, consumed by the matching ack. */
  pendingSample?: CellularSample;
  sinrSource: SinrSource | null;
  warnings: string[];
  history: TelemetryPoint[];
  decision: DecisionBody | null;
  provenance: Record<string, string> | null;
  notes: string[];
  events: TimelineEvent[];
  lastError: string | null;
}
