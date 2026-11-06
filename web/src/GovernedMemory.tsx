import { useEffect, useState } from "react";
import { api, errorMessage, isUnauthorized, type Memory } from "./api";
import { Icon } from "./design/Primitives";

export function GovernedMemory({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [memory, setMemory] = useState<Memory | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void api.memory(controller.signal).then((loaded) => {
      if (!controller.signal.aborted) setMemory(loaded);
    }).catch((caught: unknown) => {
      if (controller.signal.aborted) return;
      if (isUnauthorized(caught)) onUnauthorized();
      else setError(errorMessage(caught));
    });
    return () => controller.abort();
  }, [attempt, onUnauthorized]);

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Governed memory</h1>
          <p>Compare the evidence released to the synthetic task with facts withheld by policy.</p>
        </div>
      </header>
      {error && (
        <div className="workspace-wait">
          <p className="error" role="alert">{error}</p>
          <button type="button" className="btn" onClick={() => {
            setError("");
            setAttempt((count) => count + 1);
          }}>Retry memory</button>
        </div>
      )}
      {!error && !memory && <p className="workspace-wait" role="status">Loading governed memory…</p>}
      {memory && (
        <>
          <section className="memory-policy" aria-labelledby="memory-policy">
            <div>
              <h2 id="memory-policy">Retrieval policy</h2>
              <p>{memory.policy}</p>
            </div>
            <dl>
              <dt className="label">Task classification</dt>
              <dd><code>{memory.task_classification}</code></dd>
            </dl>
          </section>
          <div className="memory-grid">
            <section className="section" aria-labelledby="released-title">
              <div className="section-heading">
                <h2 id="released-title">Released facts</h2><span className="item-count">{memory.facts.length}</span>
              </div>
              {memory.facts.length ? (
                <ul className="memory-list">
                  {memory.facts.map((fact) => (
                    <li key={fact.id}>
                      <code>{fact.id}</code>
                      <p className="fact-statement">{fact.statement}</p>
                      <dl><dt className="label">Evidence</dt><dd>{fact.evidence}</dd></dl>
                    </li>
                  ))}
                </ul>
              ) : <p className="section-empty">The policy released no facts to this task.</p>}
            </section>
            <section className="section" aria-labelledby="withheld-title">
              <div className="section-heading">
                <h2 id="withheld-title">Withheld facts</h2><span className="item-count">{memory.withheld.length}</span>
              </div>
              {memory.withheld.length ? (
                <ul className="memory-list">
                  {memory.withheld.map((fact) => (
                    <li key={fact.id}>
                      <code>{fact.id}</code>
                      <p>{fact.reason}</p>
                    </li>
                  ))}
                </ul>
              ) : <p className="section-empty">The policy withheld no facts for this task.</p>}
            </section>
          </div>
        </>
      )}
    </>
  );
}
