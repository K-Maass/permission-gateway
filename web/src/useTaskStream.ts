import { useEffect, useState } from "react";
import { api, isUnauthorized, terminal, type Task } from "./api";
import { parseSnapshot, snapshotFrames } from "./agui/transport";

export type Connection = "connecting" | "live" | "reconnecting" | "settled";

const RETRY_MS = [500, 1000, 2000, 4000, 8000];

export function useTaskStream(
  id: string | null,
  active: boolean,
  onTask: (task: Task) => void,
  onUnauthorized: () => void,
) {
  const [stream, setStream] = useState<{ id: string; state: Connection } | null>(null);

  useEffect(() => {
    if (!id || !active) return;
    const taskId = id;
    const controller = new AbortController();
    const { signal } = controller;
    let cursor = -1;
    let attempt = 0;

    function update(state: Connection) {
      if (!signal.aborted) setStream({ id: taskId, state });
    }

    async function follow() {
      update("connecting");

      while (!signal.aborted) {
        try {
          const response = await api.stream(taskId, cursor, signal);
          if (!response.body || !response.headers.get("content-type")?.startsWith("text/event-stream")) {
            throw new Error("Expected a PAOS event stream.");
          }
          update("live");
          for await (const payload of snapshotFrames(response.body)) {
            if (signal.aborted) return;
            const snapshot = parseSnapshot(payload, taskId, cursor);
            if (!snapshot) continue;
            // Move the resume point only after accepting a complete snapshot.
            cursor = snapshot.seq;
            attempt = 0;
            onTask(snapshot.task);
            if (terminal(snapshot.task.status)) {
              update("settled");
              return;
            }
          }
        } catch (error) {
          if (signal.aborted) return;
          if (isUnauthorized(error)) {
            onUnauthorized();
            return;
          }
        }

        update("reconnecting");
        const delay = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
        attempt += 1;
        await sleep(delay, signal);
      }
    }

    void follow();
    return () => controller.abort();
  }, [id, active, onTask, onUnauthorized]);

  if (!active) return "settled";
  return stream && stream.id === id ? stream.state : "connecting";
}

function sleep(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = window.setTimeout(finish, ms);
    signal.addEventListener("abort", finish, { once: true });
    function finish() {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
  });
}
