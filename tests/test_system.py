import os
import secrets
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = [
    pytest.mark.system,
    pytest.mark.skipif(
        not os.getenv("PAOS_TEST_URL"), reason="run ./scripts/verify for system tests"
    ),
]
ROOT = Path(__file__).resolve().parents[1]


def compose(*args, env=None):
    return subprocess.check_output(
        ["docker", "compose", *args], cwd=ROOT, env=env, text=True, timeout=150
    ).strip()


def sql(query):
    return compose("exec", "-T", "db", "psql", "-U", "postgres", "-d", "paos", "-Atc", query)


def eventually(check, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(0.15)
    pytest.fail("condition did not become true before the deadline")


@pytest.fixture
def client():
    with httpx.Client(base_url=os.environ["PAOS_TEST_URL"], timeout=6) as client:
        login = client.post("/api/session", json={"token": os.environ["PAOS_TEST_TOKEN"]})
        login.raise_for_status()
        assert "set-cookie" not in login.headers
        client.headers["Authorization"] = f"Bearer {login.json()['session']}"
        yield client


def task(client, task_id):
    return client.get(f"/api/tasks/{task_id}").raise_for_status().json()


def test_anonymous_and_cross_origin_requests_are_rejected(client):
    assert httpx.get(f"{os.environ['PAOS_TEST_URL']}/api/tasks", timeout=6).status_code == 401
    response = client.post("/api/tasks", json={}, headers={"Origin": "https://example.invalid"})
    assert response.status_code == 403


def dispatches(worker):
    return int(
        compose(
            "exec",
            "-T",
            worker,
            "python",
            "-c",
            "import json,urllib.request as u;"
            "print(json.load(u.urlopen('http://127.0.0.1:9000/fixture-stats'))['dispatches'])",
        )
    )


def test_browser_run_recovers_gateway_and_stream(client):
    registered_before = sql("SELECT count(*) FROM sample_register")
    selected_before = dispatches("worker")
    excluded_before = dispatches("worker-public")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 980})
        page = context.new_page()
        stream_attempts = []
        page.on(
            "request",
            lambda request: (
                stream_attempts.append(request) if request.url.endswith("/stream") else None
            ),
        )
        page.goto(os.environ["PAOS_TEST_URL"])
        page.get_by_label("Operator token").fill(os.environ["PAOS_TEST_TOKEN"])
        page.get_by_role("button", name="Sign in", exact=True).click()

        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and urllib.parse.urlsplit(response.url).path == "/api/tasks"
            )
        ) as submit:
            page.get_by_role("button", name="Start release review", exact=True).click()
        task_id = submit.value.json()["id"]
        page.evaluate("window.__paosDocument = 'same-document'")
        eventually(lambda: dispatches("worker") == selected_before + 1)
        eventually(
            lambda: any(
                event["kind"] == "worker.attached" for event in task(client, task_id)["timeline"]
            )
        )
        expect(page.get_by_text("Live updates", exact=True)).to_be_visible()
        context.set_offline(True)
        compose("kill", "-s", "SIGKILL", "api", "gateway")
        compose("start", "api", "gateway")
        context.set_offline(False)

        expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible(timeout=45_000)
        assert page.evaluate("window.__paosDocument") == "same-document"
        assert len(stream_attempts) >= 2
        activity = page.get_by_role("region", name="Activity").bounding_box()
        routing = page.get_by_role("region", name="Worker routing").bounding_box()
        assert activity and routing and activity["x"] + activity["width"] < routing["x"]
        pending = task(client, task_id)
        assert len(pending["artifacts"]) == 2
        assert pending["artifacts"][1]["parent"] == 1
        assert "Preserve CSV headers" in pending["artifacts"][1]["text"]
        assert pending["diff"]
        assert sql("SELECT count(*) FROM sample_register") == registered_before
        assert pending["route"]["selected"] and pending["route"]["excluded"]
        assert any(event["kind"] == "worker.reattached" for event in pending["timeline"])
        assert dispatches("worker") == selected_before + 1
        assert dispatches("worker-public") == excluded_before

        page.get_by_role("button", name="Approve", exact=True).click()
        expect(page.get_by_test_id("task-status")).to_have_text("Succeeded", timeout=20_000)
        done = task(client, task_id)
        assert done["receipt"]["artifact_sha256"] == done["artifacts"][1]["sha256"]
        response = client.get(f"/api/tasks/{task_id}/artifacts/2").raise_for_status()
        assert response.text == done["artifacts"][1]["text"]
        with page.expect_download() as download:
            page.get_by_role("button", name="Download version 2", exact=True).click()
        assert download.value.path().read_text() == response.text
        page.get_by_role("button", name="Governed memory", exact=True).click()
        expect(page.get_by_role("heading", name="Governed memory", exact=True)).to_be_visible()
        facts = client.get("/api/memory").raise_for_status().json()
        assert facts["facts"] and len(facts["withheld"]) >= 2
        browser.close()


@pytest.mark.parametrize(("decision", "expected"), [("deny", "denied"), ("expire", "expired")])
def test_denial_and_expiry_never_write_to_register(client, decision, expected):
    before = sql("SELECT count(*) FROM sample_register")
    task_id = UUID(client.post("/api/tasks", json={}).raise_for_status().json()["id"])
    eventually(lambda: task(client, task_id)["status"] == "awaiting_approval")
    if decision == "deny":
        client.post(f"/api/tasks/{task_id}/decision", json={"decision": "deny"}).raise_for_status()
    else:
        sql(f"UPDATE tasks SET approval_expires_at = clock_timestamp() WHERE id = '{task_id}'")
    eventually(lambda: task(client, task_id)["status"] == expected)
    assert task(client, task_id)["receipt"] is None
    assert sql("SELECT count(*) FROM sample_register") == before


def test_backup_and_restore_to_an_empty_installation(client, tmp_path):
    task_id = client.post("/api/tasks", json={}).raise_for_status().json()["id"]
    eventually(lambda: task(client, task_id)["status"] == "awaiting_approval")
    client.post(f"/api/tasks/{task_id}/decision", json={"decision": "approve"}).raise_for_status()
    eventually(lambda: task(client, task_id)["status"] == "succeeded")
    source = task(client, task_id)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    restored_token = secrets.token_hex(32)
    project = os.environ["COMPOSE_PROJECT_NAME"]
    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project + "-restore",
        "PAOS_IMAGE": project + ":local",
        "PAOS_PORT": str(port),
        "PAOS_OPERATOR_TOKEN": restored_token,
        "PAOS_DB_ADMIN_PASSWORD": secrets.token_hex(32),
        "PAOS_DB_PASSWORD": secrets.token_hex(32),
    }
    snapshot = tmp_path / "synthetic.dump"
    expected_register = sql("SELECT count(*) FROM sample_register")
    subprocess.run(["./scripts/backup", str(snapshot)], cwd=ROOT, check=True, timeout=90)
    try:
        compose("up", "-d", "--wait", "db", env=env)
        subprocess.run(
            ["./scripts/restore", str(snapshot)], cwd=ROOT, env=env, check=True, timeout=90
        )
        assert (
            compose(
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "postgres",
                "-d",
                "paos",
                "-Atc",
                "SELECT count(*) FROM operator_sessions",
                env=env,
            )
            == "0"
        )
        compose("up", "-d", "--no-build", "--wait", env=env)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=6) as restored_client:
            previous = restored_client.get(
                "/api/tasks", headers={"Authorization": client.headers["Authorization"]}
            )
            assert previous.status_code == 401
            login = restored_client.post("/api/session", json={"token": restored_token})
            restored_client.headers["Authorization"] = (
                f"Bearer {login.raise_for_status().json()['session']}"
            )
            actual = task(restored_client, task_id)
            assert actual["status"] == "succeeded"
            assert actual["receipt"] == source["receipt"]
            download = restored_client.get(f"/api/tasks/{task_id}/artifacts/2").raise_for_status()
            assert download.text == source["artifacts"][1]["text"]
        assert (
            compose(
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "postgres",
                "-d",
                "paos",
                "-Atc",
                "SELECT count(*) FROM sample_register",
                env=env,
            )
            == expected_register
        )
    finally:
        compose("down", "--volumes", env=env)
