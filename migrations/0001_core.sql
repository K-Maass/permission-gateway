CREATE TABLE tasks (
    id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN (
        'queued', 'running', 'awaiting_approval', 'approved', 'succeeded', 'denied', 'expired', 'failed'
    )),
    route jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    external_task_id text,
    context_id text,
    event_seq integer NOT NULL DEFAULT 0,
    approval_expires_at timestamptz,
    approval_sha256 text,
    decision text CHECK (decision IN ('approve', 'deny')),
    decided_at timestamptz,
    CHECK ((external_task_id IS NULL) = (context_id IS NULL)),
    CHECK ((approval_expires_at IS NULL) = (approval_sha256 IS NULL)),
    CHECK ((decision IS NULL) = (decided_at IS NULL)),
    CHECK (status NOT IN ('approved', 'succeeded') OR decision IS NOT DISTINCT FROM 'approve')
);

CREATE INDEX tasks_active ON tasks (status, created_at)
    WHERE status IN ('queued', 'running', 'awaiting_approval', 'approved');

CREATE TABLE timeline (
    task_id uuid NOT NULL REFERENCES tasks (id),
    seq integer NOT NULL,
    kind text NOT NULL,
    message text NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (task_id, seq)
);

CREATE TABLE artifacts (
    task_id uuid NOT NULL REFERENCES tasks (id),
    version integer NOT NULL CHECK (version IN (1, 2)),
    parent integer,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    body bytea NOT NULL CHECK (octet_length(body) BETWEEN 1 AND 32768),
    PRIMARY KEY (task_id, version),
    FOREIGN KEY (task_id, parent) REFERENCES artifacts (task_id, version),
    CHECK ((version = 1 AND parent IS NULL) OR
           (version = 2 AND parent IS NOT DISTINCT FROM 1))
);

CREATE TABLE sample_register (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL UNIQUE REFERENCES tasks (id),
    artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    body bytea NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (task_id, id, artifact_sha256)
);

CREATE TABLE receipts (
    task_id uuid PRIMARY KEY REFERENCES tasks (id),
    id uuid NOT NULL UNIQUE,
    artifact_sha256 text NOT NULL,
    FOREIGN KEY (task_id, id, artifact_sha256)
        REFERENCES sample_register (task_id, id, artifact_sha256)
);

CREATE TABLE operator_sessions (
    digest text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);
