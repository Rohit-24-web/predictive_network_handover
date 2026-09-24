/**
 * WebSocket client for the existing M7 protocol.
 *
 * Responsibilities: connect, disconnect, auto-reconnect with backoff, route frames
 * through the pure reducer, and tear everything down on unmount. It holds no
 * decision logic — the backend owns that.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { emptyState, parseMessage, reduce } from "../services/protocol";
import type { DashboardState } from "../services/state";
import type { CellularSample } from "../types/protocol";

const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000];

export interface UseHandoverSocket {
  state: DashboardState;
  connect: () => void;
  disconnect: () => void;
  sendTelemetry: (sample: CellularSample, mode: "predictive" | "reactive") => boolean;
  wsUrl: string;
  setWsUrl: (url: string) => void;
}

/**
 * `role` selects the transport:
 *   "producer" -> /ws/{id}          sends telemetry (demo stream)
 *   "observer" -> /ws/observe/{id}  read-only live view of a real device session
 */
export function useHandoverSocket(
  initialUrl: string,
  sessionId: string,
  role: "producer" | "observer" = "producer",
): UseHandoverSocket {
  const [wsUrl, setWsUrl] = useState(initialUrl);
  const [state, setState] = useState<DashboardState>(() => emptyState(sessionId));

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<number | null>(null);
  const attemptsRef = useRef(0);
  // Distinguishes a user-initiated close (no reconnect) from a dropped link.
  const intentionalRef = useRef(false);

  const clearReconnect = () => {
    if (reconnectRef.current !== null) {
      window.clearTimeout(reconnectRef.current);
      reconnectRef.current = null;
    }
  };

  const connect = useCallback(() => {
    if (socketRef.current && socketRef.current.readyState <= WebSocket.OPEN) return;
    intentionalRef.current = false;
    clearReconnect();

    setState((s) => ({
      ...s,
      connection: attemptsRef.current > 0 ? "reconnecting" : "connecting",
      lastError: null,
    }));

    let ws: WebSocket;
    try {
      const path = role === "observer" ? "ws/observe" : "ws";
      ws = new WebSocket(`${wsUrl.replace(/\/$/, "")}/${path}/${sessionId}`);
    } catch (err) {
      setState((s) => ({ ...s, connection: "error", lastError: String(err) }));
      return;
    }
    socketRef.current = ws;

    ws.onopen = () => {
      attemptsRef.current = 0;
      setState((s) => ({ ...s, connection: "connected" }));
    };

    ws.onmessage = (ev) => {
      const msg = parseMessage(String(ev.data));
      setState((s) => reduce(s, msg));
    };

    ws.onerror = () => {
      setState((s) => ({
        ...s,
        lastError: "WebSocket error — is the backend running on this URL?",
      }));
    };

    ws.onclose = () => {
      socketRef.current = null;
      if (intentionalRef.current) {
        setState((s) => ({ ...s, connection: "disconnected" }));
        return;
      }
      const delay = RECONNECT_DELAYS_MS[Math.min(attemptsRef.current, RECONNECT_DELAYS_MS.length - 1)];
      attemptsRef.current += 1;
      setState((s) => ({ ...s, connection: "reconnecting" }));
      reconnectRef.current = window.setTimeout(connect, delay);
    };
  }, [wsUrl, sessionId, role]);

  const disconnect = useCallback(() => {
    intentionalRef.current = true;
    attemptsRef.current = 0;
    clearReconnect();
    socketRef.current?.close();
    socketRef.current = null;
    setState((s) => ({ ...s, connection: "disconnected" }));
  }, []);

  const sendTelemetry = useCallback(
    (sample: CellularSample, mode: "predictive" | "reactive") => {
      const ws = socketRef.current;
      // An observer socket is read-only by construction; refuse locally rather than
      // letting the backend reject the frame.
      if (role === "observer") return false;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      // Stash the sample so the matching ack can attach it to the chart history.
      setState((s) => ({ ...s, pendingSample: sample }));
      ws.send(JSON.stringify({ type: "telemetry", sample, mode }));
      return true;
    },
    [role],
  );

  // Tear down on unmount: no dangling socket, no pending reconnect timer.
  useEffect(() => () => {
    intentionalRef.current = true;
    clearReconnect();
    socketRef.current?.close();
    socketRef.current = null;
  }, []);

  return { state, connect, disconnect, sendTelemetry, wsUrl, setWsUrl };
}
