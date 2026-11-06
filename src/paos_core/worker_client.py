"""Project approved facts into the A2A adapter and translate its release updates."""

from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from paos_domain import TaskState, content_address
from paos_domain.drivers import (
    ContextPolicy,
    OutputContract,
    ProgressKind,
    ProjectContext,
    WorkerEvent,
    WorkerInvocation,
)
from paos_domain.ids import RunId, TaskId
from paos_protocol.a2a import A2AProtocolError, A2AWorkerAdapter
from paos_protocol.a2a.mapping import CONTENT_HASH_REF, SYNTHESISED_FINAL_REF, TASK_SNAPSHOT_REF

from paos_core.memory import TASK_CLASSIFICATION, Fact
from paos_core.routing import RELEASE_WORKER_ID, dispatch_url

_VERSIONS = {"release-summary-v1": 1, "release-summary-v2": 2}


@dataclass(frozen=True, slots=True)
class ReleaseUpdate:
    kind: Literal["handle", "progress", "artifact", "completed", "failed"]
    message: str = ""
    external_task_id: str = ""
    context_id: str = ""
    version: int | None = None
    body: bytes | None = None


async def _events(
    adapter: A2AWorkerAdapter, invocation: WorkerInvocation, *, reattach: bool
) -> AsyncIterator[WorkerEvent]:
    if reattach:
        snapshot = await adapter.poll(invocation.run_id)
        if any(event.kind is ProgressKind.TERMINAL for event in snapshot):
            for event in snapshot:
                yield event
            return

    stream = await adapter.invoke(invocation)
    try:
        async with aclosing(stream):
            async for event in stream:
                if (
                    event.kind is ProgressKind.TERMINAL
                    and event.terminal_status is TaskState.SUCCEEDED
                    and TASK_SNAPSHOT_REF not in event.external_refs
                ):
                    snapshot = await adapter.poll(invocation.run_id)
                    if not any(item.terminal_status is TaskState.SUCCEEDED for item in snapshot):
                        raise RuntimeError("The completed A2A task has no successful snapshot")
                    for item in snapshot:
                        if item.kind is not ProgressKind.STATUS:
                            yield item
                    return
                yield event
                if event.kind is ProgressKind.TERMINAL:
                    return
    except A2AProtocolError:
        # The task can finish after GetTask and before SubscribeToTask is accepted.
        if reattach:
            snapshot = await adapter.poll(invocation.run_id)
            if any(event.kind is ProgressKind.TERMINAL for event in snapshot):
                for event in snapshot:
                    yield event
                return
        raise


async def run_release(
    worker_url: str,
    *,
    run_id: UUID,
    objective: str,
    facts: Sequence[Fact],
    external_task_id: str = "",
    context_id: str = "",
) -> AsyncIterator[ReleaseUpdate]:
    """Yield the external handle before any progress or artifact can be persisted."""
    if bool(external_task_id) != bool(context_id):
        raise ValueError("Reattachment requires the external task and context IDs")
    adapter = A2AWorkerAdapter(RELEASE_WORKER_ID, dispatch_url(run_id, worker_url))
    invocation = WorkerInvocation(
        run_id=RunId(run_id),
        task_id=TaskId(run_id),
        objective=objective,
        project_context=ProjectContext(
            project_name="Demo release",
            policy=ContextPolicy.ACCEPTED_CLAIMS,
            data_ceiling=TASK_CLASSIFICATION.value,
            known_facts=[fact["statement"] for fact in facts],
        ),
        output_contract=OutputContract(
            expects_artifact=True, artifact_kind="release summary", mime_type="text/markdown"
        ),
    )
    reattach = bool(external_task_id)
    versions: dict[int, ReleaseUpdate] = {}
    snapshot_versions: set[int] = set()
    live_artifact_starts: set[str] = set()
    try:
        if external_task_id:
            adapter.adopt(
                invocation.run_id, external_task_id=external_task_id, context_id=context_id
            )
        events = _events(adapter, invocation, reattach=reattach)
        async with aclosing(events):
            async for event in events:
                peer_task_id = event.external_task_id
                peer_context_id = event.external_refs.get("a2a_context_id", "")
                if not external_task_id and peer_task_id and peer_context_id:
                    external_task_id, context_id = peer_task_id, peer_context_id
                    yield ReleaseUpdate(
                        "handle", external_task_id=external_task_id, context_id=context_id
                    )
                if not external_task_id or (peer_task_id and peer_task_id != external_task_id):
                    raise RuntimeError("Worker update did not identify the expected A2A task")
                if peer_context_id and peer_context_id != context_id:
                    raise RuntimeError("Worker update did not match the recorded A2A context")

                if event.kind is ProgressKind.ARTIFACT:
                    native = event.external_refs.get("a2a_artifact_id", "")
                    version = _VERSIONS.get(native)
                    if version is None or SYNTHESISED_FINAL_REF in event.external_refs:
                        continue
                    is_snapshot = TASK_SNAPSHOT_REF in event.external_refs
                    if is_snapshot:
                        live_artifact_starts.discard(native)
                    elif event.external_refs.get("a2a_append") != "true":
                        live_artifact_starts.add(native)
                    elif native not in live_artifact_starts:
                        # Subscribe can overlap a snapshot with already-queued append events.
                        # Use the completed GetTask snapshot for this version's canonical bytes.
                        continue
                    digest = event.external_refs.get(CONTENT_HASH_REF)
                    if digest is None:
                        continue
                    body = adapter.artifact_bytes_for(invocation.run_id, native)
                    if body is None or content_address(body) != digest:
                        raise RuntimeError("The A2A artifact content did not match its digest")
                    if is_snapshot:
                        snapshot_versions.add(version)
                    previous = versions.get(version)
                    if previous is not None:
                        if previous.body != body:
                            raise RuntimeError("An A2A artifact version changed after finalization")
                        continue
                    artifact = ReleaseUpdate("artifact", event.message, version=version, body=body)
                    versions[version] = artifact
                    if not reattach:
                        yield artifact
                elif event.kind is ProgressKind.TERMINAL:
                    if event.terminal_status is not TaskState.SUCCEEDED:
                        yield ReleaseUpdate("failed", event.message)
                    elif snapshot_versions != set(_VERSIONS.values()):
                        yield ReleaseUpdate(
                            "failed", "The worker did not return both artifact versions"
                        )
                    else:
                        if reattach:
                            for version in sorted(versions):
                                yield versions[version]
                        yield ReleaseUpdate("completed", event.message)
                elif event.kind in {
                    ProgressKind.STATUS,
                    ProgressKind.LOG,
                } and not event.message.startswith(("TASK_STATE_", "attached to ")):
                    yield ReleaseUpdate("progress", event.message)
    finally:
        await adapter.aclose()
