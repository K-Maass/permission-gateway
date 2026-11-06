"""A2A's vocabulary translated into the kernel's — roadmap 8.2's context mapping.

The rule this file exists to enforce is in `WorkerEvent`'s own docstring:

    Every adapter converts its protocol's native events into these, so the orchestrator never
    learns A2A's or ACP's vocabulary.

So no `a2a` type crosses out of this module. Everything here is pure — no network, no client,
no clock — because the interesting failures are *translation* failures, and a translation bug
found only against a live agent is found too late.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from a2a.types import (
    Artifact,
    Message,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict
from paos_domain import content_address
from paos_domain.drivers import EffectProposal, ProgressKind, SelectionRequest, WorkerEvent
from paos_domain.ids import ArtifactId, RunId
from paos_domain.task import TaskState as PaosTaskState
from pydantic import ValidationError

from paos_protocol.a2a.errors import A2AProtocolError

# A2A's states, by protobuf enum value. Named constants rather than the generated wrapper so
# the mapping reads as a table and an unhandled state is visible as a gap rather than a
# silently-defaulted branch.
_SUBMITTED = TaskState.Value("TASK_STATE_SUBMITTED")
_WORKING = TaskState.Value("TASK_STATE_WORKING")
_COMPLETED = TaskState.Value("TASK_STATE_COMPLETED")
_FAILED = TaskState.Value("TASK_STATE_FAILED")
_CANCELED = TaskState.Value("TASK_STATE_CANCELED")
_INPUT_REQUIRED = TaskState.Value("TASK_STATE_INPUT_REQUIRED")
_REJECTED = TaskState.Value("TASK_STATE_REJECTED")
_AUTH_REQUIRED = TaskState.Value("TASK_STATE_AUTH_REQUIRED")
_UNSPECIFIED = TaskState.Value("TASK_STATE_UNSPECIFIED")

TERMINAL_A2A_STATES: frozenset[int] = frozenset({_COMPLETED, _FAILED, _CANCELED, _REJECTED})

_VERDICTS: dict[int, PaosTaskState] = {
    _COMPLETED: PaosTaskState.SUCCEEDED,
    _FAILED: PaosTaskState.FAILED,
    _CANCELED: PaosTaskState.CANCELLED,
    # Rejected is the agent declining the work outright. It is a finished run that produced
    # nothing, which is FAILED — not CANCELLED, which in PAOS means *we* stopped it.
    _REJECTED: PaosTaskState.FAILED,
}

ARTIFACT_NAMESPACE = uuid5(NAMESPACE_URL, "https://paos.local/a2a/artifact")
"""Namespace for deriving canonical artifact ids from A2A's own.

Derived, never random. A2A streams an artifact as a sequence of `append` chunks under one
`artifact_id`, and an orchestrator that restarts mid-run reattaches and sees those chunks
again. Minting a fresh id per sighting would turn one artifact into several — the resume path
in this phase's own acceptance gate is exactly where that happens.
"""


def canonical_artifact_id(run_id: RunId, native_artifact_id: str) -> ArtifactId:
    """The canonical id for one A2A artifact within one run.

    Scoped to the run so two runs that both receive an agent's `artifact-1` get distinct
    canonical artifacts — the native id is only unique within the agent's own task.

    This is the roadmap 2.3 boundary in one function: the native id goes in, a canonical id
    comes out, and the native one survives only as a cross-reference.
    """
    return ArtifactId(uuid5(ARTIFACT_NAMESPACE, f"{run_id}:{native_artifact_id}"))


def is_terminal(state: int) -> bool:
    return state in TERMINAL_A2A_STATES


def verdict_for(state: int) -> PaosTaskState:
    """The PAOS verdict for a terminal A2A state.

    Unknown states fail closed. A state this adapter has never heard of is not evidence of
    success, and treating it as success would mark a run complete and release its lease on the
    strength of a value the code does not understand.
    """
    return _VERDICTS.get(state, PaosTaskState.FAILED)


def _text_of(message: Message | None) -> str:
    if message is None:
        return ""
    return " ".join(part.text for part in message.parts if part.HasField("text")).strip()


def _artifact_text(artifact: Artifact) -> str:
    named = artifact.name or artifact.artifact_id
    return named or "artifact"


EFFECT_PROPOSAL_KEY = "paos.effect_proposal"
"""Where a shim announces that its worker has parked on a consequential action.

**A2A has no vocabulary for "I need authorization to act".** Its closest states are
`INPUT_REQUIRED` and `AUTH_REQUIRED`, and both mean *the agent is waiting for a human to type
something* — neither carries a proposed action, a target, or a risk, which are exactly the three
things a policy engine needs. So the proposal travels as metadata on an ordinary working-state
update, and the A2A state stays `WORKING`, which is the truth: the agent has not finished and is
not asking the *client* for anything.

**Observing is not deciding.** What crosses here is the fact that authorization is required, and
enough detail to decide it. The decision itself never travels back over A2A — it goes through
PAOS's Effect/OPA/Approval path and is delivered to the worker out of band, on the worker's own
control API. An A2A client that can see this metadata learns that a run is waiting; it gains no
ability to answer, which is what keeps the approval authority inside PAOS rather than in
whatever happens to be holding the stream.
"""


SELECTION_REQUEST_KEY = "paos.selection_request"
"""Where a shim announces that its worker is waiting on a *choice* — roadmap Phase 25.

**The same problem as `EFFECT_PROPOSAL_KEY`, one step along.** A2A's `INPUT_REQUIRED` carries a
message and nothing else: no options, no ids, no way to say "one of these three". A choice needs all
three, so like a proposal it travels as metadata on an ordinary WORKING update.

The state remains WORKING. An operator answers the selection through the authenticated
PAOS control surface.

**Reading it is not permission to act on it.** The options are the *worker's* claim about what it is
willing to do. PAOS records them as the offer and later matches an answer against them; it never
learns what an option means, and never lets a client name one that was not offered.
"""


WORKSPACE_LEASE_KEY = "paos.workspace_lease"
"""Where PAOS tells a shim it authored which worktree a run was given.

**Only meaningful to a shim PAOS writes both ends of.** An invocation crosses A2A as prose,
because a third-party agent's model never sees message metadata — that is why `render_invocation`
exists and why nothing load-bearing is put here for workers like Hermes. But a shim in
`paos_adapters.workers` is PAOS's own server code reading the raw request, not a model reading a
transcript, so for those the lease can cross intact instead of being described in a sentence.

It has to, for the coding plane. A worktree is a *path on a shared filesystem*, allocated per run
against a recorded base commit; prose can mention it but a worker cannot act on a mention. Without
this the shim would have to invent its own workspace convention, and the isolation Phase 14
allocates would stop at the A2A boundary.

**Receiving this is not permission to use it.** The path names a location the *client* chose, so a
shim must prove it lies inside a root the shim itself was configured with before letting an agent
write there. `AcpAgentExecutor` does that; anything else reading this key must too.
"""


def _proposal_of(update: TaskStatusUpdateEvent, run_id: RunId) -> EffectProposal | None:
    """The effect proposal carried on a status update, if there is one.

    Absent metadata is the ordinary case and returns `None`. Malformed metadata is *not* silently
    ignored: a proposal that fails to parse would downgrade to a plain status line, the worker
    would stay parked, and the run would look like it was merely slow. Raising names the field.
    """
    if EFFECT_PROPOSAL_KEY not in update.metadata:
        return None
    raw = MessageToDict(update.metadata)[EFFECT_PROPOSAL_KEY]
    if not isinstance(raw, dict):
        raise A2AProtocolError(
            f"{EFFECT_PROPOSAL_KEY} must be an object describing the proposed action, "
            f"got {type(raw).__name__}"
        )
    fields = cast("dict[str, Any]", raw)
    try:
        return EffectProposal.model_validate(fields)
    except ValidationError as error:
        raise A2AProtocolError(
            f"a worker proposed an effect PAOS cannot decide on: {error}. A proposal must carry "
            "the action, its target, a risk class, a stable proposal key and the worker's own "
            "handle for the parked decision."
        ) from error


def _selection_of(update: TaskStatusUpdateEvent) -> SelectionRequest | None:
    """The selection request carried on a status update, if there is one.

    Absent metadata is the ordinary case and returns `None`. Malformed metadata raises rather than
    degrading to a status line, for the reason `_proposal_of` raises: a choice that failed to parse
    would leave the worker parked while the run merely looked slow.
    """
    if SELECTION_REQUEST_KEY not in update.metadata:
        return None
    raw = MessageToDict(update.metadata)[SELECTION_REQUEST_KEY]
    if not isinstance(raw, dict):
        raise A2AProtocolError(
            f"{SELECTION_REQUEST_KEY} must be an object describing the choice, "
            f"got {type(raw).__name__}"
        )
    fields = cast("dict[str, Any]", raw)
    try:
        return SelectionRequest.model_validate(fields)
    except ValidationError as error:
        raise A2AProtocolError(
            f"a worker asked for a choice PAOS cannot present: {error}. A selection must carry a "
            "prompt and between one and ten options, each with an id and a label."
        ) from error


def status_event(
    run_id: RunId,
    update: TaskStatusUpdateEvent,
) -> WorkerEvent:
    """One A2A status update as a `WorkerEvent`.

    `TASK_STATE_UNSPECIFIED` is protobuf's zero value — it means the field was never set, not
    that the agent is in an "unspecified" state. Reporting it as a status is honest; treating
    it as terminal would end a run because a field was missing.
    """
    state = update.status.state
    detail = _text_of(update.status.message)
    common: dict[str, str] = {"a2a_task_state": TaskState.Name(state)}
    if update.context_id:
        common["a2a_context_id"] = update.context_id

    # Checked before the state branches, and deliberately so. A parked worker is *working*, not
    # finished and not asking the client for input, so this rides an ordinary WORKING update —
    # and reading the state first would classify it as plain progress and drop the proposal.
    selection = _selection_of(update)
    if selection is not None:
        return WorkerEvent(
            kind=ProgressKind.INPUT_REQUEST,
            run_id=run_id,
            message=selection.prompt,
            selection_request=selection,
            external_task_id=update.task_id,
            external_refs=common | {"worker_decision_ref": selection.decision_ref},
        )

    proposal = _proposal_of(update, run_id)
    if proposal is not None:
        return WorkerEvent(
            kind=ProgressKind.EFFECT_PROPOSAL,
            run_id=run_id,
            message=proposal.summary or f"{proposal.tool} awaiting authorization",
            effect_proposal=proposal,
            external_task_id=update.task_id,
            external_refs=common | {"worker_decision_ref": proposal.decision_ref},
        )

    if is_terminal(state):
        return WorkerEvent(
            kind=ProgressKind.TERMINAL,
            run_id=run_id,
            message=detail,
            terminal_status=verdict_for(state),
            external_task_id=update.task_id,
            external_refs=common,
        )

    if state == _INPUT_REQUIRED:
        return WorkerEvent(
            kind=ProgressKind.INPUT_REQUEST,
            run_id=run_id,
            message=detail or "the agent is waiting for input",
            external_task_id=update.task_id,
            external_refs=common,
        )

    if state == _AUTH_REQUIRED:
        # Also an input request: the run is suspended until a human supplies something. It is
        # kept distinguishable through `a2a_task_state` rather than given its own kind, because
        # inventing a PAOS state for it would commit the kernel to a credential-brokering flow
        # that does not exist yet — and a state nothing can resolve is worse than a pause.
        return WorkerEvent(
            kind=ProgressKind.INPUT_REQUEST,
            run_id=run_id,
            message=detail or "the agent needs credentials before it can continue",
            external_task_id=update.task_id,
            external_refs=common,
        )

    if state == _UNSPECIFIED:
        detail = detail or "the agent sent a status update with no state"

    return WorkerEvent(
        kind=ProgressKind.STATUS,
        run_id=run_id,
        message=detail or TaskState.Name(state),
        external_task_id=update.task_id,
        external_refs=common,
    )


CONTENT_HASH_REF = "content_sha256"
"""Where the completed artifact's content hash travels.

`RunExecutor.collect_output` discriminates on it — the same bytes recorded twice is one
version — so an artifact cannot be ingested canonically without one.

The value is `paos_domain.content_address`, not a bare hex digest. It is the dedupe key, and
two encodings of the same bytes are two artifacts that should have been one — with nothing
raising, because both are perfectly good strings.
"""


def artifact_bytes(artifact: Artifact) -> bytes:
    """The artifact's content, for hashing.

    Text and raw parts only. A `url` part is a pointer to bytes this process has not seen, and
    hashing the pointer would produce a stable-looking identity for content that can change
    underneath it — worse than admitting the content is not in hand.
    """
    chunks: list[bytes] = []
    for part in artifact.parts:
        if part.HasField("text"):
            chunks.append(part.text.encode())
        elif part.HasField("raw"):
            chunks.append(bytes(part.raw))
    return b"".join(chunks)


def artifact_event(
    run_id: RunId,
    update: TaskArtifactUpdateEvent,
    *,
    accumulated: bytes = b"",
) -> WorkerEvent:
    """One A2A artifact update as a `WorkerEvent` carrying a canonical id.

    Chunked artifacts arrive as several updates under one native id; each maps to the same
    canonical id, so the kernel sees repeated news about one artifact rather than several.

    The content hash appears only on the final chunk. A partial artifact has no final identity,
    and hashing a prefix would produce an id that changes as the rest arrives — which is exactly
    the collision `collect_output`'s content-hash discriminator is there to prevent.
    """
    native = update.artifact.artifact_id
    refs = {
        "a2a_artifact_id": native,
        "a2a_append": str(update.append).lower(),
        "a2a_last_chunk": str(update.last_chunk).lower(),
    }
    if update.context_id:
        refs["a2a_context_id"] = update.context_id
    if update.last_chunk:
        body = accumulated or artifact_bytes(update.artifact)
        refs[CONTENT_HASH_REF] = content_address(body)
        refs["content_bytes"] = str(len(body))
    return WorkerEvent(
        kind=ProgressKind.ARTIFACT,
        run_id=run_id,
        message=_artifact_text(update.artifact),
        artifact_id=canonical_artifact_id(run_id, native),
        external_task_id=update.task_id,
        external_refs=refs,
    )


SYNTHESISED_FINAL_REF = "final_chunk_synthesised"
"""Marks an artifact PAOS finalised itself because the peer never marked a last chunk.

Recorded rather than hidden: the content hash is real, but the *decision* that the artifact was
complete was PAOS's, inferred from a successful terminal state. Anyone auditing where a canonical
artifact came from should be able to see that.
"""


def finalised_artifact_event(
    run_id: RunId,
    *,
    native_artifact_id: str,
    body: bytes,
    external_task_id: str = "",
    context_id: str = "",
) -> WorkerEvent:
    """A closing artifact event for a peer that streamed content but never marked a final chunk.

    A2A leaves `lastChunk` optional and some servers simply never set it — Hermes builds its
    `artifactUpdate` without the field at all, so it arrives as protobuf's default `false`. Since
    `artifact_event` withholds the content hash until the final chunk, and `worker_gateway.pump`
    ingests only artifacts that *have* a hash, such a peer's output is never recorded: the run
    succeeds and PAOS stores nothing.

    This closes that gap without weakening the rule it depends on. The caller decides *when* a
    synthesised finalisation is legitimate — see `A2AWorkerAdapter._settle_artifacts`, which does
    it only on a successful terminal — because terminality alone does not mean completeness: a
    cancelled run also has bytes and also never marked them final, and promoting those would turn
    abandoned partial output into a canonical deliverable.

    Stateless, like the rest of this module: the accumulated bytes are passed in.
    """
    refs = {
        "a2a_artifact_id": native_artifact_id,
        "a2a_last_chunk": "true",
        SYNTHESISED_FINAL_REF: "true",
        CONTENT_HASH_REF: content_address(body),
        "content_bytes": str(len(body)),
    }
    if context_id:
        refs["a2a_context_id"] = context_id
    return WorkerEvent(
        kind=ProgressKind.ARTIFACT,
        run_id=run_id,
        message=native_artifact_id or "artifact",
        artifact_id=canonical_artifact_id(run_id, native_artifact_id),
        external_task_id=external_task_id,
        external_refs=refs,
    )


def message_event(run_id: RunId, message: Message) -> WorkerEvent:
    refs: dict[str, str] = {}
    if message.context_id:
        refs["a2a_context_id"] = message.context_id
    if message.message_id:
        refs["a2a_message_id"] = message.message_id
    return WorkerEvent(
        kind=ProgressKind.LOG,
        run_id=run_id,
        message=_text_of(message),
        external_task_id=message.task_id,
        external_refs=refs,
    )


TASK_SNAPSHOT_REF = "a2a_task_snapshot"


def task_snapshot_events(run_id: RunId, task: Task) -> list[WorkerEvent]:
    """A whole `Task` as events.

    A snapshot can carry artifact bytes written while the gateway was disconnected. A
    working task can contain a partial artifact. Only a completed task supplies final hashes.
    """
    events: list[WorkerEvent] = []
    refs = {
        "a2a_task_state": TaskState.Name(task.status.state),
        TASK_SNAPSHOT_REF: "true",
    }
    if task.context_id:
        refs["a2a_context_id"] = task.context_id

    events.append(
        WorkerEvent(
            kind=ProgressKind.STATUS,
            run_id=run_id,
            message=_text_of(task.status.message) or f"attached to {task.id}",
            external_task_id=task.id,
            external_refs=refs,
        )
    )
    for artifact in task.artifacts:
        artifact_refs = refs | {"a2a_artifact_id": artifact.artifact_id}
        if task.status.state == _COMPLETED:
            artifact_refs[CONTENT_HASH_REF] = content_address(artifact_bytes(artifact))
        events.append(
            WorkerEvent(
                kind=ProgressKind.ARTIFACT,
                run_id=run_id,
                message=_artifact_text(artifact),
                artifact_id=canonical_artifact_id(run_id, artifact.artifact_id),
                external_task_id=task.id,
                external_refs=artifact_refs,
            )
        )
    if is_terminal(task.status.state):
        events.append(
            WorkerEvent(
                kind=ProgressKind.TERMINAL,
                run_id=run_id,
                message=_text_of(task.status.message),
                terminal_status=verdict_for(task.status.state),
                external_task_id=task.id,
                external_refs=refs,
            )
        )
    return events


def stream_events(
    run_id: RunId, response: StreamResponse, *, accumulated: bytes = b""
) -> list[WorkerEvent]:
    """Translate one `StreamResponse` — the union the SDK actually yields.

    Returns a list because a task snapshot is several events; the caller flattens.
    """
    payload = response.WhichOneof("payload")
    if payload == "status_update":
        return [status_event(run_id, response.status_update)]
    if payload == "artifact_update":
        return [artifact_event(run_id, response.artifact_update, accumulated=accumulated)]
    if payload == "message":
        return [message_event(run_id, response.message)]
    if payload == "task":
        return task_snapshot_events(run_id, response.task)
    # An empty oneof is a frame this SDK version knows and this adapter does not. Dropping it
    # silently would hide a protocol upgrade; a LOG line keeps the run moving and leaves a mark.
    return [
        WorkerEvent(
            kind=ProgressKind.LOG,
            run_id=run_id,
            message=f"unrecognized A2A frame: {payload or 'empty'}",
        )
    ]


def context_id_of(events: list[WorkerEvent]) -> str:
    """The A2A context id seen so far, which is the handle 'resume by context' needs."""
    for event in events:
        found = event.external_refs.get("a2a_context_id")
        if found:
            return found
    return ""


__all__ = [
    "ARTIFACT_NAMESPACE",
    "CONTENT_HASH_REF",
    "SELECTION_REQUEST_KEY",
    "SYNTHESISED_FINAL_REF",
    "TASK_SNAPSHOT_REF",
    "TERMINAL_A2A_STATES",
    "artifact_bytes",
    "artifact_event",
    "canonical_artifact_id",
    "context_id_of",
    "finalised_artifact_event",
    "is_terminal",
    "message_event",
    "status_event",
    "stream_events",
    "task_snapshot_events",
    "verdict_for",
]
