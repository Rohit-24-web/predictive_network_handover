/**
 * Producer-session discovery.
 *
 * The handset generates its own session id and the dashboard generates its own.
 * Rather than forcing them to agree in advance, the dashboard asks the backend
 * which sessions are producing telemetry and subscribes to one. That keeps the
 * phone authoritative over its own identity.
 */
import type { SessionSummary } from "../types/protocol";

/** Derive the HTTP origin from the configured ws:// URL. */
export function httpBase(wsUrl: string): string {
  return wsUrl.replace(/^ws/, "http").replace(/\/$/, "");
}

export async function fetchSessions(wsUrl: string): Promise<SessionSummary[]> {
  const res = await fetch(`${httpBase(wsUrl)}/sessions`);
  if (!res.ok) throw new Error(`session list failed: HTTP ${res.status}`);
  const body = await res.json();
  return (body.sessions ?? []) as SessionSummary[];
}

/** A session is a real device if it was not created by this dashboard's demo stream. */
export function isDeviceSession(s: SessionSummary, ownSessionId: string): boolean {
  return s.session_id !== ownSessionId;
}
