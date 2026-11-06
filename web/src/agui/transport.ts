// Adapted from the PAOS control web transport. One SSE frame carries an atomic
// envelope. A snapshot replaces the whole task state through that sequence.
import { type Task } from "../api";

interface Envelope {
  seq: number;
  events: { type: string; snapshot?: unknown }[];
}

export function parseSnapshot(payload: string, taskId: string, after: number) {
  const envelope: Partial<Envelope> | null = JSON.parse(payload);
  const seq = envelope?.seq;
  if (typeof seq !== "number" || !Number.isSafeInteger(seq) || seq < 0 ||
      !Array.isArray(envelope?.events)) {
    throw new Error("Invalid PAOS event envelope.");
  }
  if (seq <= after) return null;

  const snapshot = envelope.events.find((event) => event?.type === "STATE_SNAPSHOT")?.snapshot;
  if (!snapshot || typeof snapshot !== "object" || !("id" in snapshot) || snapshot.id !== taskId ||
      !("timeline" in snapshot) || !Array.isArray(snapshot.timeline)) {
    throw new Error("Invalid PAOS task snapshot.");
  }

  return { seq, task: snapshot as Task };
}

/**
 * Decode complete PAOS frames from a fetch stream. A torn frame is discarded
 * so its sequence is never used as the reconnect cursor.
 */
export async function* snapshotFrames(body: ReadableStream<Uint8Array>) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });

      for (let separator = /\r?\n\r?\n/.exec(buffer); separator; separator = /\r?\n\r?\n/.exec(buffer)) {
        const block = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        const payload = framePayload(block);
        if (payload !== null) yield payload;
      }

      // A frame is small for the synthetic task. Bound any incomplete frame.
      if (buffer.length > 1024 * 1024) throw new Error("PAOS stream frame exceeded the buffer limit.");
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function framePayload(block: string) {
  let type = "";
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) type = line.slice(6).replace(/^ /, "");
    if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  return type === "paos.agui" && data.length ? data.join("\n") : null;
}
