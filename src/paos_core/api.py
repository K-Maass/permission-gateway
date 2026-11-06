"""Authenticated same-origin API and AG-UI stream for the local operator console."""

from __future__ import annotations

import asyncio
import hmac
import os
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from paos_protocol.agui.stream import snapshot_envelope
from pydantic import BaseModel, ConfigDict, Field

from paos_core.db import IntegrityError, Store
from paos_core.memory import memory_view
from paos_core.routing import route_release
from paos_core.settings import database_url, operator_token

LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)
BEARER_HEADER = re.compile(r"Bearer ([A-Za-z0-9_-]{43})", re.ASCII | re.IGNORECASE)
DEFAULT_WEB_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.operator_token = operator_token()
    app.state.store = Store(database_url())
    app.state.web = StaticFiles(
        directory=os.environ.get("PAOS_WEB_DIR", DEFAULT_WEB_DIR), check_dir=False
    )
    await run_in_threadpool(app.state.store.migrate)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error(422, "Invalid request body or path parameters")


def session_digest(token: str, session: str) -> str:
    # Keep credentials issued for the earlier cookie transport invalid as bearer tokens.
    return hmac.digest(token.encode(), b"bearer-session:" + session.encode(), "sha256").hex()


def request_session_digest(request: Request) -> str | None:
    headers = request.headers.getlist("authorization")
    match = BEARER_HEADER.fullmatch(headers[0]) if len(headers) == 1 else None
    if match is None:
        return None
    return session_digest(request.app.state.operator_token, match[1])


@app.middleware("http")
async def local_operator(request: Request, call_next) -> Response:
    host = request.headers.get("host", "")
    if not LOOPBACK_HOST.fullmatch(host):
        return error(400, "A loopback Host header is required")
    method, path = request.method, request.url.path
    api_path = path == "/api" or path.startswith("/api/")
    if api_path:
        if method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and origin != f"{request.url.scheme}://{host.lower()}":
                return error(403, "Cross-origin requests are forbidden")
            if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                return error(403, "Cross-origin requests are forbidden")
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if method == "POST" and content_type != "application/json":
                return error(415, "Expected application/json")
        if (method, path) != ("POST", "/api/session"):
            digest = request_session_digest(request)
            if digest is None or not await run_in_threadpool(
                request.app.state.store.session_valid, digest
            ):
                return error(401, "Operator sign-in is required")
            request.state.session_digest = digest
    response = await call_next(request)
    if api_path:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Login(RequestBody):
    token: str = Field(min_length=1, max_length=256)


class Decision(RequestBody):
    decision: Literal["approve", "deny"]


@app.get("/healthz")
def health(request: Request) -> Response:
    try:
        ready = request.app.state.store.is_ready()
    except psycopg.Error:
        ready = False
    return JSONResponse(
        {"status": "ok" if ready else "unavailable"}, status_code=200 if ready else 503
    )


@app.post("/api/session")
def login(body: Login, request: Request) -> dict[str, str]:
    token = request.app.state.operator_token
    if not hmac.compare_digest(token.encode(), body.token.encode()):
        raise HTTPException(401, "Invalid operator token")
    store = request.app.state.store
    session = secrets.token_urlsafe(32)
    previous_digest = request_session_digest(request)
    if previous_digest is not None:
        store.delete_session(previous_digest)
    store.new_session(session_digest(token, session))
    return {"session": session}


@app.get("/api/session", status_code=204)
def session_status() -> Response:
    return Response(status_code=204)


@app.delete("/api/session", status_code=204)
def logout(request: Request) -> Response:
    request.app.state.store.delete_session(request.state.session_digest)
    return Response(status_code=204)


@app.post("/api/tasks", status_code=201)
def create_task(body: RequestBody, request: Request) -> dict:
    route = route_release()
    selected = route["selected"]
    return request.app.state.store.create_task(
        {
            "selected": selected["id"],
            "excluded": [
                {"worker": entry["id"], "reason": entry["reason"]} for entry in route["excluded"]
            ],
        },
        selected_reason=selected["reason"],
    )


@app.get("/api/tasks")
def list_tasks(request: Request) -> list[dict]:
    return request.app.state.store.list_tasks()


def task_or_404(store: Store, task_id: UUID) -> dict:
    result = store.get_task(task_id)
    if result is None:
        raise HTTPException(404, "Task not found")
    return result


@app.get("/api/tasks/{task_id}")
def get_task(task_id: UUID, request: Request) -> dict:
    return task_or_404(request.app.state.store, task_id)


@app.post("/api/tasks/{task_id}/decision")
def decide(task_id: UUID, body: Decision, request: Request) -> dict:
    store = request.app.state.store
    result = store.decide(task_id, body.decision)
    if result == "missing":
        raise HTTPException(404, "Task not found")
    if result == "conflict":
        raise HTTPException(409, "The approval is no longer pending")
    return task_or_404(store, task_id)


@app.get("/api/tasks/{task_id}/artifacts/{version}")
def download(task_id: UUID, version: int, request: Request) -> Response:
    if version not in (1, 2):
        raise HTTPException(404, "Artifact not found")
    try:
        artifact = request.app.state.store.get_artifact(task_id, version)
    except IntegrityError as exc:
        raise HTTPException(409, "Artifact integrity check failed") from exc
    if artifact is None:
        raise HTTPException(404, "Artifact not found")
    return Response(
        artifact,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="release-summary-v{version}.txt"'},
    )


@app.get("/api/tasks/{task_id}/stream")
async def stream(task_id: UUID, request: Request) -> StreamingResponse:
    store = request.app.state.store
    initial = await run_in_threadpool(task_or_404, store, task_id)
    initial_seq = initial["timeline"][-1]["seq"]
    header = request.headers.get("last-event-id", "")
    last_seq = int(header) if re.fullmatch(r"[0-9]{1,10}", header) else -1
    if last_seq > initial_seq:
        last_seq = -1
    digest = request.state.session_digest

    async def frames() -> AsyncIterator[str]:
        nonlocal last_seq
        current = initial
        loop = asyncio.get_running_loop()
        heartbeat_at = loop.time() + 10
        while True:
            seq = current["timeline"][-1]["seq"]
            if seq > last_seq:
                yield snapshot_envelope(seq, current).to_frame()
                last_seq = seq
                heartbeat_at = loop.time() + 10
            elif loop.time() >= heartbeat_at:
                yield ": keep-alive\n\n"
                heartbeat_at = loop.time() + 10
            await asyncio.sleep(0.5)
            if not await run_in_threadpool(store.session_valid, digest):
                return
            current = await run_in_threadpool(task_or_404, store, task_id)

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@app.get("/api/memory")
def get_memory() -> dict:
    return memory_view()


@app.get("/{path:path}", include_in_schema=False)
async def web(path: str, request: Request) -> Response:
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "API endpoint not found")
    static = request.app.state.web
    return await static.get_response(
        static.get_path({"path": f"/{path or 'index.html'}"}), request.scope
    )
