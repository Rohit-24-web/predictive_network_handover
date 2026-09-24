import type { TimelineEvent } from "../services/state";
import { Panel, Placeholder } from "./Panels";

/** Events are derived from real WebSocket frames only — nothing is synthesised. */
export function EventTimeline({ events }: { events: TimelineEvent[] }) {
  return (
    <Panel title="Event timeline" note={`${events.length} recent`}>
      {events.length === 0 ? (
        <Placeholder headline="No events yet" detail="Activity appears here as frames arrive." />
      ) : (
        <div className="timeline">
          {events.map((e, i) => (
            <div className={`tl-row tl-${e.kind}`} key={`${e.at}-${i}`}>
              <span className="tl-time">{new Date(e.at).toLocaleTimeString("en-GB")}</span>
              <span className="tl-text">{e.text}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
