"""`A2AWorkerAdapter` — an A2A agent behind the kernel's `WorkerAdapter` boundary.

Roadmap 8.1: *do not implement protocol framing from scratch*. Framing, transport negotiation,
and streaming are the official SDK's; this file is the seam that keeps the SDK's vocabulary out
of the kernel. Nothing here yields an `a2a` type — `mapping.py` translates every frame first.

Two things the boundary makes explicit:

**This adapter never retries.** It classifies and re-raises, and Restate retries the durable
step. An adapter with its own retry loop would retry *inside* a journaled step, so a crash
mid-loop would replay work Restate believed had not started, and the two backoffs would
multiply.

**Streaming is consumed outside the durable handler.** `kernel/worker_gateway.py` explains why:
a real agent's chunking is not replay-deterministic, so a handler that consumed the stream
could never be replayed. This adapter is driven by that gateway, not by a handler.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx
from a2a.client import (
    AuthInterceptor,
    Client,
    ClientCallInterceptor,
    ClientConfig,
    ClientFactory,
    CredentialService,
)
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
)
from paos_domain import uuid7
from paos_domain.drivers import ProgressKind, WorkerEvent, WorkerInvocation
from paos_domain.ids import RunId
from paos_domain.observability import TraceRef
from paos_domain.secrets import SecretUrl, as_url, cause_for, display_url, origin_of, scrub

from paos_protocol.a2a import mapping
from paos_protocol.a2a.cards import CachedCard, CardCache
from paos_protocol.a2a.errors import (
    A2AEndpointMismatchError,
    A2AProtocolError,
    A2ATransportError,
)
from paos_protocol.a2a.projection import render_invocation

TRACEPARENT = "traceparent"


def pin_to_endpoint(card: AgentCard, endpoint: str | SecretUrl) -> None:
    """Force every interface on `card` to address the configured endpoint. Mutates in place.

    **Why this exists.** `ClientFactory.create` builds its transport at `selected_interface.url`,
    and the a2a card resolver promotes a legacy card's top-level `url` into that field verbatim.
    The card is fetched from the agent, so without this the agent picks the destination — and for a
    worker deliberately given no egress, the gateway is the one component that has it. A
    prompt-injected Agent Zero could name an attacker host and have PAOS post the objective,
    project context and its own credentials there.

    **Refuse first, then pin.** The refusal is the security property: a card naming a different
    origin is evidence of a compromised or misconfigured worker, and continuing quietly against a
    rewritten URL would hide that. The pin that follows is defence in depth — it normalises a
    matching origin so nothing downstream can re-derive a destination from card text.

    **Origin, not the whole URL.** The path is left alone: it is how the agent routes A2A within
    its own process, it cannot redirect packets to another host, and overwriting it would break
    every agent that serves A2A somewhere other than `/`.
    """
    configured = origin_of(endpoint)
    for interface in card.supported_interfaces:
        if origin_of(interface.url) != configured:
            scheme, host, port = configured
            raise A2AEndpointMismatchError(
                f"agent card names {origin_of(interface.url)} but PAOS dispatches to "
                f"{(scheme, host, port)}; refusing to send this worker's traffic to an origin it "
                "chose for itself"
            )
        # Rebuilt from the configured origin rather than trusted as-is, so a card that merely
        # *compares* equal cannot smuggle through a spelling the comparison normalised away.
        parsed = urlsplit(interface.url)
        base = urlsplit(as_url(endpoint))
        interface.url = urlunsplit(
            (base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment)
        )


@dataclass
class RunHandle:
    """What the adapter remembers about a live run.

    `context_id` is the half that matters after a restart: A2A's context is what "resume by
    context" in this phase's acceptance gate resumes *by*, and it is assigned by the agent, so
    it can only be learned by listening.
    """

    external_task_id: str = ""
    context_id: str = ""
    terminal: bool = False
    seen_artifacts: set[str] = field(default_factory=set[str])
    artifact_bytes: dict[str, bytes] = field(default_factory=dict[str, bytes])
    """Accumulated content per native artifact id, so the final hash covers the whole file.

    A2A appends an artifact in chunks. Hashing only the last chunk would give the artifact an
    identity derived from its tail, and two files sharing a final chunk would collide.
    """

    finalised_artifacts: set[str] = field(default_factory=set[str])
    """Natives the peer itself marked `lastChunk` — as opposed to merely `seen_artifacts`.

    The difference is what `_settle_artifacts` acts on. `lastChunk` is optional in A2A and some
    servers never send it, so "seen" and "finished" are genuinely different facts and collapsing
    them would either drop good output or promote abandoned output.
    """


class A2AWorkerAdapter:
    """One remote A2A agent, exposed as a `WorkerAdapter`.

    One adapter per agent, not per run: the card cache, the httpx connection pool and the
    client are all per-agent state, and rebuilding them per run would re-fetch the card and
    re-negotiate transport on every dispatch.
    """

    def __init__(
        self,
        worker_id: str,
        base_url: str | SecretUrl,
        *,
        http: httpx.AsyncClient | None = None,
        cards: CardCache | None = None,
        streaming: bool = True,
        credentials: CredentialService | None = None,
    ) -> None:
        self._worker_id = worker_id
        # Held as given. A `SecretUrl` renders redacted from `str()`/`repr()`, so the three error
        # messages below and any log line that interpolates it are safe by construction rather
        # than by remembering — which matters because Agent Zero carries its A2A credential in
        # the URL *path*, so "the endpoint" and "the secret" are the same string.
        raw = as_url(base_url).rstrip("/")
        self._endpoint: str | SecretUrl = (
            SecretUrl(raw, secret=base_url.reveal().rsplit("/t-", 1)[-1])
            if isinstance(base_url, SecretUrl)
            else raw
        )
        self._credentials = credentials
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
        self._cards = cards or CardCache(http=self._http)
        self._streaming = streaming
        self._client: Client | None = None
        self._runs: dict[RunId, RunHandle] = {}

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def base_url(self) -> str:
        """The endpoint, **redacted**.

        A property this generic gets interpolated into logs and diagnostics by callers who have
        no idea a credential is in it. Handing out the real value here would undo the whole point;
        code that needs to connect goes through `self._endpoint`.
        """
        return display_url(self._endpoint)

    async def card(self, *, force: bool = False) -> CachedCard:
        # The `SecretUrl` itself, **not** `as_url(...)`. The cache reveals it internally to
        # fetch and to key, and keeps the redacted form for its own discovery error — which is
        # the message a dead endpoint actually produces. Revealing it here handed the cache a
        # plain string with no secret to scrub, and the token surfaced in that error verbatim.
        return await self._cards.get(self._endpoint, force=force)

    async def capabilities(self) -> list[str]:
        """What this agent can do, in PAOS's vocabulary rather than its own."""
        return (await self.card()).capabilities

    async def _ensure_client(self) -> Client:
        if self._client is not None:
            return self._client
        entry = await self.card()
        config = ClientConfig(
            httpx_client=self._http,
            streaming=self._streaming,
            # Polling is the fallback when the agent does not support streaming. Left on so an
            # agent that advertises no streaming still runs, rather than being unroutable.
            polling=True,
        )
        # Auth (roadmap 8.2) rides on an interceptor rather than a header this adapter builds
        # itself: the agent's card declares which security scheme it wants, and the interceptor
        # asks the credential service for that scheme by name. Hand-rolling an Authorization
        # header would mean guessing the scheme, and guessing wrong reads as a 401 the retry
        # logic would keep retrying.
        interceptors: list[ClientCallInterceptor] | None = (
            [AuthInterceptor(self._credentials)] if self._credentials is not None else None
        )
        # **Before the factory sees the card.** `ClientFactory.create` takes its destination from
        # `selected_interface.url`, so an unpinned card chooses where every subsequent
        # `send_message`, `subscribe` and `get_task` goes — including, for a compromised worker, a
        # host the gateway can reach and the worker cannot.
        pin_to_endpoint(entry.card, self._endpoint)
        try:
            self._client = ClientFactory(config).create(entry.card, interceptors)
        except Exception as exc:
            raise A2AProtocolError(
                f"cannot build an A2A client for {display_url(self._endpoint)}: "
                f"{scrub(self._endpoint, str(exc))}"
            ) from exc
        return self._client

    def _message_for(self, invocation: WorkerInvocation, handle: RunHandle) -> Message:
        """The invocation as an A2A message, with the trace carried in metadata.

        Trace propagation (roadmap 8.2): the id is minted before the boundary is crossed, per
        `TraceRef`'s own docstring, so the agent's spans join this task's trace instead of
        arriving as an orphan tree.
        """
        # One renderer, because the worker reads prose whatever PAOS models. `metadata` is not an
        # option: Step 0 established that A2A message metadata never reaches Hermes's model, so
        # anything not in the text part is not communicated at all.
        body = render_invocation(invocation)

        message = Message(
            message_id=str(uuid7()),
            role=Role.ROLE_USER,
            parts=[Part(text=body)],
        )
        if handle.context_id:
            message.context_id = handle.context_id
        if handle.external_task_id:
            message.task_id = handle.external_task_id
        if invocation.trace_id:
            message.metadata[TRACEPARENT] = TraceRef(trace_id=invocation.trace_id).traceparent()
        if invocation.workspace_lease is not None:
            # The one thing prose cannot carry. A worktree is a path on a shared filesystem, and a
            # worker cannot act on a sentence describing one — so for the shims PAOS authors, the
            # lease crosses as structure. Third-party workers ignore metadata they do not know,
            # which is why adding it here does not change what any existing worker receives.
            message.metadata[mapping.WORKSPACE_LEASE_KEY] = invocation.workspace_lease.model_dump(
                mode="json"
            )
        return message

    async def invoke(self, invocation: WorkerInvocation) -> AsyncIterator[WorkerEvent]:
        """Dispatch the run and stream the agent's output as `WorkerEvent`s.

        Reattaches rather than re-dispatches when a handle is already known: after an
        orchestrator restart the run is still going on the agent's side, and sending the message
        again would start a second one.
        """
        client = await self._ensure_client()
        handle = self._runs.setdefault(invocation.run_id, RunHandle())

        if handle.external_task_id:
            return self._consume(
                client.subscribe(SubscribeToTaskRequest(id=handle.external_task_id)),
                invocation.run_id,
                handle,
            )

        request = SendMessageRequest(message=self._message_for(invocation, handle))
        return self._consume(client.send_message(request), invocation.run_id, handle)

    async def _consume(
        self,
        frames: AsyncIterator[StreamResponse],
        run_id: RunId,
        handle: RunHandle,
    ) -> AsyncIterator[WorkerEvent]:
        """Translate the SDK's frames, remembering the handles a resume will need.

        A stream that ends without a terminal frame gets one synthesised. The agent going quiet
        is not a verdict, but the kernel suspends on a promise only a terminal event resolves —
        so silence has to become an explicit FAILED here, or the run hangs until its TTL.
        """
        emitted_terminal = False
        try:
            async for frame in frames:
                accumulated = self._accumulate(handle, frame)
                for event in mapping.stream_events(run_id, frame, accumulated=accumulated):
                    if event.external_task_id and not handle.external_task_id:
                        handle.external_task_id = event.external_task_id
                    context = event.external_refs.get("a2a_context_id")
                    if context:
                        handle.context_id = context
                    if event.kind is ProgressKind.ARTIFACT:
                        native = event.external_refs.get("a2a_artifact_id", "")
                        handle.seen_artifacts.add(native)
                        if event.external_refs.get("a2a_last_chunk") == "true":
                            handle.finalised_artifacts.add(native)
                    if event.kind is ProgressKind.TERMINAL:
                        # Settle *before* the terminal, never after. Phase 11 Step 0 verified
                        # that the terminal frame is the last one a consumer sees, and
                        # `worker_gateway.pump` stops forwarding progress once it arrives — so an
                        # artifact emitted afterwards would be silently dropped by the very code
                        # this exists to feed.
                        for settled in self._settle_artifacts(
                            run_id, handle, event.terminal_status
                        ):
                            yield settled
                        emitted_terminal = True
                        handle.terminal = True
                    yield event
        except (httpx.HTTPError, ConnectionError, TimeoutError) as exc:
            raise A2ATransportError(
                f"A2A stream to {display_url(self._endpoint)} failed: "
                f"{scrub(self._endpoint, str(exc))}"
            ) from cause_for(self._endpoint, exc)
        except Exception as exc:  # noqa: BLE001 — normalize errors from SDK iterators
            raise A2AProtocolError(
                f"unreadable A2A response from {display_url(self._endpoint)}: "
                f"{scrub(self._endpoint, str(exc))}"
            ) from cause_for(self._endpoint, exc)

        if not emitted_terminal:
            # Silence is not success, so anything unfinalised is discarded rather than promoted.
            for settled in self._settle_artifacts(run_id, handle, mapping.PaosTaskState.FAILED):
                yield settled
            yield WorkerEvent(
                kind=ProgressKind.TERMINAL,
                run_id=run_id,
                message="the agent closed the stream without reporting an outcome",
                terminal_status=mapping.PaosTaskState.FAILED,
                external_task_id=handle.external_task_id,
            )

    def _settle_artifacts(
        self,
        run_id: RunId,
        handle: RunHandle,
        verdict: mapping.PaosTaskState | None,
    ) -> list[WorkerEvent]:
        """Decide what becomes of artifacts the peer streamed but never marked final.

        **Terminality is not completeness.** A run that was cancelled halfway also has bytes and
        also never marked them final. Finalising on *any* terminal state would turn abandoned
        partial output into a canonical, content-addressed deliverable that looks exactly like a
        finished one — the worst kind of wrong, because nothing downstream could tell.

        So the verdict decides:

        - **SUCCEEDED** — the peer finished and simply never set `lastChunk` (A2A leaves it
          optional; Hermes omits the field entirely). Synthesise the finalisation so the artifact
          can be ingested, and mark it as synthesised so the inference is auditable.
        - **anything else** — discard, and say so on the run. `ArtifactVersion` has no way to
          represent an incomplete version, so persisting one would mean inventing schema that
          belongs to the Artifact Service phase; a LOG event keeps the discard visible instead of
          silent, and is where a completeness representation would later plug in.

        An artifact the peer *did* mark final is untouched by any of this.
        """
        pending = sorted(handle.seen_artifacts - handle.finalised_artifacts)
        if not pending:
            return []

        if verdict is not mapping.PaosTaskState.SUCCEEDED:
            outcome = verdict.value if verdict is not None else "unknown"
            return [
                WorkerEvent(
                    kind=ProgressKind.LOG,
                    run_id=run_id,
                    message=(
                        f"discarded {len(pending)} unfinished artifact(s) from a run that ended "
                        f"{outcome}: the agent streamed content but never marked it complete, "
                        "and partial output must not become a canonical artifact"
                    ),
                    external_task_id=handle.external_task_id,
                    external_refs={"a2a_discarded_artifacts": ",".join(pending)},
                )
            ]

        settled: list[WorkerEvent] = []
        for native in pending:
            handle.finalised_artifacts.add(native)
            settled.append(
                mapping.finalised_artifact_event(
                    run_id,
                    native_artifact_id=native,
                    body=handle.artifact_bytes.get(native, b""),
                    external_task_id=handle.external_task_id,
                    context_id=handle.context_id,
                )
            )
        return settled

    @staticmethod
    def _accumulate(handle: RunHandle, frame: StreamResponse) -> bytes:
        """Append this frame's artifact bytes and return the running total.

        Kept on the adapter rather than in `mapping`, which is deliberately stateless: chunk
        accumulation is per-run state, and a pure translation function that quietly remembered
        things between calls would be far harder to reason about than one that does not.
        """
        which = frame.WhichOneof("payload")
        if which == "task":
            # Snapshots recover bytes emitted while the gateway was disconnected. The
            # mapping carries the separate verdict about whether those bytes are final.
            for artifact in frame.task.artifacts:
                native = artifact.artifact_id
                handle.artifact_bytes[native] = mapping.artifact_bytes(artifact)
            return b""
        if which != "artifact_update":
            return b""
        update = frame.artifact_update
        native = str(update.artifact.artifact_id)
        chunk = mapping.artifact_bytes(update.artifact)
        previous = handle.artifact_bytes.get(native, b"") if update.append else b""
        total = previous + chunk
        handle.artifact_bytes[native] = total
        return total

    def artifact_bytes_for(self, run_id: RunId, native_artifact_id: str) -> bytes | None:
        """The content this run accumulated for one native artifact, if it is still held.

        The adapter is the only place the bytes exist: A2A streams them as chunks and nothing
        downstream sees more than a hash. Exposed rather than pushed onto the `WorkerEvent`
        because events are journaled and read back as progress — an artifact's whole content
        does not belong in a log.

        `None` once the handle is gone, which is the honest answer after a restart: the bytes
        were never persisted here, and the version already recorded says where they went.
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        return handle.artifact_bytes.get(native_artifact_id)

    async def cancel(self, run_id: RunId, *, reason: str) -> None:
        """Ask the agent to stop.

        Never raises. Conformance requires this and so does the kernel: cancel is called from a
        shared handler, from restart paths, and sometimes twice, so a raise here turns a clean
        stop into a retry loop against an agent already being told to stop. A run this adapter
        has never seen — the ordinary case after a restart — is a no-op.
        """
        handle = self._runs.get(run_id)
        if handle is None or not handle.external_task_id or handle.terminal:
            return
        with contextlib.suppress(Exception):
            client = await self._ensure_client()
            await client.cancel_task(CancelTaskRequest(id=handle.external_task_id))
        handle.terminal = True

    async def poll(self, run_id: RunId) -> list[WorkerEvent]:
        """One-shot state fetch — roadmap 8.2's task polling, for agents that do not stream.

        Also the reconciliation path: a supervisor asking "is this run actually still alive?"
        should not have to open a subscription to find out.
        """
        handle = self._runs.get(run_id)
        if handle is None or not handle.external_task_id:
            return []
        client = await self._ensure_client()
        try:
            task = await client.get_task(GetTaskRequest(id=handle.external_task_id))
        except Exception as exc:
            raise A2ATransportError(f"could not poll {handle.external_task_id}: {exc}") from exc
        self._accumulate(handle, StreamResponse(task=task))
        return mapping.task_snapshot_events(run_id, task)

    def handle_for(self, run_id: RunId) -> RunHandle | None:
        """The remembered handles for a run — what a resume needs, and what tests assert on."""
        return self._runs.get(run_id)

    def adopt(self, run_id: RunId, *, external_task_id: str, context_id: str = "") -> None:
        """Restore a handle the kernel journaled before this process existed.

        The whole point of journaling `worker_external_task_id`: after a restart the adapter is
        new but the agent's task is not, so the kernel hands the handle back and `invoke`
        reattaches instead of dispatching a second run.
        """
        handle = self._runs.setdefault(run_id, RunHandle())
        handle.external_task_id = external_task_id or handle.external_task_id
        handle.context_id = context_id or handle.context_id

    async def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        if self._owns_http:
            await self._http.aclose()


__all__ = ["TRACEPARENT", "A2AWorkerAdapter", "RunHandle"]
