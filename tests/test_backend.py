import hashlib
import hmac
import os
import re
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

ROUTE = {
    "selected": "synthetic",
    "excluded": [{"worker": "read-only", "reason": "Missing capability"}],
}
TOKEN = secrets.token_hex(32)
HOST = "http://127.0.0.1:8765"


@pytest.fixture
def database_url():
    dsn = os.environ.get("PAOS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PAOS_TEST_DATABASE_URL points to the dedicated PostgreSQL test instance")
    schema = f"backend_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield psycopg.conninfo.make_conninfo(dsn, options=f"-csearch_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def store(database_url):
    from paos_core.db import Store

    store = Store(database_url)
    store.migrate()
    return store


def proposal(store, approval_seconds=30):
    task = store.create_task(ROUTE)
    task_id = task["id"]
    store.start_task(task_id)
    store.record_handle(task_id, "task-1", "context-1")
    store.save_artifact(task_id, 1, b"Status: draft\n")
    store.save_artifact(task_id, 2, b"Status: reviewed\n")
    store.propose(task_id, approval_seconds)
    return task_id


def test_concurrent_decisions_apply_once(store):
    task_id = proposal(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        approve = pool.submit(store.decide, task_id, "approve")
        deny = pool.submit(store.decide, task_id, "deny")
    outcomes = [approve.result(), deny.result()]
    assert sorted(outcomes) in (["approved", "conflict"], ["conflict", "denied"])
    with store.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS total FROM sample_register").fetchone()["total"] == 0
        )


def test_approval_expires_even_without_gateway(store):
    task_id = proposal(store, approval_seconds=0)
    assert store.decide(task_id, "approve") == "conflict"
    assert store.get_task(task_id)["status"] == "expired"
    with store.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS total FROM sample_register").fetchone()["total"] == 0
        )


def test_gateway_recovers_committed_write_without_receipt(store):
    from paos_core.gateway import finish_approved

    task_id = proposal(store)
    assert store.decide(task_id, "approve") == "approved"
    source = Path(__file__).resolve().parents[1] / "src"
    crash = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import secrets
from uuid import UUID

from paos_core.db import Store
from paos_core.gateway import finish_approved

store = Store(os.environ["PAOS_DATABASE_URL"])
store.record_receipt = lambda *_: os._exit(73)
finish_approved(store, UUID(os.environ["PAOS_TEST_TASK_ID"]))
""",
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(source),
            "PAOS_DATABASE_URL": store.dsn,
            "PAOS_TEST_TASK_ID": task_id,
        },
        timeout=10,
        check=False,
    )
    assert crash.returncode == 73
    with store.connect() as conn:
        committed = conn.execute(
            "SELECT id FROM sample_register WHERE task_id = %s", (task_id,)
        ).fetchone()
    assert committed is not None
    assert store.get_task(task_id)["receipt"] is None
    finish_approved(store, task_id)
    snapshot = store.get_task(task_id)
    assert snapshot["status"] == "succeeded"
    assert snapshot["receipt"] == {
        "id": str(committed["id"]),
        "artifact_sha256": hashlib.sha256(b"Status: reviewed\n").hexdigest(),
    }
    with store.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS total FROM sample_register").fetchone()["total"] == 1
        )


def test_corrupted_approved_artifact_cannot_be_registered(store):
    from paos_core.db import IntegrityError

    task_id = proposal(store)
    assert store.decide(task_id, "approve") == "approved"
    with store.connect() as conn:
        conn.execute(
            "UPDATE artifacts SET body = %s WHERE task_id = %s AND version = 2",
            (b"Changed after approval\n", task_id),
        )
    with pytest.raises(IntegrityError):
        store.write_register(task_id)
    with store.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS total FROM sample_register").fetchone()["total"] == 0
        )


def test_worker_cannot_store_a_nul_in_a_text_artifact(store):
    from paos_core.db import IntegrityError

    task_id = store.create_task(ROUTE)["id"]
    store.start_task(task_id)
    with pytest.raises(IntegrityError):
        store.save_artifact(task_id, 1, b"bad\x00text")
    store.record_progress(task_id, "working\x00 safely")
    assert store.get_task(task_id)["artifacts"] == []
    assert store.get_task(task_id)["timeline"][-1]["message"] == "working safely"


def test_operator_session_and_origin_boundary(database_url, monkeypatch, tmp_path: Path):
    from paos_core.api import app

    (tmp_path / "index.html").write_text("<!doctype html><title>PAOS Core</title>")
    monkeypatch.setenv("PAOS_DATABASE_URL", database_url)
    monkeypatch.setenv("PAOS_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("PAOS_WEB_DIR", str(tmp_path))
    with TestClient(app, base_url=HOST) as client:
        assert client.get("/healthz").status_code == 200
        assert "<title>PAOS Core</title>" in client.get("/").text
        assert client.get("/api/tasks").status_code == 401
        assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
        assert (
            client.post(
                "/api/session", json={"token": TOKEN}, headers={"Origin": "http://foreign.test"}
            ).status_code
            == 403
        )
        invalid = client.post("/api/session", json={"token": TOKEN, "unknown": TOKEN})
        assert invalid.status_code == 422
        assert TOKEN not in invalid.text
        session = client.post("/api/session", json={"token": TOKEN})
        assert session.status_code == 200
        bearer = session.json()["session"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", bearer)
        assert "set-cookie" not in session.headers
        assert session.headers["cache-control"] == "no-store"
        assert client.get("/api/session").status_code == 401
        assert (
            client.get("/api/session", headers={"Cookie": f"paos_session={bearer}"}).status_code
            == 401
        )
        legacy = "a" * 43
        app.state.store.new_session(hmac.digest(TOKEN.encode(), legacy.encode(), "sha256").hex())
        assert (
            client.get("/api/session", headers={"Authorization": f"Bearer {legacy}"}).status_code
            == 401
        )
        client.headers["Authorization"] = f"Bearer {bearer}"
        assert client.get("/api/session").status_code == 204
        assert (
            client.post(
                "/api/tasks", json={}, headers={"Origin": "http://foreign.test"}
            ).status_code
            == 403
        )
        assert client.post("/api/tasks", content="{}").status_code == 415
        assert client.get("/api/tasks").json() == []
        assert client.get("/api/tasks", headers={"Host": "foreign.test"}).status_code == 400
        assert client.delete("/api/session").status_code == 204
        assert client.get("/api/tasks").status_code == 401
