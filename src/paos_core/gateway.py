"""Restartable synthetic release worker gateway."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import aclosing
from datetime import UTC, datetime
from uuid import UUID

import httpx
import psycopg

from paos_core.db import GATEWAY_LOCK, IntegrityError, Store
from paos_core.memory import memory_view
from paos_core.settings import approval_seconds, database_url
from paos_core.worker_client import run_release

LOG = logging.getLogger(__name__)
OBJECTIVE = """Draft a release summary from the changelog. Use the supplied release facts for the recommendation.

## Synthetic changelog
- Keep keyboard focus inside the confirmation dialog.
- Preserve CSV headers when an export has no rows.
- Add a reviewer quickstart to the release guide."""
WORK_DEADLINE_SECONDS = 120


def finish_approved(store: Store, task_id: UUID) -> None:
    receipt = store.write_register(task_id)
    store.record_receipt(task_id, receipt)


async def advance(store: Store, task: dict, worker_url: str, expiry_seconds: int) -> None:
    task_id = task["id"]
    status = task["status"]
    if status == "approved":
        try:
            finish_approved(store, task_id)
        except IntegrityError:
            store.fail(task_id, "The approved artifact or committed register failed verification.")
        return

    external_id, context_id = task["external_task_id"], task["context_id"]
    if status == "running":
        if external_id is None:
            store.fail(
                task_id, "Gateway restarted before the A2A handle was recorded. Dispatch stopped."
            )
            return
        age = (datetime.now(UTC) - task["started_at"]).total_seconds()
        if age >= WORK_DEADLINE_SECONDS:
            store.fail(task_id, "The A2A task did not finish within the worker deadline.")
            return
    else:
        if not store.start_task(task_id):
            return
        age = 0

    try:
        updates = run_release(
            worker_url,
            run_id=task_id,
            objective=OBJECTIVE,
            facts=memory_view()["facts"],
            external_task_id=external_id or "",
            context_id=context_id or "",
        )
        reattaching = bool(external_id)
        async with asyncio.timeout(WORK_DEADLINE_SECONDS - age), aclosing(updates):
            async for update in updates:
                if reattaching:
                    store.record_reattachment(task_id)
                    reattaching = False
                if update.kind == "handle":
                    store.record_handle(task_id, update.external_task_id, update.context_id)
                    external_id = update.external_task_id
                elif update.kind == "progress":
                    store.record_progress(task_id, update.message[:256])
                elif update.kind == "artifact":
                    if update.version is None or update.body is None:
                        raise IntegrityError("Incomplete worker artifact")
                    store.save_artifact(task_id, update.version, update.body)
                elif update.kind == "completed":
                    store.propose(task_id, expiry_seconds)
                    return
                elif update.kind == "failed":
                    store.fail(task_id, "The A2A worker did not complete the release summary.")
                    return
        raise IntegrityError("The A2A stream ended without a terminal state")
    except IntegrityError:
        store.fail(task_id, "The A2A worker output failed verification.")
    except TimeoutError:
        store.fail(task_id, "The A2A task did not finish within the worker deadline.")
    except (httpx.TransportError, ConnectionError):
        if external_id is None:
            store.fail(task_id, "A2A dispatch did not return a handle. Dispatch stopped.")
        else:
            await asyncio.sleep(0.5)
    except (ValueError, RuntimeError) as exc:
        LOG.error("event=worker_error task_id=%s error_type=%s", task_id, type(exc).__name__)
        store.fail(task_id, "The A2A exchange failed.")


async def serve() -> None:
    store = Store(database_url())
    store.migrate()
    expiry_seconds = approval_seconds()
    worker_url = os.environ.get("PAOS_WORKER_URL", "http://worker:9000")
    while True:
        try:
            with store.connect(autocommit=True) as lock:
                acquired = lock.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired", (GATEWAY_LOCK,)
                ).fetchone()["acquired"]
                if acquired:
                    LOG.info("event=gateway_ready")
                    while True:
                        lock.execute("SELECT 1")
                        store.expire_pending()
                        task = store.next_task()
                        if task is not None:
                            await advance(store, task, worker_url, expiry_seconds)
                        else:
                            await asyncio.sleep(0.25)
        except psycopg.OperationalError as exc:
            LOG.warning("event=database_unavailable error_type=%s", type(exc).__name__)
        await asyncio.sleep(1)


def healthy() -> bool:
    """The singleton gateway is ready once it holds the database queue lock."""
    try:
        with Store(database_url()).connect() as conn:
            row = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1
                      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                      AND ((classid::bigint << 32) | objid::bigint) = %s
                ) AS ready
                """,
                (GATEWAY_LOCK,),
            ).fetchone()
            return bool(row and row["ready"])
    except (RuntimeError, psycopg.Error):
        return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("a2a").setLevel(logging.WARNING)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
