import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DecisionPanel } from "./components/DecisionPanel";
import { EventTimeline } from "./components/EventTimeline";
import { Header } from "./components/Header";
import { Metric, Panel } from "./components/Panels";
import { PathCards } from "./components/PathCards";
import { TelemetryCharts } from "./components/TelemetryCharts";
import { TelemetryPanel } from "./components/TelemetryPanel";
import { useHandoverSocket } from "./hooks/useHandoverSocket";
import { fmt, fmtPct, severityOf } from "./services/protocol";
import { demoSample } from "./services/demoStream";
import { fetchSessions, isDeviceSession } from "./services/sessions";
import type { SessionSummary } from "./types/protocol";

const DEFAULT_WS = import.meta.env.VITE_WS_URL ?? "ws://localhost:8000";
const SESSION_ID = `dashboard-${Math.random().toString(36).slice(2, 10)}`;

export default function App() {
  const [mode, setMode] = useState<"predictive" | "reactive">("predictive");
  const [demoRunning, setDemoRunning] = useState(false);
  const demoIndex = useRef(0);

  // "demo"   -> this dashboard produces synthetic samples on its own session
  // "device" -> this dashboard OBSERVES a real handset's session, read-only
  const [source, setSource] = useState<"demo" | "device">("demo");
  const [deviceSession, setDeviceSession] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionError, setSessionError] = useState<string | null>(null);

  const observing = source === "device" && deviceSession !== null;
  const activeSession = observing ? deviceSession! : SESSION_ID;

  const { state, connect, disconnect, sendTelemetry, wsUrl, setWsUrl } =
    useHandoverSocket(DEFAULT_WS, activeSession, observing ? "observer" : "producer");

  const connected = state.connection === "connected";

  const toggleConnection = useCallback(() => {
    if (connected) {
      setDemoRunning(false);
      disconnect();
    } else {
      connect();
    }
  }, [connected, connect, disconnect]);

  // Demo stream: clearly-labelled synthetic input, sent over the same socket so it
  // exercises the real backend path. Stops automatically if the link drops.
  useEffect(() => {
    if (!demoRunning || !connected) return;
    const id = window.setInterval(() => {
      const ok = sendTelemetry(demoSample(demoIndex.current++), mode);
      if (!ok) setDemoRunning(false);
    }, 1000);
    return () => window.clearInterval(id);
  }, [demoRunning, connected, mode, sendTelemetry]);

  useEffect(() => {
    if (!connected && demoRunning) setDemoRunning(false);
  }, [connected, demoRunning]);

  // Poll the producer-session list so a phone that starts later still appears.
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchSessions(wsUrl)
        .then((list) => {
          if (cancelled) return;
          setSessions(list.filter((s) => isDeviceSession(s, SESSION_ID)));
          setSessionError(null);
        })
        .catch((e) => { if (!cancelled) setSessionError(String(e.message ?? e)); });
    };
    load();
    const id = window.setInterval(load, 5000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [wsUrl]);

  // Switching source tears down the old socket; the hook reconnects on the new path.
  const selectDevice = useCallback((sid: string) => {
    disconnect();
    setDemoRunning(false);
    setDeviceSession(sid);
    setSource("device");
  }, [disconnect]);

  const selectDemo = useCallback(() => {
    disconnect();
    setDeviceSession(null);
    setSource("demo");
  }, [disconnect]);

  const decision = state.decision;
  const risk = decision?.degradation_probability ?? null;
  const sev = severityOf(risk);
  const total = state.requiredWindow ?? 26;

  const networkState = useMemo(() => {
    if (!connected) return "Offline";
    if (state.predictionReady) return "Prediction active";
    if (state.bufferSize > 0) return `Collecting ${state.bufferSize}/${total}`;
    return "Awaiting telemetry";
  }, [connected, state.predictionReady, state.bufferSize, total]);

  return (
    <div className="shell">
      <Header
        connection={state.connection}
        sessionId={state.sessionId}
        wsUrl={wsUrl}
        onUrlChange={setWsUrl}
        onToggle={toggleConnection}
        mode={mode}
        onModeChange={setMode}
      />

      {state.lastError && (
        <div className="panel" style={{ padding: "10px 16px", marginBottom: 16, borderColor: "rgba(255,107,107,0.4)" }}>
          <span className="sev-high" style={{ fontSize: "var(--t-sm)" }}>{state.lastError}</span>
        </div>
      )}

      <div className="metric-row">
        <Metric
          label="Cellular signal"
          value={state.latestSample ? fmt(state.latestSample.rsrp_dbm, 1, "dBm") : "N/A"}
          sub={state.latestSample ? `SINR ${fmt(state.latestSample.sinr_db, 1, "dB")}` : "no samples"}
        />
        <Metric
          label="Predicted degradation risk"
          value={fmtPct(risk)}
          tone={`sev-${sev ?? "none"}`}
          sub={sev ? `${sev} · next ${decision?.prediction_horizon_seconds ?? 5}s` : "awaiting prediction"}
        />
        <Metric
          label="Path in use"
          value={decision?.current_path ?? "N/A"}
          sub={decision ? `recommended ${decision.selected_path}` : "simulated candidates"}
        />
        <Metric label="Network state" value={networkState}
                sub={state.predictionReady ? "window full" : `${state.bufferSize}/${total} samples`} />
      </div>

      <div className="main-grid">
        <div style={{ display: "grid", gap: "var(--gap)" }}>
          <TelemetryPanel state={state} demo={!observing && demoRunning} />
          <TelemetryCharts history={state.history} />
        </div>
        <div style={{ display: "grid", gap: "var(--gap)" }}>
          <PathCards decision={decision} />
          <Panel
            title="Telemetry source"
            note={observing
              ? <span className="badge badge-real">Live device</span>
              : <span className="badge badge-demo">Demo input</span>}
          >
            <p className="panel-note" style={{ marginTop: 0 }}>
              {observing
                ? "Read-only view of a handset's session. The phone posts to /telemetry; this dashboard only watches and cannot inject samples."
                : "Deterministic synthetic samples sent from this browser. Not a handset."}
            </p>

            <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
              <button onClick={selectDemo} disabled={source === "demo"}>Demo stream</button>
              <button
                onClick={() => sessions[0] && selectDevice(sessions[0].session_id)}
                disabled={sessions.length === 0}
              >
                Live device
              </button>
            </div>

            {source === "demo" ? (
              <button disabled={!connected} onClick={() => setDemoRunning((v) => !v)}>
                {demoRunning ? "Stop demo stream" : "Start demo stream"}
              </button>
            ) : (
              <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-muted)" }}>
                Observing <code style={{ fontFamily: "var(--font-mono)" }}>{deviceSession}</code>
              </div>
            )}

            <div style={{ marginTop: 14 }}>
              <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-muted)", marginBottom: 6 }}>
                Devices producing telemetry
              </div>
              {sessionError ? (
                <div className="warn-line">{sessionError}</div>
              ) : sessions.length === 0 ? (
                <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-dim)" }}>
                  None detected. Start the Android app and point it at this backend.
                </div>
              ) : (
                sessions.map((s) => (
                  <button
                    key={s.session_id}
                    onClick={() => selectDevice(s.session_id)}
                    disabled={deviceSession === s.session_id}
                    style={{ display: "block", width: "100%", textAlign: "left", marginBottom: 6 }}
                  >
                    <span style={{ fontFamily: "var(--font-mono)" }}>{s.session_id}</span>
                    <span style={{ color: "var(--ink-dim)", fontSize: "var(--t-xs)" }}>
                      {"  "}{s.buffer_size}/{s.required_window_steps}
                      {s.prediction_ready ? " · ready" : ""}
                      {s.network_type ? ` · ${s.network_type}` : ""}
                    </span>
                  </button>
                ))
              )}
            </div>
          </Panel>
        </div>
      </div>

      <div className="bottom-grid">
        <DecisionPanel state={state} />
        <EventTimeline events={state.events} />
      </div>

      <footer className="panel" style={{ marginTop: "var(--gap)", padding: "12px 18px" }}>
        <div className="provenance">
          <span className="badge badge-real">Real</span>
          <span>cellular RSRP, RSRQ, RSSI, SINR, network type</span>
          <span className="badge badge-sim">Simulated</span>
          <span>LEO-1, MEO-1, GEO-1 path conditions, orbital geometry, latency, loss, throughput</span>
        </div>
        <p className="panel-note" style={{ marginTop: 8 }}>
          Decisions shown are simulated traffic-steering choices. Nothing here commands a
          real carrier or satellite handover.
        </p>
      </footer>
    </div>
  );
}
