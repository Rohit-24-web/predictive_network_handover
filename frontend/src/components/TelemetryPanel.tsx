import { fmt, sinrSourceLabel } from "../services/protocol";
import type { DashboardState } from "../services/state";
import { Panel, Placeholder } from "./Panels";

/** Live REAL cellular readouts. Absent values render N/A — never a placeholder number. */
export function TelemetryPanel({ state, demo }: { state: DashboardState; demo: boolean }) {
  const s = state.latestSample;
  const src = sinrSourceLabel(state.sinrSource);
  const srcTone =
    src === "measured" ? "var(--ok)" : src === "estimated" ? "var(--warn)" : "var(--ink-dim)";

  return (
    <Panel
      title="Live cellular telemetry"
      note={demo
        ? <span className="badge badge-demo">Demo input — not a handset</span>
        : <span className="badge badge-real">Real measurements</span>}
    >
      {!s ? (
        <Placeholder
          headline="Waiting for cellular telemetry"
          detail="Connect a handset or start the demo stream to begin receiving samples."
        />
      ) : (
        <>
          <div className="tele-grid">
            <Cell label="RSRP" value={fmt(s.rsrp_dbm, 1, "dBm")} />
            <Cell label="RSRQ" value={fmt(s.rsrq_db, 1, "dB")} />
            <Cell label="RSSI" value={fmt(s.rssi_dbm, 1, "dBm")} />
            <Cell label="SINR" value={fmt(s.sinr_db, 1, "dB")}
                  sub={<span style={{ color: srcTone }}>Source: {src}</span>} />
            <Cell label="Network" value={s.network_type || "N/A"} />
            <Cell label="Samples" value={String(state.samplesReceived)} />
          </div>
          {state.warnings.length > 0 && (
            <div className="warn-line">{state.warnings[0]}</div>
          )}
        </>
      )}
    </Panel>
  );
}

function Cell({ label, value, sub }: { label: string; value: string; sub?: React.ReactNode }) {
  return (
    <div className="tele-cell">
      <div className="label">{label}</div>
      <div className={`value ${value === "N/A" ? "na" : ""}`}>{value}</div>
      {sub ? <div className="src">{sub}</div> : null}
    </div>
  );
}
