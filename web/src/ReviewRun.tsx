import { useEffect, useState } from "react";
import { api, errorMessage, isUnauthorized, terminal, type Decision, type Task } from "./api";
import { Icon, Status } from "./design/Primitives";
import type { Connection } from "./useTaskStream";

const connectionLabels: Record<Connection, string> = {
  connecting: "Connecting",
  live: "Live updates",
  reconnecting: "Reconnecting",
  settled: "Recorded",
};

const clock = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export function ReviewRun({ task, connection, deciding, decisionError, onDecision, onUnauthorized }: {
  task: Task;
  connection: Connection;
  deciding: boolean;
  decisionError: string;
  onDecision: (decision: Decision) => void;
  onUnauthorized: () => void;
}) {
  return (
    <article aria-label="Review run">
      <div className="run-bar">
        <div className="run-id"><span className="label">Run</span><code>{task.id}</code></div>
        <Status value={task.status} primary />
      </div>
      <div className="run-grid">
        <div className="main-evidence">
          <section className="section" aria-labelledby="activity-title">
            <div className="section-heading">
              <h2 id="activity-title">Activity</h2>
              <span className="connection" data-connection={connection} role="status">
                <span aria-hidden="true" />{connectionLabels[connection]}
              </span>
            </div>
            {task.timeline.length ? (
              <ol className="timeline">
                {task.timeline.map((event) => (
                  <li key={event.seq}>
                    <span className="timeline-dot" aria-hidden="true" />
                    <p>{event.message}</p>
                    <time className="muted mono" dateTime={event.at} title={event.at}>
                      {Number.isNaN(Date.parse(event.at)) ? event.at : clock.format(new Date(event.at))}
                    </time>
                  </li>
                ))}
              </ol>
            ) : <p className="section-empty">Events will appear as the worker starts.</p>}
          </section>
          <Artifacts task={task} onUnauthorized={onUnauthorized} />
        </div>

        <div className="side-evidence">
          <Routing route={task.route} />
          <Approval task={task} deciding={deciding} error={decisionError} onDecision={onDecision} />
          {task.receipt && (
            <section className="section receipt" aria-labelledby="receipt-title">
              <div className="section-heading"><h2 id="receipt-title">Approval receipt</h2></div>
              <p>The revision was recorded in the local workspace.</p>
              <dl>
                <dt>Receipt ID</dt><dd><code>{task.receipt.id}</code></dd>
                <dt>Recorded artifact SHA-256</dt><dd><code>{task.receipt.artifact_sha256}</code></dd>
              </dl>
            </section>
          )}
        </div>
      </div>
    </article>
  );
}

function Routing({ route }: { route: Task["route"] }) {
  return (
    <section className="section" aria-labelledby="route-title">
      <div className="section-heading"><h2 id="route-title">Worker routing</h2><Icon name="worker" /></div>
      {route?.selected ? (
        <dl className="routing">
          <dt className="label">Selected worker</dt>
          <dd><code>{route.selected}</code></dd>
          {route.excluded.length > 0 && (
            <>
              <dt className="label">Excluded workers</dt>
              <dd>
                <ul>
                  {route.excluded.map(({ worker, reason }) => (
                    <li key={worker}><code>{worker}</code><p>{reason}</p></li>
                  ))}
                </ul>
              </dd>
            </>
          )}
        </dl>
      ) : <p className="section-empty">The worker selection will appear here.</p>}
    </section>
  );
}

function Approval({ task, deciding, error, onDecision }: {
  task: Task;
  deciding: boolean;
  error: string;
  onDecision: (decision: Decision) => void;
}) {
  return (
    <section className="section" aria-labelledby="approval-title">
      <div className="section-heading"><h2 id="approval-title">Operator decision</h2><Icon name="lock" /></div>
      {task.status === "awaiting_approval" && task.approval ? (
        <ApprovalPrompt expiresAt={task.approval.expires_at} deciding={deciding} onDecision={onDecision} />
      ) : <p className="section-empty">{approvalSummary(task.status)}</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

function approvalSummary(status: Task["status"]) {
  switch (status) {
    case "approved":
      return "Approved. PAOS Core is recording the revision.";
    case "succeeded":
      return "Approved. The local receipt is available.";
    case "denied":
      return "The operator denied this revision.";
    case "expired":
      return "The approval window expired.";
    case "failed":
      return "The review ended with an error. See Activity for details.";
    default:
      return "A decision will be available when the proposed revision is ready.";
  }
}

function ApprovalPrompt({ expiresAt, deciding, onDecision }: {
  expiresAt: string;
  deciding: boolean;
  onDecision: (decision: Decision) => void;
}) {
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const remaining = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000));
  const open = remaining > 0;
  const expiry = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;

  return (
    <div className="approval-prompt">
      <div className="approval-window"><span className="label">Approval window</span>
        <time dateTime={expiresAt} title={expiresAt}>{open ? expiry : "Expired"}</time>
      </div>
      <p id="approval-detail">Approve version 2 to record it and generate a receipt in this workspace.</p>
      <div className="actions">
        <button type="button" className="btn btn--primary" aria-describedby="approval-detail"
          disabled={deciding || !open} onClick={() => onDecision("approve")}>Approve</button>
        <button type="button" className="btn btn--danger" disabled={deciding || !open} onClick={() => onDecision("deny")}>Deny</button>
      </div>
      {deciding && <p className="small" role="status">Recording decision…</p>}
    </div>
  );
}

function Artifacts({ task, onUnauthorized }: { task: Task; onUnauthorized: () => void }) {
  const [active, setActive] = useState<number | "diff">("diff");
  const [downloading, setDownloading] = useState<number | null>(null);
  const [downloadError, setDownloadError] = useState("");
  const artifact = task.artifacts.find((item) => item.version === active);
  const hasDiff = Boolean(task.diff);
  const showDiff = active === "diff" && hasDiff;
  const preview = showDiff ? undefined : artifact ?? task.artifacts.at(-1);

  async function download(version: number) {
    if (downloading !== null) return;
    setDownloading(version);
    setDownloadError("");
    try {
      const body = await api.artifact(task.id, version);
      const url = URL.createObjectURL(body);
      const link = document.createElement("a");
      link.href = url;
      link.download = `release-summary-v${version}.txt`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (isUnauthorized(error)) onUnauthorized();
      else setDownloadError(errorMessage(error));
    } finally {
      setDownloading(null);
    }
  }

  return (
    <section className="section artifacts" aria-labelledby="artifact-title">
      <div className="section-heading"><h2 id="artifact-title">Artifact comparison</h2><Icon name="artifact" /></div>
      {task.artifacts.length ? (
        <>
          <ul className="artifact-list">
            {task.artifacts.map((item) => (
              <li key={item.version}>
                <div className="artifact-row-head">
                  <h3>Version {item.version}</h3>
                  <span className="muted small">{item.parent === null ? "Baseline" : `Derived from v${item.parent}`}</span>
                  <button type="button" className="download" disabled={downloading !== null}
                    onClick={() => void download(item.version)} aria-label={`Download version ${item.version}`}>
                    <Icon name="download" /> {downloading === item.version ? "Downloading…" : "Download"}
                  </button>
                </div>
                <div className="artifact-hash"><span className="label">SHA-256</span><code>{item.sha256}</code></div>
              </li>
            ))}
          </ul>
          {downloadError && <p className="error" role="alert">{downloadError}</p>}
          <div className="preview">
            <div className="preview-controls" role="group" aria-label="Artifact view">
              {hasDiff && <button type="button" aria-pressed={showDiff} onClick={() => setActive("diff")}>Text diff</button>}
              {task.artifacts.map((item) => (
                <button key={item.version} type="button" aria-pressed={preview?.version === item.version}
                  onClick={() => setActive(item.version)}>Version {item.version}</button>
              ))}
            </div>
            {showDiff ? <TextDiff diff={task.diff} /> : (
              <pre className="document" tabIndex={0} aria-label={`Version ${preview?.version} contents`}>{preview?.text}</pre>
            )}
          </div>
        </>
      ) : <p className="section-empty">{terminal(task.status) ? "No artifact versions were recorded." : "Artifact versions will appear here as they are recorded."}</p>}
    </section>
  );
}

function TextDiff({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  if (lines.at(-1) === "") lines.pop();

  return (
    <pre className="document diff" tabIndex={0} aria-label="Unified text diff">
      {lines.map((line, index) => (
        <span key={index} data-line={diffLine(line, index)}>{line}{"\n"}</span>
      ))}
    </pre>
  );
}

function diffLine(line: string, index: number) {
  if (index < 2 || line.startsWith("@@ ")) return "header";
  if (line.startsWith("+")) return "added";
  if (line.startsWith("-")) return "removed";
  return "context";
}
