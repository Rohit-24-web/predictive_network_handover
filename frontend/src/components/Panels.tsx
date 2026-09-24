import type { ReactNode } from "react";

export function Panel({ title, note, children, bodyClass }: {
  title: string; note?: ReactNode; children: ReactNode; bodyClass?: string;
}) {
  return (
    <section className="panel">
      <header className="panel-head">
        <h2 className="panel-title">{title}</h2>
        {note ? <span className="panel-note">{note}</span> : null}
      </header>
      <div className={bodyClass ?? "panel-body"}>{children}</div>
    </section>
  );
}

export function Metric({ label, value, sub, tone }: {
  label: string; value: string; sub?: ReactNode; tone?: string;
}) {
  const na = value === "N/A";
  return (
    <div className="panel metric">
      <div className="label">{label}</div>
      <div className={`value ${na ? "na" : ""} ${tone ?? ""}`}>{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

/** Empty/loading/error state. Never leaves a panel blank. */
export function Placeholder({ headline, detail, progress }: {
  headline: string; detail?: string; progress?: { done: number; total: number };
}) {
  return (
    <div className="empty">
      <div className="headline">{headline}</div>
      {detail ? <div className="detail">{detail}</div> : null}
      {progress && progress.total > 0 ? (
        <>
          <div className="progress-track" role="progressbar"
               aria-valuenow={progress.done} aria-valuemin={0} aria-valuemax={progress.total}>
            <span style={{ width: `${Math.min(100, (progress.done / progress.total) * 100)}%` }} />
          </div>
          <div className="detail">{progress.done} of {progress.total} samples</div>
        </>
      ) : null}
    </div>
  );
}
