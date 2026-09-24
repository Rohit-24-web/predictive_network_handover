import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { TelemetryPoint } from "../services/state";
import { Panel, Placeholder } from "./Panels";

const AXIS = { stroke: "#64748b", fontSize: 11, fontFamily: "IBM Plex Mono" };

/**
 * Only RSRP and SINR are charted: they are the only time-series the backend
 * actually returns per sample. Latency/loss/throughput belong to the simulated
 * candidate paths, so charting them here would blur the real/simulated line.
 */
export function TelemetryCharts({ history }: { history: TelemetryPoint[] }) {
  if (history.length < 2) {
    return (
      <Panel title="Signal history" note="Real cellular · rolling 120 samples">
        <Placeholder headline="Not enough samples to plot yet"
                     detail="The chart appears once two or more telemetry samples arrive." />
      </Panel>
    );
  }

  const data = history.map((p, i) => ({ i, rsrp: p.rsrp, sinr: p.sinr }));
  const hasSinr = history.some((p) => p.sinr !== null);

  return (
    <Panel title="Signal history" note="Real cellular · rolling 120 samples">
      <ChartBlock label="RSRP (dBm)">
        <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid stroke="rgba(120,150,200,0.10)" vertical={false} />
          <XAxis dataKey="i" tick={AXIS} tickLine={false} axisLine={false} minTickGap={28} />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} domain={["dataMin - 3", "dataMax + 3"]} width={54} />
          <Tooltip contentStyle={TOOLTIP} labelFormatter={(v) => `sample ${v}`}
                   formatter={(v: number) => [`${v.toFixed(1)} dBm`, "RSRP"]} />
          <Line type="monotone" dataKey="rsrp" stroke="#7c8cff" strokeWidth={2}
                dot={false} isAnimationActive={false} />
        </LineChart>
      </ChartBlock>

      <ChartBlock label="SINR (dB)">
        {hasSinr ? (
          <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
            <CartesianGrid stroke="rgba(120,150,200,0.10)" vertical={false} />
            <XAxis dataKey="i" tick={AXIS} tickLine={false} axisLine={false} minTickGap={28} />
            <YAxis tick={AXIS} tickLine={false} axisLine={false} domain={["dataMin - 2", "dataMax + 2"]} width={54} />
            <Tooltip contentStyle={TOOLTIP} labelFormatter={(v) => `sample ${v}`}
                     formatter={(v: number) => [`${v.toFixed(1)} dB`, "SINR"]} />
            {/* connectNulls stays false so gaps show as gaps, not invented continuity. */}
            <Line type="monotone" dataKey="sinr" stroke="#35d6a4" strokeWidth={2}
                  dot={false} isAnimationActive={false} connectNulls={false} />
          </LineChart>
        ) : null}
      </ChartBlock>
      {!hasSinr && (
        <div className="warn-line">This device is not reporting SINR, so no SINR series is drawn.</div>
      )}
    </Panel>
  );
}

const TOOLTIP = {
  background: "#0d1526", border: "1px solid rgba(120,150,200,0.3)",
  borderRadius: 8, fontSize: 12, fontFamily: "IBM Plex Mono",
} as const;

function ChartBlock({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: "var(--t-xs)", color: "var(--ink-muted)", marginBottom: 4 }}>{label}</div>
      <div style={{ height: 150 }}>
        {children ? <ResponsiveContainer width="100%" height="100%">{children as any}</ResponsiveContainer> : null}
      </div>
    </div>
  );
}
