# Predictive Network Handover — Live Dashboard (M8)

React + TypeScript + Vite dashboard for the existing FastAPI backend. It connects to
the M7 WebSocket, streams telemetry, and visualises the prediction and the
deterministic steering decision.

The dashboard **visualises backend results**. It contains no model, no scoring and no
decision logic.

## Real vs simulated

| REAL | SIMULATED |
|---|---|
| Cellular RSRP, RSRQ, RSSI, SINR, network type | LEO-1 / MEO-1 / GEO-1 path conditions |
| Sample timestamps and counts | Orbital geometry, latency, packet loss, throughput, load |

Both are labelled on screen: a green **Real cellular telemetry** badge on the telemetry
panel, a grey **Simulated** badge on the candidate paths and decision panels, and a
provenance strip in the footer. Decisions are described as *simulated traffic steering*
— nothing here commands a real carrier or satellite handover.

## Run it

```bash
# 1. Backend (from backend/)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 2. Dashboard (from frontend/)
npm install
npm run dev          # http://localhost:5173
```

Other commands:

```bash
npm run build        # type-check (tsc -b) + production bundle into dist/
npm test             # vitest, 24 tests
npm run preview      # serve the production build
```

## Backend WebSocket configuration

The dashboard connects to `<base>/ws/<session_id>`. The base URL comes from, in order:

1. the URL field in the header (editable while disconnected), then
2. `VITE_WS_URL`, then
3. the default `ws://localhost:8000`.

```bash
VITE_WS_URL=ws://192.168.1.20:8000 npm run dev
```

The session id is generated per page load. Reloading starts a fresh session; the
backend keeps its own buffer per session id.

## Demo stream

With no Android handset, press **Start demo stream**. It sends deterministic synthetic
samples over the same WebSocket, so the full backend path runs unchanged.

It is labelled **Demo input — not a handset** wherever it is the source, and the
telemetry panel's badge switches from "Real measurements" to the demo badge while it
runs. No backend change was needed for it.

Values stay inside the backend's plausible cellular ranges, drift slowly and dip
periodically, so the charts and the risk gauge visibly move during a demo.

## Risk thresholds are UI-only

The backend returns a raw probability and does not classify severity. The dashboard
bands it purely for colour and labelling:

| Band | Range |
|---|---|
| low | < 25% |
| medium | 25% – 60% |
| high | ≥ 60% |

These live in `RISK_THRESHOLDS` (`src/services/protocol.ts`). They are presentation
only and are not model output.

## Missing values

Anything the backend does not send renders as **N/A**. The SINR readout always shows
its provenance — `measured`, `estimated`, or `unavailable` — and when a device reports
no SINR the chart draws a gap rather than interpolating across it.

## Structure

```
src/
  components/   Header, Panels, TelemetryPanel, TelemetryCharts,
                PathCards, DecisionPanel, EventTimeline
  hooks/        useHandoverSocket — connect/disconnect/reconnect, clean unmount
  services/     protocol.ts (pure parsing + reducer), state.ts, demoStream.ts
  types/        protocol.ts — mirrors the M7 wire format
  styles/       tokens.css (design tokens), app.css
```

All message handling is a pure reducer in `services/protocol.ts`, separate from React
and from the socket, which is why it can be unit-tested with no DOM and no server.
Telemetry history is capped at 120 points and the event log at 60 entries, so a
long-running demo cannot grow without bound.
