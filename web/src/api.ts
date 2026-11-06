export const statusLabels = {
  queued: "Queued",
  running: "Running",
  awaiting_approval: "Awaiting approval",
  approved: "Approved",
  succeeded: "Succeeded",
  denied: "Denied",
  expired: "Expired",
  failed: "Failed",
} as const;

export type TaskStatus = keyof typeof statusLabels;
export type Decision = "approve" | "deny";

export interface Artifact {
  version: number;
  sha256: string;
  parent: number | null;
  text: string;
}

export interface Task {
  id: string;
  status: TaskStatus;
  route: { selected: string; excluded: { worker: string; reason: string }[] } | null;
  timeline: { seq: number; kind: string; message: string; at: string }[];
  artifacts: Artifact[];
  diff: string;
  approval: { expires_at: string } | null;
  receipt: { id: string; artifact_sha256: string } | null;
}

export interface Memory {
  task_classification: string;
  facts: { id: string; statement: string; evidence: string }[];
  withheld: { id: string; reason: string }[];
  policy: string;
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export function isUnauthorized(error: unknown) {
  return error instanceof ApiError && error.status === 401;
}

export function errorMessage(error: unknown) {
  return error instanceof ApiError ? error.message : "The local service could not be reached. Try again.";
}

const SESSION_KEY = "paos.core.session";

export function sessionToken() {
  return sessionStorage.getItem(SESSION_KEY);
}

export function clearSession() {
  sessionStorage.removeItem(SESSION_KEY);
}

async function request(path: string, init?: RequestInit, authenticated = true): Promise<Response> {
  const session = authenticated ? sessionToken() : null;
  const headers = new Headers(init?.headers);
  if (session) headers.set("Authorization", `Bearer ${session}`);
  const response = await fetch(path, {
    ...init,
    headers,
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  if (!response.ok) {
    if (response.status === 401 && session && sessionToken() === session) clearSession();
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? body?.error ?? body?.message;
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : `Request failed (${response.status}).`,
    );
  }
  return response;
}

async function json<T>(path: string, init?: RequestInit, authenticated = true): Promise<T> {
  return (await request(path, init, authenticated)).json();
}

function post<T>(path: string, body: unknown, authenticated = true) {
  return json<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, authenticated);
}

function taskPath(id: string) {
  return `/api/tasks/${encodeURIComponent(id)}`;
}

export const api = {
  session: (signal?: AbortSignal) => request("/api/session", { signal }),
  signIn: async (token: string) => {
    const result = await post<{ session?: unknown }>("/api/session", { token }, false);
    if (typeof result?.session !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(result.session)) {
      throw new ApiError(502, "The local service returned an invalid session.");
    }
    sessionStorage.setItem(SESSION_KEY, result.session);
  },
  signOut: () => request("/api/session", { method: "DELETE" }),
  tasks: (signal?: AbortSignal) => json<Task[]>("/api/tasks", { signal }),
  task: (id: string, signal?: AbortSignal) => json<Task>(taskPath(id), { signal }),
  start: () => post<Task>("/api/tasks", {}),
  decide: (id: string, decision: Decision) => post<Task>(`${taskPath(id)}/decision`, { decision }),
  memory: (signal?: AbortSignal) => json<Memory>("/api/memory", { signal }),
  stream: (id: string, after: number, signal: AbortSignal) => request(`${taskPath(id)}/stream`, {
    signal,
    headers: {
      Accept: "text/event-stream",
      ...(after >= 0 ? { "Last-Event-ID": String(after) } : {}),
    },
  }),
  artifact: async (id: string, version: number) =>
    (await request(`${taskPath(id)}/artifacts/${version}`)).blob(),
};

export function terminal(status: TaskStatus) {
  return status === "succeeded" || status === "denied" || status === "expired" || status === "failed";
}

export function taskSequence(task: Task) {
  return task.timeline.at(-1)?.seq ?? 0;
}
