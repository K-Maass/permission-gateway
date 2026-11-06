import { useCallback, useEffect, useState } from "react";
import { api, errorMessage, isUnauthorized, taskSequence, terminal, type Decision, type Task } from "./api";
import { Brand, Icon, Status } from "./design/Primitives";
import { GovernedMemory } from "./GovernedMemory";
import { ReviewRun } from "./ReviewRun";
import { useTaskStream } from "./useTaskStream";

function mergeTasks(current: Task[], incoming: Task[]) {
  const merged = new Map(current.map((task) => [task.id, task]));
  for (const task of incoming) {
    const previous = merged.get(task.id);
    // A late list or mutation response can predate an already visible event.
    if (!previous || taskSequence(task) > taskSequence(previous)) merged.set(task.id, task);
  }
  return [...merged.values()].sort((a, b) =>
    Date.parse(b.timeline[0]?.at ?? "") - Date.parse(a.timeline[0]?.at ?? ""));
}

type View = "review" | "memory";

export function Console({ onSignOut, onUnauthorized, signingOut }: {
  onSignOut: () => void;
  onUnauthorized: () => void;
  signingOut: boolean;
}) {
  const [view, setView] = useState<View>("review");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [listAttempt, setListAttempt] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const [decisionPending, setDecisionPending] = useState<string | null>(null);
  const [decisionError, setDecisionError] = useState<{ id: string; message: string } | null>(null);
  const current = tasks.find((task) => task.id === selectedId) ?? tasks[0];

  const updateTask = useCallback((task: Task) => setTasks((known) => mergeTasks(known, [task])), []);
  const connection = useTaskStream(current?.id ?? null, Boolean(current && !terminal(current.status)),
    updateTask, onUnauthorized);

  useEffect(() => {
    const controller = new AbortController();
    void api.tasks(controller.signal).then((loaded) => {
      if (!controller.signal.aborted) setTasks((known) => mergeTasks(known, loaded));
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      if (isUnauthorized(error)) onUnauthorized();
      else setListError(errorMessage(error));
    }).finally(() => {
      if (!controller.signal.aborted) setListLoading(false);
    });
    return () => controller.abort();
  }, [listAttempt, onUnauthorized]);

  async function startReview() {
    if (creating) return;
    setCreating(true);
    setCreateError("");
    try {
      const task = await api.start();
      updateTask(task);
      setSelectedId(task.id);
      setView("review");
    } catch (error) {
      if (isUnauthorized(error)) onUnauthorized();
      else setCreateError(errorMessage(error));
    } finally {
      setCreating(false);
    }
  }

  async function decide(id: string, decision: Decision) {
    if (decisionPending) return;
    setDecisionError(null);
    setDecisionPending(id);
    try {
      updateTask(await api.decide(id, decision));
    } catch (error) {
      if (isUnauthorized(error)) {
        onUnauthorized();
      } else {
        setDecisionError({ id, message: errorMessage(error) });
        try {
          updateTask(await api.task(id));
        } catch (refreshError) {
          if (isUnauthorized(refreshError)) onUnauthorized();
        }
      }
    } finally {
      setDecisionPending(null);
    }
  }

  return (
    <div className="shell">
      <a href="#main" className="skip-link">Skip to content</a>
      <aside className="rail">
        <Brand />
        <nav aria-label="Workspace" className="nav">
          <span className="label nav-label">Workspace</span>
          <button type="button" aria-current={view === "review" ? "page" : undefined} onClick={() => setView("review")}>
            <Icon name="review" /> Release review
          </button>
          <button type="button" aria-current={view === "memory" ? "page" : undefined} onClick={() => setView("memory")}>
            <Icon name="memory" /> Governed memory
          </button>
        </nav>
        <section className="history" aria-labelledby="recent-runs">
          <h2 id="recent-runs" className="label">Review runs</h2>
          {listLoading && <p className="muted small">Loading runs…</p>}
          {listError && (
            <>
              <p className="error small" role="alert">{listError}</p>
              <button type="button" className="btn" onClick={() => {
                setListError("");
                setListLoading(true);
                setListAttempt((count) => count + 1);
              }}>Retry history</button>
            </>
          )}
          {!listLoading && !listError && !tasks.length && <p className="muted small">No runs yet.</p>}
          {tasks.length > 0 && (
            <ul>
              {tasks.map((task) => (
                <li key={task.id}>
                  <button type="button" className="history-run" aria-pressed={view === "review" && current?.id === task.id}
                    onClick={() => { setSelectedId(task.id); setView("review"); }}>
                    <span className="history-title">{task.id}</span>
                    <Status value={task.status} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
        <div className="rail-foot">
          <span className="local-mark"><Icon name="lock" /> Local session</span>
          <button type="button" className="btn btn--quiet" disabled={signingOut} onClick={onSignOut}>Sign out</button>
        </div>
      </aside>
      <main id="main" className="workspace" tabIndex={-1}>
        {view === "review" ? (
          <>
            <header className="page-header">
              <div>
                <h1>Release register review</h1>
                <p>Inspect a synthetic release register and decide whether to record the proposed revision locally.</p>
              </div>
              <button type="button" className="btn btn--primary" onClick={() => void startReview()} disabled={creating}>
                {creating ? "Starting review…" : "Start release review"} <Icon name="arrow" />
              </button>
            </header>
            {createError && <p className="error" role="alert">{createError}</p>}
            {current ? (
              <ReviewRun key={current.id} task={current} connection={connection}
                deciding={decisionPending !== null} decisionError={decisionError?.id === current.id ? decisionError.message : ""}
                onDecision={(decision) => void decide(current.id, decision)} onUnauthorized={onUnauthorized} />
            ) : listLoading ? (
              <p className="workspace-wait" role="status">Loading the workspace…</p>
            ) : (
              <ReviewBrief />
            )}
          </>
        ) : <GovernedMemory onUnauthorized={onUnauthorized} />}
      </main>
    </div>
  );
}

function ReviewBrief() {
  return (
    <section className="brief" aria-labelledby="brief-title">
      <div className="brief-title">
        <Icon name="artifact" />
        <div>
          <h2 id="brief-title">Review brief</h2>
          <p className="muted">Synthetic data · Local execution</p>
        </div>
      </div>
      <div className="brief-steps">
        <div><span className="step-number">01</span><h3>Route</h3><p>See which worker the policy selects and why it excludes another.</p></div>
        <div><span className="step-number">02</span><h3>Compare</h3><p>Inspect both versions, their SHA-256 hashes, and the text diff.</p></div>
        <div><span className="step-number">03</span><h3>Decide</h3><p>Approve or deny the revision. Approval produces a local receipt.</p></div>
      </div>
    </section>
  );
}
