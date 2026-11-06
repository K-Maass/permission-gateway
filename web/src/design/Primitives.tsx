import type { ReactElement } from "react";
import { statusLabels, type TaskStatus } from "../api";

// The circle-based task symbols and PAOS aperture come from the control console.
const done = <><circle cx="12" cy="12" r="8.4" /><path d="m8.3 12.2 2.5 2.5 4.9-5.2" /></>;
const cancelled = <><circle cx="12" cy="12" r="8.4" /><path d="M8.6 12h6.8" /></>;

const shapes: Record<TaskStatus, ReactElement> = {
  queued: <circle cx="12" cy="12" r="8.4" />,
  running: <path d="M12 3.6a8.4 8.4 0 1 1-6 2.5" />,
  awaiting_approval: <><circle cx="12" cy="12" r="8.4" /><path d="M10 9.3v5.4M14 9.3v5.4" /></>,
  approved: done,
  succeeded: done,
  denied: cancelled,
  expired: cancelled,
  failed: <><circle cx="12" cy="12" r="8.4" /><path d="m9.3 9.3 5.4 5.4m0-5.4-5.4 5.4" /></>,
};

const glyphs = {
  review: <><path d="M9 6.2h11.3M9 12h11.3M9 17.8h11.3" /><path d="m3.4 5.9 1.3 1.3 2.2-2.4m-3.5 6 1.3 1.3 2.2-2.4m-3.5 6 1.3 1.3 2.2-2.4" /></>,
  memory: <><ellipse cx="12" cy="6" rx="7" ry="2.75" /><path d="M5 6v12c0 1.5 3.1 2.75 7 2.75s7-1.2 7-2.75V6M5 12c0 1.5 3.1 2.75 7 2.75s7-1.2 7-2.75" /></>,
  worker: <><rect x="7.6" y="7.6" width="8.8" height="8.8" rx="2.2" transform="rotate(45 12 12)" /><circle cx="12" cy="12" r="1.5" /></>,
  artifact: <><path d="M6.2 3.6h7.4l4.2 4.2v12.6H6.2Z" /><path d="M13.6 3.6v4.2h4.2" /></>,
  download: <><path d="M12 3.5v12m-4.5-4.5 4.5 4.5 4.5-4.5M5 17v3.5h14V17" /></>,
  arrow: <path d="M4.5 12h14m-5-5 5 5-5 5" />,
  lock: <><rect x="5.5" y="10.3" width="13" height="10" rx="2" /><path d="M8 10.3V7a4 4 0 0 1 8 0v3.3" /></>,
};

function Symbol({ children }: { children: ReactElement }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.55"
      strokeLinecap="round" strokeLinejoin="round" className="icon" aria-hidden="true">
      {children}
    </svg>
  );
}

export function Icon({ name }: { name: keyof typeof glyphs }) {
  return <Symbol>{glyphs[name]}</Symbol>;
}

export function Status({ value, primary = false }: { value: TaskStatus; primary?: boolean }) {
  return (
    <span className="status" data-status={value} data-testid={primary ? "task-status" : undefined}
      aria-live={primary ? "polite" : undefined}>
      <Symbol>{shapes[value]}</Symbol>
      {statusLabels[value]}
    </span>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <span className="presence" aria-hidden="true"><span /></span>
      <strong>PAOS Core</strong>
    </div>
  );
}
