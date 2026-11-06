"""Provider-neutral interfaces — roadmap Phase 2.3, scaffold contracts/.

These are the seams that let a provider be replaced without migrating a single canonical
record. Every one of them is defined here, in the domain package, so that an adapter depends
on the contract rather than the contract growing to fit an adapter.

`RuntimeDriver` and `WorkerAdapter` are `Protocol`s rather than base classes: an adapter
satisfies them structurally, and the type checker verifies conformance without the adapter
importing a framework.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from paos_domain.base import PaosModel, RiskClass
from paos_domain.effect import Effect
from paos_domain.execution import ExecutionProvider, LifecycleState, RuntimeLease, SandboxProfile
from paos_domain.ids import ArtifactId, EffectId, RunId, RuntimeId, TaskId
from paos_domain.task import TaskState
from paos_domain.workspace import LeaseMode, RepositoryView, WorkspaceKind, WorkspaceLease


class HealthReport(PaosModel):
    state: LifecycleState
    detail: str = ""
    ready: bool = False


class RuntimeRequest(PaosModel):
    """What the runtime manager asks a provider for.

    Deliberately contains no provider vocabulary: an adapter translates this into an E2B
    sandbox spec, a Docker run, a Kubernetes Job or an SSH invocation.
    """

    run_id: RunId
    worker_id: str
    template: str | None = None
    template_version: str | None = None
    sandbox_profile: SandboxProfile | None = None
    workspace_lease: WorkspaceLease | None = None
    credential_grant_ids: list[str] = Field(default_factory=list[str])

    service_command: str | None = Field(
        default=None,
        description=(
            "Which agent the in-runtime shim should launch, from the worker's record.\n\n"
            "Carried on the request rather than read from config by the driver, for the reason "
            "this whole model exists: an adapter translating a request must not also be the thing "
            "that decides what the request meant."
        ),
    )

    service_args: str = Field(
        default="",
        description="Whitespace-separated arguments for `service_command`. Empty is normal.",
    )

    task_id: str = ""
    task_mode: str = "NORMAL"
    capability_envelope: list[str] = Field(
        default_factory=list[str],
        description=(
            "The calling task's capability envelope, carried so the runtime's tool arrival can be "
            "published with it.\n\n"
            "**On the request rather than looked up by the driver**, for the reason this whole "
            "model exists: an adapter translating a request must not also be the thing that "
            "decides what the request meant. The driver is the only component that knows the "
            "broker's address on a runtime network, and the kernel is the only one that knows the "
            "envelope — so the envelope travels here rather than the address travelling back.\n\n"
            "`paos.tools` intersects a tool call against this, so an empty envelope authorizes no "
            "tool at all. That is the right default for a runtime whose task declared nothing."
        ),
    )

    persistent_paths: list[str] = Field(
        default_factory=list[str],
        description=(
            "Container paths whose contents must survive this runtime, backed by a worker-scoped "
            "volume rather than the runtime-scoped one.\n\n"
            "The worker adapter declares the paths required to keep worker state across "
            "container recreation. The sandbox profile determines whether they are available."
        ),
    )

    lease_duration: timedelta = timedelta(minutes=30)


@runtime_checkable
class RuntimeDriver(Protocol):
    """Lifecycle interface every ExecutionProvider adapter implements.

    Matches the scaffold's `contracts/runtime_driver.md`: probe, install, start, health,
    acquire_lease, heartbeat, cancel, stop, destroy. Provider-native allocation ids are
    returned as external metadata on the lease, never as canonical ids.
    """

    @property
    def provider(self) -> ExecutionProvider: ...

    async def probe(self) -> HealthReport:
        """Is this provider reachable and usable right now?"""
        ...

    async def install(self, template: str, version: str | None = None) -> None: ...

    async def start(self, request: RuntimeRequest) -> RuntimeId: ...

    async def health(self, runtime_id: RuntimeId) -> HealthReport: ...

    async def acquire_lease(
        self, request: RuntimeRequest, runtime_id: RuntimeId
    ) -> RuntimeLease: ...

    async def heartbeat(self, lease: RuntimeLease) -> RuntimeLease: ...

    async def cancel(self, runtime_id: RuntimeId, *, reason: str) -> None: ...

    async def stop(self, runtime_id: RuntimeId) -> None:
        """Put a runtime to sleep: the container stops, its identity survives.

        **Sleep, not termination**, and pairing it with `resume` is what makes that distinction
        real. `destroy` removes the container and the runtime-scoped volume; this keeps both,
        which is why a slept runtime can be woken and a destroyed one cannot.
        """
        ...

    async def resume(self, runtime_id: RuntimeId) -> None:
        """Wake a slept runtime — the *same* container, started again.

        Deliberately not `restart_runtime`, which takes a `run_id` and exists for "the process
        must re-read a file it already has". Those are different lifecycle events, and giving them
        one name would put two meanings on one call.

        The container ID must survive to preserve runtime admission.
        """
        ...

    async def destroy(self, runtime_id: RuntimeId) -> None: ...


class WorkspaceRequest(PaosModel):
    """What a run asks the workspace coordinator for.

    Provider-vocabulary-free for the same reason `RuntimeRequest` is: `repository_id` names an
    entry in `configs/workspaces/repository.yaml`, never a path, so the same request shape works for
    a local worktree, an isolated remote clone, or a browser profile in Phase 17.
    """

    run_id: RunId
    repository_id: str
    mode: LeaseMode = LeaseMode.MUTABLE
    lease_duration: timedelta = timedelta(hours=2)


class AllocatedWorkspace(PaosModel):
    """What the allocator produced, before any of it is written down.

    `view_uri` set means **PAOS created this view and must remove it** — the exact analogue of
    `RuntimeLease.external_ids` being non-empty, and it is what tells the release path whether
    there is anything to tear down. A read-only lease over a shared checkout records occupancy and
    creates nothing.
    """

    workspace_id: str
    view_uri: str | None = None
    repository: RepositoryView | None = None
    expires_at: datetime | None = None


class WorkspaceCleanupState(StrEnum):
    """Whether the *filesystem* still has something to remove.

    **Deliberately not derivable from the lease.** `released_at` says the mutable claim is
    surrendered and another run may allocate; it says nothing about whether a directory still
    exists, and inferring one from the other is the mistake this enum removes. A worktree whose
    removal was refused leaves a released lease and a `DEFERRED` cleanup, and the orphan sweep finds
    it by cleanup state rather than by lease state.
    """

    PENDING = "pending"
    DONE = "done"
    DEFERRED = "deferred"


class ReleaseOutcome(PaosModel):
    """The result of giving a workspace back.

    Two independent facts, which is the whole point: `lease_released` is a claim about the row that
    unblocks the repository, `cleanup` is a claim about the filesystem. A release that could not
    remove the directory still released the lease — holding it would let one stuck worktree brick
    every future run on that repository through the partial unique index.
    """

    lease_released: bool = True
    cleanup: WorkspaceCleanupState = WorkspaceCleanupState.DONE
    cleanup_reason: str = ""


@runtime_checkable
class WorkspaceAllocator(Protocol):
    """How PAOS gives one run an isolated, mutable view of a repository — roadmap 14.1A.

    *"Every mutating coding Run gets its own branch/worktree or isolated remote clone rooted at a
    recorded base commit. The ACP bridge receives the leased workspace; it never decides the shared
    checkout itself."*

    A Protocol rather than a class because master spec §4.9 names five workspace kinds and Phase 17
    needs `browser_profile`. It costs one definition now and prevents a second code path later.

    **Narrower than `RuntimeDriver`, and the omission with the most reasoning behind it is
    `heartbeat`.** The two failure modes are inverted. A container *dies silently*, so a lease can
    name an occupancy that is already false, and `RuntimeLease.last_heartbeat_at` is how "is it
    still there" gets asked. A worktree does not die — it *leaks*: the directory stays, the lease
    stays unreleased, and the partial unique index then refuses every later run on that repository.
    Heartbeating "the directory still exists" is trivially true and proves nothing about the process
    that was supposed to be using it. Leakage is answered by `expires_at`, a reaper armed at
    allocation, and an orphan sweep — not by a pulse.
    """

    @property
    def kind(self) -> WorkspaceKind: ...

    async def probe(self) -> HealthReport:
        """Is this allocator usable right now — tooling present, roots configured?"""
        ...

    async def allocate(self, request: WorkspaceRequest) -> AllocatedWorkspace: ...

    async def release(
        self, allocated: AllocatedWorkspace, *, discard: bool = True
    ) -> ReleaseOutcome:
        """Give the view back. `discard=False` keeps the directory for inspection after a failure.

        Must be idempotent: the finishing run, the reaper and the orphan sweep can all reach the
        same workspace, exactly as they can reach the same runtime.
        """
        ...

    async def inspect(self, allocated: AllocatedWorkspace) -> HealthReport: ...


class ProgressKind(StrEnum):
    LOG = "log"
    STATUS = "status"
    ARTIFACT = "artifact"
    EFFECT_PROPOSAL = "effect_proposal"
    INPUT_REQUEST = "input_request"
    APPROVAL_REQUEST = "approval_request"
    TERMINAL = "terminal"


SECRET_ARGUMENT_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "private_key",
)
"""Argument names whose *values* never enter a canonical record.

`Effect.payload_preview` has always said "never the secret", but saying it is not enforcing it:
until now every payload was built by PAOS itself, so the rule held by construction. A worker's
tool arguments are the first payload the kernel does not author, and a `run_shell` command line
carrying an API key would otherwise be written verbatim into an Effect row, an approval card and
a Telegram message.

Matched as a substring against the lowercased argument name, so `--auth-token` and `GITHUB_TOKEN`
are both caught. Deliberately over-broad: redacting one argument too many costs an operator a
question, and redacting one too few is unrecoverable once the row is written.
"""

REDACTED = "[redacted]"

ARGUMENT_VALUE_BUDGET = 2_000
"""How much of one argument survives into the record.

A `write_file` proposal carries the whole file, and an approval card is read on a phone. The
value is truncated rather than dropped so the human still sees what kind of thing it is.
"""


def redact_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    """A worker's tool arguments, safe to persist and safe to show a human.

    Values become strings here rather than at the database boundary because this is the last
    point that knows they came from a worker. Everything downstream — the Effect row, the OPA
    input, the approval card — treats them as ordinary canonical data.
    """
    safe: dict[str, str] = {}
    for name, value in arguments.items():
        key = str(name)
        if any(hint in key.lower() for hint in SECRET_ARGUMENT_HINTS):
            safe[key] = REDACTED
            continue
        rendered = value if isinstance(value, str) else str(value)
        if len(rendered) > ARGUMENT_VALUE_BUDGET:
            rendered = (
                rendered[:ARGUMENT_VALUE_BUDGET]
                + f"… (+{len(rendered) - ARGUMENT_VALUE_BUDGET} chars)"
            )
        safe[key] = rendered
    return safe


class EffectProposal(PaosModel):
    """A consequential action a worker wants to take, and is blocked until PAOS answers.

    **This is the type that makes `ProgressKind.EFFECT_PROPOSAL` mean something.** The enum
    member has existed since Phase 2 and named nothing: an adapter could report that a proposal
    happened, but had no way to say what was proposed, so the kernel logged a line and the
    worker waited forever. Everything a policy engine or a human needs to decide has to cross
    here, because after this point the worker is not asked again.

    **`proposal_key` is the identity, not the effect id.** PAOS mints the Effect; the worker
    cannot know its id, and a stream that reconnects will replay the same parked decision. The
    key is derived by the adapter from facts that survive a reconnect — the run and the worker's
    own handle for the decision — so a replayed proposal resolves to the Effect that already
    exists rather than a second independently-approvable one.

    **`decision_ref` is how the answer gets back.** A worker that parks is waiting on its own
    internal handle, and PAOS must be able to release exactly that one, from a different process,
    possibly hours later. It is provider-native by nature and stays out of business identity
    (roadmap 2.3), which is why it lives here rather than becoming the Effect's id.
    """

    worker_id: str
    tool: str = Field(description="The worker's own name for the action, e.g. 'write_file'")
    action: str = Field(description="The canonical Effect action this maps to")
    capability: str
    resource: str = Field(description="What is being acted on — a path, an address, a URL")
    risk: RiskClass

    arguments: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Normalized and redacted; build it with `redact_arguments`.",
    )
    summary: str = Field(default="", description="One line a human can decide from")

    payload_hash: str = ""
    proposal_key: str = Field(min_length=1)
    decision_ref: str = Field(min_length=1)
    session_ref: str = ""
    sequence: int = 0


class SelectionOption(PaosModel):
    """One thing a worker is offering the operator. An id, a label, a sentence — nothing else.

    **Deliberately the same shape as the client registry's `Action`**, and for the registry's own
    reason: an option carrying a URL or a callback is a capability handed to a surface PAOS did not
    write; an option carrying an id is a request the server resolves. The worker decides what each
    id *means*. The browser only ever names one of them back.
    """

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    detail: str = Field(default="", max_length=400)


class SelectionRequest(PaosModel):
    """A worker asking the operator to choose — roadmap Phase 25.

    The operator supplies choices through the PAOS control surface. A2A INPUT_REQUIRED
    conveys a message and has no structured options.

    So this rides metadata on an ordinary WORKING update, exactly as `EffectProposal` does and for
    the same stated reason, and the shim turns it into a `ProgressKind.INPUT_REQUEST`.
    """

    prompt: str = Field(min_length=1, max_length=2000)
    options: list[SelectionOption] = Field(min_length=1, max_length=10)
    """At least one, because a choice with no options is a dead end; at most ten, because this is
    read on a phone and a list nobody can hold in their head is not a question."""

    decision_ref: str = Field(default="", max_length=200)
    """The worker's own handle for the park, so the answer can be delivered back to it — the same
    role `EffectProposal.decision_ref` plays for an authorization."""


class WorkerEvent(PaosModel):
    """Normalized worker output.

    Every adapter converts its protocol's native events into these, so the orchestrator never
    learns A2A's or ACP's vocabulary. Artifacts and effects cross as canonical refs, which is
    the rule in roadmap 2.3 ("do not allow provider-native file ids to become business ids").
    """

    kind: ProgressKind
    run_id: RunId
    message: str = ""
    artifact_id: ArtifactId | None = None
    effect_id: EffectId | None = None
    effect_proposal: EffectProposal | None = None
    """Set on an `EFFECT_PROPOSAL` event. Distinct from `effect_id`, which names an Effect that
    already exists — a proposal is the request to create one, so it cannot carry an id yet."""
    selection_request: SelectionRequest | None = None
    """Set on an `INPUT_REQUEST` event that carries a *choice* rather than a free-text prompt.

    Optional because `INPUT_REQUEST` predates this: a third-party A2A agent reporting
    `INPUT_REQUIRED` still produces one of these events with no selection, and that stays a prompt
    with no options rather than being promoted into a choice nobody offered."""
    terminal_status: TaskState | None = None
    """The verdict on a `TERMINAL` event, in the domain's own vocabulary.

    Typed rather than free-form on purpose. It was a plain `str` compared against the lowercase
    literal `"succeeded"`, so an adapter that reported success with any other spelling had the
    run recorded as FAILED — silently, because both spellings are perfectly good strings. A
    worker cannot be trusted to guess an undocumented casing convention, so the type states it
    and a wrong value is rejected at the boundary instead of inverting the outcome.
    """
    external_task_id: str = ""
    """The worker's own handle for this run, if its protocol has one.

    Its own field rather than a key in `external_refs`: the kernel journals this exactly once to
    avoid re-dispatching after a restart, so it must be findable without knowing which protocol
    produced the event. `external_refs` stays the free-form bag of provider-native ids that
    roadmap 2.3 keeps *out* of business identity.
    """
    usage: dict[str, Any] = Field(default_factory=dict[str, Any])
    external_refs: dict[str, str] = Field(default_factory=dict[str, str])


class ContextPolicy(StrEnum):
    """How much of a conversation may be projected to a worker — `Conversation.context_policy`.

    **A policy names a compaction strategy, never a volume.** There is deliberately no value
    meaning "the whole thread", and adding one would be the wrong shape of change: the way to give
    a worker more useful history is a better *selection*, not a higher limit. Roadmap 11.4's
    "Hermes does not receive the whole memory database by default" is a statement about this
    vocabulary, not about a number.

    `PROJECT` is the schema default and, in this build, the structural minimum: task titles and
    states, never message bodies. That is a floor to build on rather than the intended end state —
    a relevance-selected, model-compacted policy belongs with the memory projection service
    (master spec §E, Phase 19), which is the phase that has something to select *from*.

    The reason the minimum is structural rather than a length cap: a field that cannot contain a
    message body cannot grow into one, whereas "the last N turns" is a limit somebody raises.
    """

    NONE = "none"
    """Nothing beyond the task itself. For a conversation whose history must not travel."""

    PROJECT = "project"
    """Project-scoped, compacted to this build's minimum: sibling task titles and states."""

    ACCEPTED_CLAIMS = "accepted_claims"
    """`PROJECT`, plus the canonical claims PAOS currently believes about this project.

    **Named for the selection, not the source.** `"memory"` would name where the facts come from
    and so imply "all of it"; this names the filter — *accepted* claims, believed now, valid now,
    within the task's ceiling. A superseded claim is not in it, a proposed one is not in it, and a
    claim above the ceiling is not in it.

    It is a strategy rather than a volume in the sense this enum requires: adding facts to a
    projection is a *narrower* statement than adding turns, because every claim in it passed
    governance and carries the event that stated it.
    """


class ProjectContext(PaosModel):
    """What a worker is told about the world around its task.

    Bounded by construction rather than by trimming: every field here is a name, a state or an
    id, so there is no field through which a conversation body or an artifact's contents could
    arrive.

    **Phase 19 filled this in, and kept the bound.** `known_facts` holds governed memory — claims
    that passed acceptance, are still believed, are valid now and sit within the task's ceiling —
    rendered as subject/predicate/object triples. It is a third list of short strings, not a new
    kind of container: `MemoryClaim.claim_text` is deliberately absent, because it is the one
    field on a claim that could carry a message body.

    **H3 added nothing here, having tried to.** A `retrieval_decision_id` was written and taken
    back out: a third-party agent's model never sees message metadata — only `render_invocation`'s
    body reaches it — and `memory.retrieve`, the capability that would let a worker ask, is
    declared in the taxonomy and wired to nothing. A pointer no reader can follow is the
    declared-with-no-reader pattern, added to the one container shaped to refuse additions. The
    operator's route to the same answer is `GET /memory/retrieval/for-task/{task_id}`, which needs
    no field here because the decision already records the task.
    """

    project_name: str = ""
    project_summary: str = ""
    conversation_id: str | None = None
    policy: ContextPolicy = ContextPolicy.PROJECT

    recent_tasks: list[str] = Field(
        default_factory=list[str],
        description="Sibling tasks in this conversation as 'title (STATE)'. Titles, never bodies.",
    )

    known_facts: list[str] = Field(
        default_factory=list[str],
        description=(
            "Accepted claims about this project, as 'subject predicate object'. Rendered from "
            "those three fields only — never `MemoryClaim.claim_text`, which is the one field "
            "that could hold a message body, and so the one that would turn this container into "
            "the thing `recent_tasks` was shaped to prevent."
        ),
    )
    data_ceiling: str = ""

    truncated: bool = Field(
        default=False,
        description=(
            "Something was left out. A field rather than a silent cut: a projection that quietly "
            "drops the item that mattered is worse than one that says it dropped something, and "
            "the reader of a worker's confused answer needs to know which they are looking at."
        ),
    )


class EffectEnvelope(PaosModel):
    """What the worker may **propose**. Advisory, and it must not read as a permission.

    Two surfaces are easy to confuse here. Effects a worker *asks PAOS for* are decided by OPA at
    commit time — this envelope only tells the worker in advance what will be entertained, saving
    a round trip. Tools the worker runs *itself* are bounded inside the worker's own
    configuration, are enforced by `kernel/readiness.py` before it is routable at all, and never
    appear in this message: sending them would be describing a boundary to the thing it constrains.

    Nothing the worker does with this field is trusted. It is a hint, and treating it as a
    guarantee would mean the governed party held the governing record.
    """

    proposable_capabilities: list[str] = Field(default_factory=list[str])
    data_ceiling: str = ""
    dry_run: bool = False


class OutputContract(PaosModel):
    """What shape the answer should take — from `Task.desired_output_spec`.

    Advisory in the same way: what actually arrives is validated on receipt, and richer validation
    is the Artifact Service's (Phase 18). This is the first consumer of `desired_output_spec`,
    which has been plumbed through the repository and read by nothing.
    """

    expects_artifact: bool = False
    artifact_kind: str = ""
    mime_type: str = ""
    notes: str = ""

    def is_empty(self) -> bool:
        """No contract stated. Renders as no section at all, rather than an empty heading."""
        return not (self.expects_artifact or self.artifact_kind or self.mime_type or self.notes)


class WorkerInvocation(PaosModel):
    """The task projection handed to a worker — scaffold `contracts/worker_adapter.md`.

    A projection, not the Task itself: a worker receives what it needs and no more, and can
    never mutate canonical state directly.
    """

    run_id: RunId
    task_id: TaskId
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list[str])
    input_artifact_ids: list[ArtifactId] = Field(default_factory=list[ArtifactId])
    workspace_lease: WorkspaceLease | None = None
    credential_grant_ids: list[str] = Field(default_factory=list[str])
    trace_id: str | None = None
    dry_run: bool = False

    # Roadmap 11.4's remaining three. Optional so a worker can be dispatched without them — a
    # projection that could fail a dispatch would make context a reliability risk rather than an
    # improvement.
    project_context: ProjectContext | None = None
    effect_envelope: EffectEnvelope | None = None
    output_contract: OutputContract | None = None


@runtime_checkable
class WorkerAdapter(Protocol):
    """Boundary every worker sits behind, whatever protocol it speaks."""

    @property
    def worker_id(self) -> str: ...

    async def invoke(self, invocation: WorkerInvocation) -> AsyncIterator[WorkerEvent]: ...

    async def cancel(self, run_id: RunId, *, reason: str) -> None: ...


@runtime_checkable
class WorkerDecisionResolver(Protocol):
    """How PAOS releases a worker that parked on a consequential action.

    Separate from `WorkerAdapter` on purpose, and the separation is the whole point rather than
    tidiness. `WorkerAdapter.invoke` returns a live stream, so an adapter instance belongs to the
    process consuming it; a decision may be answered six hours later, by a durable handler in a
    different process, after the one that dispatched the run has died and been replaced. A
    resolver is therefore built from configuration, not held from a dispatch, and is looked up by
    worker id at the moment the answer exists.

    Building the answer into the pump instead would be the natural mistake and a quiet one: it
    works in every test where the approval is instant, and orphans the worker forever the first
    time a human takes a lunch break.
    """

    @property
    def worker_id(self) -> str: ...

    async def release(self, decision_ref: str, *, approved: bool) -> bool:
        """Tell the worker to proceed exactly once, or to refuse.

        Returns whether *this* call was the one that resolved it. `False` means somebody else
        got there first, which is information the caller needs: a decision PAOS did not make is
        the failure this whole path exists to prevent.
        """
        ...

    async def pending(self, decision_ref: str) -> bool | None:
        """Is the decision still parked?

        The postcondition probe for a mediated effect, and tri-state for the same reason
        `VerificationStatus` is: `True` still waiting, `False` already resolved, `None` could not
        tell. `None` must never be spelled as either of the others — an unreachable worker looks
        exactly like a resolved one to a boolean, and guessing wrong authorizes twice.
        """
        ...


class DispatchEnvelope(PaosModel):
    """What a worker pool consumes.

    Pools consume envelopes, never raw database rows (roadmap 2.3). That indirection is what
    lets the backing queue move from Postgres to NATS later without touching a pool.
    """

    run_id: RunId
    task_id: TaskId
    worker_id: str
    priority: int = Field(default=50, ge=0, le=100)
    idempotency_key: str
    enqueued_at_ms: int
    attempt: int = Field(default=1, ge=1)


@runtime_checkable
class DispatchQueue(Protocol):
    """Kernel dispatch interface. Restate/Postgres-backed initially."""

    async def enqueue(self, envelope: DispatchEnvelope) -> None: ...

    async def claim(self, worker_id: str, *, limit: int = 1) -> list[DispatchEnvelope]: ...

    async def ack(self, envelope: DispatchEnvelope) -> None: ...

    async def nack(self, envelope: DispatchEnvelope, *, reason: str) -> None: ...

    async def depth(self, worker_id: str) -> int: ...


class VerificationStatus(StrEnum):
    """Did the external write actually land?

    **Tri-state on purpose.** A boolean would be the bug: "I could not tell" must not be
    spelled `False`, or an uncertain recovery silently repeats the write. Master spec §5.3
    requires the uncertain case to stop and ask a human instead.
    """

    HELD = "held"
    """The postcondition holds — a previous attempt already committed. Adopt its receipt."""

    NOT_HELD = "not_held"
    """The postcondition does not hold. Safe to commit."""

    UNKNOWN = "unknown"
    """Could not determine. Never retry on this — escalate for reconciliation."""


class EffectContestedError(RuntimeError):
    """Another actor resolved this effect's external decision before PAOS did.

    Terminal by nature: retrying cannot un-answer a question somebody else already answered,
    and the whole point of the mediated path is that PAOS is the only answer. Raised rather
    than folded into a receipt because a contested effect is not a successful one — the write
    may well have happened, but not because PAOS authorized it, and that distinction is the
    entire audit trail.

    Reaching this in practice means a preflight assumption broke: a bound channel, a second
    operator surface, a GUI somebody left open. It is worth an alarm, not a retry.
    """


class EffectReceipt(PaosModel):
    """Provider evidence that the write happened.

    An Effect may not reach COMMITTED without one: "we think it worked" is not a receipt, and
    reconciliation months later needs the provider's own identifier.
    """

    provider: str
    external_id: str | None = None
    committed_at: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict[str, Any])


class EffectVerification(PaosModel):
    status: VerificationStatus
    receipt: EffectReceipt | None = None
    detail: str = ""


@runtime_checkable
class EffectAdapter(Protocol):
    """The boundary for one class of external write — master spec §5.3.

    This is the one place `durable_step` cannot protect: its transactional trick works because
    a rollback un-writes a row, and a rollback does not un-send an email. So the adapter
    carries the substitute protocol itself — probe the postcondition *before* committing, and
    treat "unknown" as a stop condition rather than a retry.
    """

    @property
    def action(self) -> str:
        """The Effect action this adapter handles, e.g. 'send_email'."""
        ...

    async def preview(self, effect: Effect) -> dict[str, Any]:
        """Human-readable diff for the approval card. Must not mutate anything."""
        ...

    async def commit(self, effect: Effect, *, idempotency_key: str) -> EffectReceipt:
        """Perform the write, passing the provider its own idempotency key where supported."""
        ...

    async def verify(self, effect: Effect) -> EffectVerification:
        """Probe whether the write has already landed. Called before commit and after."""
        ...
