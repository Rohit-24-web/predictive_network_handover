import { fmtPct, scoreBreakdown, severityOf } from "../services/protocol";
import type { DashboardState } from "../services/state";
import { Panel, Placeholder } from "./Panels";

export function DecisionPanel({ state }: { state: DashboardState }) {
  const d = state.decision;

  if (!d) {
    const total = state.requiredWindow ?? 26;
    const warming = state.bufferSize > 0 && state.bufferSize < total;
    return (
      <Panel title="Decision engine" note="Deterministic policy">
        {warming ? (
          <Placeholder headline="Collecting telemetry"
                       detail={state.statusReason ?? "The model needs a full window before it will predict."}
                       progress={{ done: state.bufferSize, total }} />
        ) : (
          <Placeholder
            headline={state.connection === "connected" ? "Prediction engine idle" : "Not connected"}
            detail={state.connection === "connected"
              ? (state.statusReason ?? "Waiting for telemetry samples.")
              : "Connect to the backend to begin live monitoring."} />
        )}
      </Panel>
    );
  }

  const switched = d.decision === "SWITCH";
  const sev = severityOf(d.degradation_probability);
  const incumbent = d.candidates.find((c) => c.path_id === d.current_path);

  return (
    <Panel
      title="Decision engine"
      note={<span className="badge badge-sim">Simulated traffic steering</span>}
    >
      <div className={`decision-hero ${switched ? "switch" : ""}`}>
        <div>
          <div className="decision-verb">
            {switched ? `Switch → ${d.selected_path}` : `Stay on ${d.current_path}`}
          </div>
          <div className="decision-reason">{d.decision_reason}</div>
        </div>
      </div>

      <div className="decision-meta">
        <Meta label="Predicted risk" value={fmtPct(d.degradation_probability)} tone={`sev-${sev ?? "none"}`} />
        <Meta label="Mode" value={d.mode} />
        <Meta label="Horizon" value={d.prediction_horizon_seconds ? `${d.prediction_horizon_seconds}s` : "N/A"} />
        <Meta label="Model" value={d.model_version ?? "not used"} />
        <Meta label="Current path" value={d.current_path} />
        <Meta label="Recommended" value={d.selected_path} />
      </div>

      {incumbent && (
        <div style={{ marginTop: 18 }}>
          <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-muted)", marginBottom: 8 }}>
            Score breakdown — {incumbent.path_id}
          </div>
          {scoreBreakdown(incumbent).map((row) => (
            <div className="breakdown-row" key={row.label}>
              <span className="label">{row.label}</span>
              <span className="bar">
                <span
                  style={{
                    width: `${Math.min(100, Math.abs(row.value) * 100)}%`,
                    background: row.kind === "cost" ? "var(--crit)" : "var(--active)",
                  }}
                />
              </span>
              <span className="num">{row.value.toFixed(2)}</span>
            </div>
          ))}
        </div>
      )}

      {state.notes.length > 0 && (
        <p className="panel-note" style={{ marginTop: 14 }}>{state.notes[0]}</p>
      )}
    </Panel>
  );
}

function Meta({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-muted)" }}>{label}</div>
      <div className={tone} style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-base)" }}>{value}</div>
    </div>
  );
}
