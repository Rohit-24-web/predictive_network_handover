import type { ConnectionState } from "../types/protocol";

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  disconnected: "Disconnected",
  connecting: "Connecting",
  connected: "Connected",
  reconnecting: "Reconnecting",
  error: "Error",
};

export function Header(props: {
  connection: ConnectionState;
  sessionId: string;
  wsUrl: string;
  onUrlChange: (v: string) => void;
  onToggle: () => void;
  mode: "predictive" | "reactive";
  onModeChange: (m: "predictive" | "reactive") => void;
}) {
  const live = props.connection === "connected";
  const tone = live ? "var(--ok)" : props.connection === "error" ? "var(--crit)" : "var(--ink-dim)";

  return (
    <header className="masthead">
      <div>
        <h1>Predictive Network Handover</h1>
        <p className="sub">Real-time intelligent network steering</p>
      </div>

      <span className="badge badge-real">
        <span className="dot" style={{ background: "var(--real)" }} />
        Real cellular telemetry
      </span>
      <span className="badge badge-sim">Simulated satellite paths</span>

      <div className="spacer" />

      <div className="masthead-controls">
        <span className="badge" style={{ color: tone, borderColor: "var(--hairline)" }}>
          <span className={`dot ${live ? "dot-live" : ""}`} style={{ background: tone, color: tone }} />
          {CONNECTION_LABEL[props.connection]}
        </span>

        <input
          className="url"
          aria-label="Backend WebSocket URL"
          value={props.wsUrl}
          onChange={(e) => props.onUrlChange(e.target.value)}
          disabled={live}
          spellCheck={false}
        />

        <select
          className="mode"
          aria-label="Decision mode"
          value={props.mode}
          onChange={(e) => props.onModeChange(e.target.value as "predictive" | "reactive")}
        >
          <option value="predictive">Predictive</option>
          <option value="reactive">Reactive</option>
        </select>

        <button className={live ? "danger" : ""} onClick={props.onToggle}>
          {live ? "Disconnect" : "Connect"}
        </button>
      </div>

      <div style={{ flexBasis: "100%", fontSize: "var(--t-xs)", color: "var(--ink-dim)" }}>
        Session <code style={{ fontFamily: "var(--font-mono)" }}>{props.sessionId}</code>
      </div>
    </header>
  );
}
