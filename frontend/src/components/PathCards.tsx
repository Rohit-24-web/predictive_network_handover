import { fmt, fmtPct, severityOf } from "../services/protocol";
import type { CandidateScore, DecisionBody } from "../types/protocol";
import { Panel, Placeholder } from "./Panels";

/**
 * SIMULATED candidate satellite paths. Labelled as such on the panel so they are
 * never mistaken for handset measurements.
 */
export function PathCards({ decision }: { decision: DecisionBody | null }) {
  return (
    <Panel title="Candidate paths" note={<span className="badge badge-sim">Simulated</span>}>
      {!decision ? (
        <Placeholder headline="No decision yet"
                     detail="Candidate scores appear once the telemetry window is full." />
      ) : (
        decision.candidates.map((c) => (
          <PathCard key={c.path_id} c={c} selected={c.path_id === decision.selected_path} />
        ))
      )}
    </Panel>
  );
}

function PathCard({ c, selected }: { c: CandidateScore; selected: boolean }) {
  const sev = severityOf(c.risk);
  const riskPct = c.risk === null ? 0 : Math.min(100, c.risk * 100);

  return (
    <article className={`path-card ${selected ? "selected" : ""} ${c.available ? "" : "unavailable"}`}>
      <div className="path-head">
        <span className="path-id">{c.path_id}</span>
        <span className="path-total">{Number.isFinite(c.score) ? c.score.toFixed(3) : "—"}</span>
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontSize: "var(--t-xs)" }}>
        <span style={{ color: "var(--ink-muted)" }}>Degradation risk</span>
        <span className={`sev-${sev ?? "none"}`} style={{ fontFamily: "var(--font-mono)" }}>
          {fmtPct(c.risk)}
        </span>
      </div>
      <div className="bar"><span className={`bg-${sev ?? "none"}`} style={{ width: `${riskPct}%` }} /></div>

      <dl className="path-rows">
        <dt>Quality</dt><dd>{fmt(c.quality, 2)}</dd>
        <dt>Latency</dt><dd>{fmt(c.latency_score, 2)}</dd>
        <dt>Throughput</dt><dd>{fmt(c.tput_score, 2)}</dd>
        <dt>Load penalty</dt><dd>{fmt(c.load_penalty, 2)}</dd>
        <dt>Switch penalty</dt><dd>{fmt(c.switching_penalty, 2)}</dd>
        <dt>Risk penalty</dt><dd>{fmt(c.risk_penalty, 2)}</dd>
      </dl>

      <div style={{ marginTop: 8, fontSize: "var(--t-xs)" }}>
        {!c.available && <span className="sev-high">Unavailable</span>}
        {c.available && selected && <span style={{ color: "var(--active)" }}>● Path in use</span>}
        {c.available && !selected && <span style={{ color: "var(--ink-dim)" }}>Standby</span>}
      </div>
    </article>
  );
}
