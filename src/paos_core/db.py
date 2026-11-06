"""PostgreSQL queue, approval ledger, and synthetic register."""

from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATIONS_LOCK = 769013213611300881
GATEWAY_LOCK = 769013213611300882
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
TASK_DOCUMENT = """
    SELECT t.*, (SELECT jsonb_agg(
        jsonb_build_object('seq', e.seq, 'kind', e.kind, 'message', e.message, 'at', e.at)
        ORDER BY e.seq
    ) FROM timeline e WHERE e.task_id = t.id) AS timeline,
    (SELECT jsonb_agg(
        jsonb_build_object(
            'version', a.version, 'parent', a.parent, 'sha256', a.sha256,
            'text', convert_from(a.body, 'UTF8')
        ) ORDER BY a.version
    ) FROM artifacts a WHERE a.task_id = t.id) AS artifacts,
    (SELECT jsonb_build_object('id', r.id, 'artifact_sha256', r.artifact_sha256)
     FROM receipts r WHERE r.task_id = t.id) AS receipt
    FROM tasks t
"""


class IntegrityError(Exception):
    """The proposal, artifact, or committed register failed verification."""


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def checked_body(row: dict, expected_sha256: str | None = None) -> bytes:
    body = bytes(row["body"])
    digest = sha256(body)
    if digest != row["sha256"] or (expected_sha256 is not None and digest != expected_sha256):
        raise IntegrityError("Artifact digest mismatch")
    return body


def snapshot(row: dict) -> dict:
    artifacts = row["artifacts"] or []
    diff = ""
    if len(artifacts) == 2:
        draft, revision = artifacts
        diff = "".join(
            difflib.unified_diff(
                draft["text"].splitlines(keepends=True),
                revision["text"].splitlines(keepends=True),
                fromfile="release-summary-v1.txt",
                tofile="release-summary-v2.txt",
            )
        )
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "route": row["route"],
        "timeline": row["timeline"] or [],
        "artifacts": artifacts,
        "diff": diff,
        "approval": (
            {"expires_at": row["approval_expires_at"].isoformat()}
            if row["status"] == "awaiting_approval"
            else None
        ),
        "receipt": row["receipt"],
    }


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self, *, autocommit: bool = False):
        return psycopg.connect(
            self.dsn, row_factory=dict_row, connect_timeout=3, autocommit=autocommit
        )

    def migrate(self) -> None:
        directory = Path(os.environ.get("PAOS_MIGRATIONS_DIR", MIGRATIONS_DIR))
        migrations = sorted(directory.glob("*.sql"))
        if not migrations:
            raise RuntimeError("PAOS Core migrations directory is empty")
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATIONS_LOCK,))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paos_schema_migrations (
                    name text PRIMARY KEY,
                    sha256 text NOT NULL
                )
            """)
            for migration in migrations:
                source = migration.read_bytes()
                digest = sha256(source)
                applied = conn.execute(
                    "SELECT sha256 FROM paos_schema_migrations WHERE name = %s", (migration.name,)
                ).fetchone()
                if applied is not None:
                    if applied["sha256"] != digest:
                        raise IntegrityError(f"Migration checksum changed for {migration.name}")
                    continue
                conn.execute(source.decode("utf-8"))
                conn.execute(
                    "INSERT INTO paos_schema_migrations (name, sha256) VALUES (%s, %s)",
                    (migration.name, digest),
                )

    def is_ready(self) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT EXISTS (SELECT FROM paos_schema_migrations) AS ready"
            ).fetchone()["ready"]

    @staticmethod
    def _event(conn, task_id, kind: str, message: str) -> None:
        seq = conn.execute(
            "UPDATE tasks SET event_seq = event_seq + 1 WHERE id = %s RETURNING event_seq",
            (task_id,),
        ).fetchone()["event_seq"]
        conn.execute(
            "INSERT INTO timeline (task_id, seq, kind, message) VALUES (%s, %s, %s, %s)",
            (task_id, seq, kind, message),
        )

    def create_task(
        self, route: dict, *, selected_reason: str = "Matches release review policy"
    ) -> dict:
        task_id = uuid4()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, status, route) VALUES (%s, 'queued', %s)",
                (task_id, Jsonb(route)),
            )
            self._event(conn, task_id, "queued", "Synthetic release review queued.")
            self._event(
                conn, task_id, "routed", f"Selected {route['selected']}. {selected_reason}."
            )
            return snapshot(conn.execute(f"{TASK_DOCUMENT} WHERE t.id = %s", (task_id,)).fetchone())

    def get_task(self, task_id) -> dict | None:
        with self.connect() as conn:
            self._expire(conn, task_id)
            row = conn.execute(f"{TASK_DOCUMENT} WHERE t.id = %s", (task_id,)).fetchone()
            return snapshot(row) if row else None

    def list_tasks(self) -> list[dict]:
        self.expire_pending()
        with self.connect() as conn:
            rows = conn.execute(f"{TASK_DOCUMENT} ORDER BY t.created_at DESC, t.id").fetchall()
            return [snapshot(row) for row in rows]

    def next_task(self) -> dict | None:
        with self.connect() as conn:
            return conn.execute("""
                SELECT id, status, started_at, external_task_id, context_id FROM tasks
                WHERE status IN ('approved', 'running', 'queued')
                ORDER BY CASE status WHEN 'approved' THEN 0 WHEN 'running' THEN 1 ELSE 2 END,
                         created_at, id
                LIMIT 1
            """).fetchone()

    def start_task(self, task_id) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE tasks SET status = 'running', started_at = clock_timestamp()
                WHERE id = %s AND status = 'queued'
            """,
                (task_id,),
            )
            if not result.rowcount:
                return False
            self._event(conn, task_id, "started", "Opening A2A work with the selected worker.")
            return True

    def record_handle(self, task_id, external_task_id: str, context_id: str) -> None:
        if not external_task_id or not context_id:
            raise IntegrityError("Incomplete A2A handle")
        with self.connect() as conn:
            task = conn.execute(
                """
                SELECT status, external_task_id, context_id FROM tasks WHERE id = %s FOR UPDATE
            """,
                (task_id,),
            ).fetchone()
            if task["status"] != "running":
                return
            current_handle = task["external_task_id"], task["context_id"]
            received_handle = external_task_id, context_id
            if current_handle == received_handle:
                return
            if current_handle != (None, None):
                raise IntegrityError("A2A handle changed")
            conn.execute(
                "UPDATE tasks SET external_task_id = %s, context_id = %s WHERE id = %s",
                (*received_handle, task_id),
            )
            self._event(conn, task_id, "worker.attached", "A2A task and context IDs recorded.")

    def record_reattachment(self, task_id) -> None:
        with self.connect() as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task["status"] != "running":
                return
            seen = conn.execute(
                "SELECT 1 FROM timeline WHERE task_id = %s AND kind = 'worker.reattached'",
                (task_id,),
            ).fetchone()
            if not seen:
                self._event(
                    conn, task_id, "worker.reattached", "Reattached using the recorded A2A handle."
                )

    def record_progress(self, task_id, message: str) -> None:
        message = message.replace("\x00", "")
        with self.connect() as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task["status"] != "running":
                return
            seen = conn.execute(
                "SELECT 1 FROM timeline WHERE task_id = %s AND kind = 'worker_progress' AND message = %s",
                (task_id, message),
            ).fetchone()
            if not seen:
                self._event(conn, task_id, "worker_progress", message)

    def save_artifact(self, task_id, version: int, body: bytes) -> None:
        if version not in (1, 2) or not 1 <= len(body) <= 32768 or b"\x00" in body:
            raise IntegrityError("Invalid artifact")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrityError("Artifact is not UTF-8") from exc
        digest = sha256(body)
        with self.connect() as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task["status"] != "running":
                return
            existing = conn.execute(
                "SELECT sha256, body FROM artifacts WHERE task_id = %s AND version = %s",
                (task_id, version),
            ).fetchone()
            if existing is not None:
                checked_body(existing, digest)
                return
            conn.execute(
                "INSERT INTO artifacts (task_id, version, parent, sha256, body) VALUES (%s, %s, %s, %s, %s)",
                (task_id, version, version - 1 if version == 2 else None, digest, body),
            )
            self._event(conn, task_id, "artifact", f"Recorded release summary version {version}.")

    def get_artifact(self, task_id, version: int) -> bytes | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sha256, body FROM artifacts WHERE task_id = %s AND version = %s",
                (task_id, version),
            ).fetchone()
            return checked_body(row) if row else None

    def propose(self, task_id, approval_seconds: int) -> None:
        with self.connect() as conn:
            task = conn.execute(
                """
                SELECT status, external_task_id FROM tasks WHERE id = %s FOR UPDATE
            """,
                (task_id,),
            ).fetchone()
            if task["status"] != "running":
                return
            artifacts = conn.execute(
                "SELECT sha256, body FROM artifacts WHERE task_id = %s ORDER BY version", (task_id,)
            ).fetchall()
            if task["external_task_id"] is None or len(artifacts) != 2:
                raise IntegrityError(
                    "The worker completed without the expected artifacts or handle"
                )
            for artifact in artifacts:
                checked_body(artifact)
            conn.execute(
                """
                UPDATE tasks SET status = 'awaiting_approval',
                    approval_expires_at = clock_timestamp() + make_interval(secs => %s),
                    approval_sha256 = %s
                WHERE id = %s
            """,
                (approval_seconds, artifacts[1]["sha256"], task_id),
            )
            self._event(
                conn, task_id, "approval_requested", "Operator approval requested for version 2."
            )

    def _expire(self, conn, task_id) -> bool:
        updated = conn.execute(
            """
            UPDATE tasks SET status = 'expired'
            WHERE id = %s AND status = 'awaiting_approval' AND approval_expires_at <= clock_timestamp()
        """,
            (task_id,),
        )
        if updated.rowcount:
            self._event(conn, task_id, "expired", "The approval window expired.")
        return bool(updated.rowcount)

    def expire_pending(self) -> None:
        with self.connect() as conn:
            tasks = conn.execute("""
                SELECT id FROM tasks WHERE status = 'awaiting_approval' AND approval_expires_at <= clock_timestamp()
                ORDER BY id FOR UPDATE SKIP LOCKED
            """).fetchall()
            for task in tasks:
                self._expire(conn, task["id"])

    def decide(self, task_id, decision: str) -> str:
        if decision not in ("approve", "deny"):
            raise ValueError("Invalid approval decision")
        with self.connect() as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                return "missing"
            if task["status"] != "awaiting_approval":
                return "conflict"
            state = "approved" if decision == "approve" else "denied"
            updated = conn.execute(
                """
                UPDATE tasks SET status = %s, decision = %s, decided_at = clock_timestamp()
                WHERE id = %s AND approval_expires_at > clock_timestamp()
            """,
                (state, decision, task_id),
            )
            if not updated.rowcount:
                self._expire(conn, task_id)
                return "conflict"
            self._event(conn, task_id, state, f"Operator {state} release summary version 2.")
            return state

    def write_register(self, task_id) -> dict:
        """Probe or apply the one local effect, committing it before a receipt is recorded."""
        with self.connect() as conn:
            task = conn.execute(
                """
                SELECT status, approval_sha256 FROM tasks WHERE id = %s FOR UPDATE
            """,
                (task_id,),
            ).fetchone()
            if not task or task["status"] not in ("approved", "succeeded"):
                raise IntegrityError("The sample write requires an approval")
            artifact = conn.execute(
                "SELECT sha256, body FROM artifacts WHERE task_id = %s AND version = 2 FOR SHARE",
                (task_id,),
            ).fetchone()
            if artifact is None or task["approval_sha256"] is None:
                raise IntegrityError("The approved artifact is missing")
            body = checked_body(artifact, task["approval_sha256"])
            existing = conn.execute(
                "SELECT id, artifact_sha256, body FROM sample_register WHERE task_id = %s FOR SHARE",
                (task_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["artifact_sha256"] != artifact["sha256"]
                    or bytes(existing["body"]) != body
                ):
                    raise IntegrityError("The register entry differs from the approved artifact")
                return existing
            return conn.execute(
                """
                INSERT INTO sample_register (id, task_id, artifact_sha256, body)
                VALUES (%s, %s, %s, %s)
                RETURNING id, artifact_sha256
            """,
                (uuid4(), task_id, artifact["sha256"], body),
            ).fetchone()

    def record_receipt(self, task_id, receipt: dict) -> None:
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE tasks SET status = 'succeeded' WHERE id = %s AND status = 'approved'
            """,
                (task_id,),
            )
            if not updated.rowcount:
                return
            conn.execute(
                "INSERT INTO receipts (task_id, id, artifact_sha256) VALUES (%s, %s, %s)",
                (task_id, receipt["id"], receipt["artifact_sha256"]),
            )
            self._event(
                conn,
                task_id,
                "succeeded",
                "The local sample register recorded the approved artifact.",
            )

    def fail(self, task_id, message: str) -> None:
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE tasks SET status = 'failed' WHERE id = %s AND status IN ('running', 'approved')
            """,
                (task_id,),
            )
            if updated.rowcount:
                self._event(conn, task_id, "failed", message)

    def new_session(self, digest: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM operator_sessions WHERE expires_at <= clock_timestamp()")
            conn.execute(
                """
                INSERT INTO operator_sessions (digest, expires_at)
                VALUES (%s, clock_timestamp() + interval '12 hours')
            """,
                (digest,),
            )

    def session_valid(self, digest: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    """
                SELECT 1 FROM operator_sessions WHERE digest = %s AND expires_at > clock_timestamp()
            """,
                    (digest,),
                ).fetchone()
                is not None
            )

    def delete_session(self, digest: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM operator_sessions WHERE digest = %s", (digest,))
