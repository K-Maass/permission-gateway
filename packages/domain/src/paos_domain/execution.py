"""Execution plane — master spec §4.9, roadmap Phase 2.3.

The single most important separation in the system:

    Worker            logical provider of capabilities        "local-worker"
    Runtime           one running instance of a worker        container worker-3
    Machine           a host                                  a workstation, a cloud sandbox
    ExecutionProvider where a runtime can be provisioned      local-docker, e2b
    WorkerPool        worker x provider + autoscaling         summarizer-local
    RuntimeLease      time-bounded allocation to one Run      lease-01

Collapsing any two of these is the mistake that makes cloud execution a rewrite instead of an
adapter. These objects exist from the first migration even though the only provider today is
local Docker — that is the whole point of "abstract early, integrate late".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from paos_domain.base import (
    Aggregate,
    DataClassification,
    Locality,
    Money,
    PaosModel,
    StateMachine,
    StateTransitionError,
    utc_now,
)
from paos_domain.ids import (
    CONFIG_NAME_PATTERN,
    CredentialGrantId,
    ExecutionProviderId,
    MachineId,
    RunId,
    RuntimeId,
    RuntimeLeaseId,
    WorkerId,
    WorkerPoolId,
    WorkspaceLeaseId,
)


class LifecycleState(StrEnum):
    """Master spec §4.9. Applies to a Runtime; a Worker is a definition and has no lifecycle."""

    INSTALLABLE = "INSTALLABLE"
    INSTALLED = "INSTALLED"
    SLEEPING = "SLEEPING"
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    OFFLINE = "OFFLINE"
    EXPERIMENTAL = "EXPERIMENTAL"


class WorkerKind(StrEnum):
    """How PAOS talks to the worker — the protocol boundary, not the vendor."""

    A2A = "a2a"
    A2A_ADAPTER = "a2a_adapter"
    ACP = "acp"
    CLI_ADAPTER = "cli_adapter"
    LIBRARY_ADAPTER = "library_adapter"
    OPENCLAW_DEVICE = "openclaw_device"
    INTERNAL = "internal"


class ProviderType(StrEnum):
    LOCAL_PROCESS = "local_process"
    DOCKER = "docker"
    SSH_MACHINE = "ssh_machine"
    E2B = "e2b"
    DAYTONA = "daytona"
    MODAL = "modal"
    KUBERNETES = "kubernetes"
    FAKE = "fake"  # conformance harness; see packages/testing


class ProviderStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BENCHMARK_REQUIRED = "BENCHMARK_REQUIRED"
    OPTIONAL = "OPTIONAL"
    DISABLED = "DISABLED"


class LockKind(StrEnum):
    """Validated kinds of resources that can be held exclusively."""

    WORKER = "worker"
    """A worker that may have only one run in flight."""

    WORKTREE = "worktree"
    """A Git worktree held by one run."""

    DESKTOP = "desktop"
    """A single-seat GUI session — Phase 17's subject. No writer yet."""

    BROWSER_PROFILE = "browser_profile"
    """A browser profile that cannot be driven twice at once. No writer yet."""

    FILE = "file"
    """A path held for exclusive write. No writer yet."""


class ProbeSource(StrEnum):
    """Who made an observation about a machine — roadmap §23, definition 7.

    **The load-bearing distinction in the whole health model.** `machine_agent.collect()` runs *on*
    the machine it describes, so its arrival proves the host is up and its silence proves nothing:
    the host is down, or the reporter crashed, or the network dropped. Only an observer that is
    still running when the subject is not can witness a failure.
    """

    SELF_REPORT = "self_report"
    """The machine reported about itself. Can prove UP; can never prove DOWN."""

    EXTERNAL = "external"
    """Something else probed the machine. The only source a DOWN verdict may come from."""


class ProbeOutcome(StrEnum):
    """What the observation saw. Paired with `ProbeSource`, never read alone."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Machine(Aggregate):
    """A host. Location, not identity — master spec §16."""

    machine_id: MachineId
    kind: str
    os: str | None = None
    arch: str | None = None
    trust_domain: str
    always_on: bool = False
    provisioned: bool = True
    network: str | None = None

    last_probe_at: datetime | None = None
    """When the last observation was made. **Not** `last_seen_at`, which says when we last heard
    from the host and is a different question: one timestamp answering both is how a liveness
    signal silently becomes a health claim."""

    last_probe_source: ProbeSource | None = None
    last_probe_outcome: ProbeOutcome | None = None

    host_capabilities: list[str] = Field(default_factory=list[str])
    services: list[str] = Field(default_factory=list[str])
    host_daemons: list[str] = Field(default_factory=list[str])

    cpu_count: int | None = Field(default=None, ge=1)
    memory_gb: float | None = Field(default=None, gt=0)
    gpu: str | None = None
    free_disk_gb: float | None = Field(default=None, ge=0)
    last_seen_at: datetime | None = None


class ExecutionProvider(Aggregate):
    """Where a runtime can be provisioned — master spec §4.9.

    The router only ever sees the normalized fields below. Provider mechanics (E2B sandbox
    ids, Kubernetes namespaces, SSH host details) live in the adapter, never here.
    """

    provider_id: ExecutionProviderId = Field(pattern=CONFIG_NAME_PATTERN)
    provider_type: ProviderType
    locality: Locality
    region: str | None = None
    jurisdiction: str | None = None

    trust_tier: str = "untrusted"
    data_classification_ceiling: DataClassification = DataClassification.PUBLIC

    supported_architectures: list[str] = Field(default_factory=list[str])
    runtime_templates: list[str] = Field(default_factory=list[str])
    egress_profile: str = "task_scoped"

    credential_broker_ref: str | None = None
    max_concurrent_runtimes: int | None = Field(default=None, ge=0)
    startup_latency_seconds: float | None = Field(default=None, ge=0)
    price_per_hour: Money | None = None

    status: ProviderStatus = ProviderStatus.BENCHMARK_REQUIRED
    healthy: bool = True
    provider_adapter: str = Field(
        description="Import path of the ProviderAdapter, e.g. adapters/execution/docker"
    )

    def permits(self, classification: DataClassification) -> bool:
        return self.data_classification_ceiling.permits(classification)


def _usable_url(value: str | None) -> str | None:
    """A URL that could actually be connected to, or `None`.

    Presence is not usability, and the difference is not pedantic here. `endpoint: "   "` is
    truthy, and `endpoint: "not-a-url"` passes straight through `endpoint_for`'s
    `removesuffix`/`rstrip` to fail much later at connect time with an error that names the
    transport rather than the config. This project has paid for that class of bug before:
    `base.supplied` exists in the policy bundle because `{"id": ""}` defeated `not input.a.b`,
    and `standing_grants` once shipped as strings where the bundle read objects.
    """
    if not value or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _usable_adapter(value: str | None) -> str | None:
    """A dotted module path a shim could plausibly be imported from, or `None`.

    Shape only — importability is not checked here, because the registry is loaded in processes
    that deliberately do not import adapter code (the control plane never pays for the protobuf
    runtime). A malformed path is still a config error worth catching early.
    """
    if not value or not value.strip():
        return None
    candidate = value.strip()
    parts = candidate.split(".")
    if not all(part.isidentifier() for part in parts):
        return None
    return candidate


def usable_dispatch_target(
    *,
    endpoint: str | None = None,
    agent_card_url: str | None = None,
    adapter: str | None = None,
) -> str | None:
    """The first declaration a run could actually be dispatched through, or `None`.

    One rule, imported by both the registry and `scripts/validate_configs.py`, because a second
    copy is how the two answers drift — and drift here means a worker that validates clean and
    cannot be reached, or the reverse.
    """
    return _usable_url(agent_card_url) or _usable_url(endpoint) or _usable_adapter(adapter)


class TrustClass(StrEnum):
    """How much a worker may do that PAOS did not enumerate — ordered, least first.

    **The ordering axis is unenumerated capability**, not danger in the abstract and not fitness
    for a task. Each step adds something the worker can do that no PAOS configuration lists:

        BOUNDED    nothing. Every action comes from a declared, attested tool envelope.
        SANDBOXED  arbitrary execution, confined — no egress, no host reach.
        GENERAL    arbitrary execution plus network reach.
        HOST       arbitrary execution plus reach into the host machine.

    Choosing that axis resolves what otherwise looks like a crossing. A bounded worker may well
    touch the network — Hermes has web tools — but it does so through operations PAOS listed and
    a runtime attestation confirmed. That is not the same kind of reach as a shell with a socket,
    and collapsing the two would make the ladder incoherent at its first rung.

    **This is a policy dimension, deliberately separate from `allowed_data_classes`.** They answer
    different questions and cross freely: `a0-private` is the *least* privileged Agent Zero
    profile (no egress at all) and simultaneously the one cleared for the *most* sensitive data,
    precisely because it cannot send anything anywhere. Folding either into the other would make
    one of those two facts inexpressible.

    **It is also separate from `sandbox_profile`.** The trust class is what routing consumes; the
    profile is what runtime enforcement consumes. Where both are declared,
    `scripts/validate_configs.py` checks they agree — a worker may not claim containment its
    profile does not deliver — but neither is derived from the other, so an externally-run worker
    can carry a trust class without a profile, and a profile can be tightened without silently
    re-ranking anything.

    Deliberately **not** a free-form integer. A rank invites arithmetic, and arithmetic on a scale
    nobody defined is how "3" comes to mean whatever the last person needed it to.
    """

    BOUNDED = "bounded"
    SANDBOXED = "sandboxed"
    GENERAL = "general"
    HOST = "host"

    @property
    def rank(self) -> int:
        """Position on the ladder. Compare with this, never by string."""
        return _TRUST_ORDER.index(self)

    def at_most(self, other: TrustClass) -> bool:
        return self.rank <= other.rank


_TRUST_ORDER: tuple[TrustClass, ...] = (
    TrustClass.BOUNDED,
    TrustClass.SANDBOXED,
    TrustClass.GENERAL,
    TrustClass.HOST,
)


class Worker(Aggregate):
    """A logical provider of capabilities. Carries no location and no lifecycle."""

    worker_id: WorkerId = Field(pattern=CONFIG_NAME_PATTERN)
    kind: WorkerKind
    role: str
    capabilities: list[str] = Field(default_factory=list[str])

    allowed_data_classes: list[DataClassification] = Field(
        default_factory=lambda: [DataClassification.PUBLIC]
    )
    effects_mediated: bool = Field(
        default=True,
        description="False is only legal for internal workers; every external worker "
        "proposes Effects rather than committing them (feature matrix rule 3).",
    )
    agent_card_url: str | None = None
    endpoint: str | None = None
    adapter: str | None = None

    trust_class: TrustClass = Field(
        default=TrustClass.GENERAL,
        description=(
            "How much this worker may do that PAOS did not enumerate. Routing prefers the least "
            "privileged worker that can serve a task, so this is the field that decides between "
            "two workers who can both do the job. Defaults to GENERAL: a worker that has not "
            "declared its containment is not assumed to have any."
        ),
    )

    a2a_auth: str | None = Field(
        default=None,
        description=(
            "How this worker's A2A credential is carried. `path_token` means it is part of the "
            "URL — Agent Zero's arrangement — so the endpoint must be assembled at dispatch from "
            "a secret rather than written into config. `None` means the ordinary header scheme "
            "the Agent Card declares."
        ),
    )

    agent_card_path: str | None = Field(
        default=None,
        description=(
            "Where this agent serves its A2A card, when it is not the SDK default of "
            "`/.well-known/agent-card.json`.\n\n"
            "The adapter uses the path declared for that agent version."
        ),
    )

    service_port: int = Field(
        default=80,
        ge=1,
        le=65535,
        description=(
            "The TCP port this worker's service listens on inside its runtime.\n\n"
            "The connector uses this declared port when forwarding to the signed worker."
        ),
    )

    service_prefix: str = Field(
        default="/a2a",
        description=(
            "Path prefix the worker's service is mounted under, or `/` for the root.\n\n"
            "Agent Zero serves A2A under `/a2a` and strips the prefix in its own middleware; the "
            "coding-plane shim serves at the root. Normalised to a leading slash and no trailing "
            "slash, and refused if it contains a traversal segment or an authority — a prefix that "
            "could climb out of its own path would let trusted metadata name a destination the "
            "worker record never intended."
        ),
    )

    service_command: str | None = Field(
        default=None,
        description=(
            "The executable the in-runtime shim launches for this worker, absolute.\n\n"
            "**Trusted routing metadata, like `service_port`.** The coding-plane image carries "
            "three agents and the shim can front any of them, so which one a runtime actually runs "
            "is a property of the worker — not a default baked into the image. It was exactly that "
            "default for a while, which meant an OpenCode runtime served an agent card reading "
            "`claude-code`: the wrong agent, announcing itself convincingly.\n\n"
            "`None` means the image's own default applies, which is right for a worker whose image "
            "serves one agent and only becomes wrong once an image serves several."
        ),
    )

    service_args: str = Field(
        default="",
        description=(
            "Whitespace-separated arguments for `service_command`. Empty is normal.\n\n"
            "OpenCode needs `acp` here: its ACP server is a subcommand rather than the default "
            "behaviour of its binary, so an empty value would launch its interactive TUI and the "
            "shim would wait forever for a protocol that was never going to start."
        ),
    )

    runtime_image: str | None = Field(
        default=None,
        description=(
            "Which `images:` entry in `configs/upstream.lock.yaml` this worker's runtime starts "
            "from.\n\n"
            "Names an approved image rather than an image reference: the digest lives in the lock "
            "file, which is the one place provenance is established. Defaults to the worker id, "
            "the convention for a worker with an image of its own — Agent Zero's three profiles "
            "are the same image behind different envelopes, so they name it explicitly."
        ),
    )

    sandbox_profile: str | None = Field(
        default=None,
        description=(
            "The `configs/execution/sandbox_profiles.yaml` profile this worker's runtime is "
            "started under. Naming one is, for now, also what marks the worker as having a "
            "PAOS-owned runtime lifecycle — see `Worker.runtime_managed`."
        ),
    )

    local_tool_envelope: list[str] = Field(
        default_factory=list[str],
        description=(
            "Tools the worker may run ITSELF, which never reach PAOS's Effect/OPA gateway. "
            "`effects_mediated` covers only the other surface — effects the worker asks PAOS "
            "for — so a worker that acts locally is bounded by this list and by nothing else. "
            "Declaring one is a containment claim, and a claim has to be proved: a worker that "
            "declares an envelope is not routable until a runtime attestation confirms the "
            "running process actually enforces it. Empty means no containment claim is made."
        ),
    )

    serialized_runs: bool = Field(
        default=False,
        description=(
            "May this worker have more than one Run in flight at a time?\n\n"
            "**Declared because of tool identity, and it costs throughput.** A governed MCP call "
            "is evaluated against `input.task.capability_envelope` (`paos.tools`), so the broker "
            "must know *which run* is calling. A worker in a PAOS-owned runtime is identified "
            "topologically, the way the egress proxy identifies one — from the accepted socket's "
            "local address, never from anything the worker asserts. A worker PAOS merely calls "
            "over HTTP has no such address, and the alternatives it does offer are all "
            "process-wide: Hermes' `identity_header` is static or profile-scoped, and rewriting "
            "its `mcp_servers` per run is a global, polled, prompt-cache-invalidating reload.\n\n"
            "So for those workers the binding is made rather than discovered: a run holds an "
            "exclusive `ResourceLock` on the worker for its lifetime, and `held_by_run_id` *is* "
            "the calling run. The partial unique index beneath it is what keeps that true.\n\n"
            "The price is one run at a time, which is why this is declared per worker rather than "
            "inferred from `runtime_managed`. A worker exposed to no MCP server needs none of it."
        ),
    )

    # Integration state. A worker declared in configs/workers/_planned.yaml is known to the
    # registry but may not be routed to.
    workspace_repository: str | None = Field(
        default=None,
        description=(
            "The `configs/workspaces/repository.yaml` repository this worker's runs get an "
            "isolated view of, from the worker's `workspace:` block.\n\n"
            "Naming one is what marks the worker as needing a `WorkspaceLease` before dispatch — "
            "roadmap 14.1A, *'every mutating coding Run gets its own branch/worktree'*. Like "
            "`sandbox_profile` marking a PAOS-owned runtime, this is a convenience rather than an "
            "architectural definition: the route's `requirements.isolated_worktree` is the better "
            "long-term source, and belongs to the parallel coding coordinator that needs routing "
            "to decide."
        ),
    )

    integrated: bool = False
    planned_phase: int | None = None

    @property
    def needs_workspace(self) -> bool:
        """Must a lease be held before this worker is dispatched to?

        Separate property rather than a bare `is not None` check at the call site, so the two
        lifecycle questions — *does PAOS own the runtime* and *does the run need an isolated
        checkout* — read the same way and can diverge later without hunting for call sites.
        """
        return self.workspace_repository is not None

    @property
    def runtime_managed(self) -> bool:
        """Does PAOS start and stop this worker's runtime?

        **A convenience, not an architectural definition**, and named rather than inlined so it
        has somewhere to move. Today it reads a policy pointer — "this worker declares a sandbox
        profile" — and answers a lifecycle question, which are genuinely different things. A
        future worker could be runtime-managed while selecting its profile some other way, or
        declare a profile that some other component applies.

        The two coincide in this build because there is exactly one runtime-managed worker family
        and one way to select a profile. Reading that coincidence as an invariant is the mistake
        this docstring exists to prevent.
        """
        return self.sandbox_profile is not None

    @property
    def dispatch_target(self) -> str | None:
        """Where a run would actually be sent, or `None` if nowhere.

        `INTERNAL` workers legitimately have none — they are invoked directly rather than
        through the gateway — so callers check the kind, not just this.
        """
        return usable_dispatch_target(
            endpoint=self.endpoint,
            agent_card_url=self.agent_card_url,
            adapter=self.adapter,
        )

    @model_validator(mode="after")
    def _service_prefix_cannot_climb_out_of_itself(self) -> Self:
        """Normalise the prefix, and refuse one that could name somewhere else.

        The connector joins this onto a request path, so a prefix carrying `..` or an authority is
        trusted metadata that names an unintended destination — the exact property the "target is
        never caller-supplied" rule exists to protect, defeated one layer up by the record itself.
        Normalisation is deliberately dull: a leading slash, no trailing slash, nothing else.
        """
        prefix = self.service_prefix.strip()
        if not prefix.startswith("/"):
            raise ValueError(
                f"worker {self.worker_id!r} service_prefix {self.service_prefix!r} must start "
                "with '/'"
            )
        if "//" in prefix or "\\" in prefix:
            raise ValueError(
                f"worker {self.worker_id!r} service_prefix {self.service_prefix!r} may not contain "
                "an authority — '//' would make the rest of it a host rather than a path"
            )
        if any(segment == ".." for segment in prefix.split("/")):
            raise ValueError(
                f"worker {self.worker_id!r} service_prefix {self.service_prefix!r} contains a "
                "traversal segment, which could reach a path the worker record does not name"
            )
        object.__setattr__(
            self, "service_prefix", "/" + prefix.strip("/") if prefix != "/" else "/"
        )
        return self

    @model_validator(mode="after")
    def _external_workers_are_mediated(self) -> Self:
        if self.kind is not WorkerKind.INTERNAL and not self.effects_mediated:
            raise ValueError(
                f"worker {self.worker_id!r} is external and may not commit effects directly"
            )
        return self


class Runtime(StateMachine):
    """One running instance of a Worker on a Machine, via an ExecutionProvider."""

    @property
    def runtime_id(self) -> RuntimeId:
        """Typed view of `id`. Identity lives in one field only."""
        return RuntimeId(self.id)

    worker_id: WorkerId
    provider_id: ExecutionProviderId
    machine_id: MachineId | None = None
    pool_id: WorkerPoolId | None = None

    template_version: str | None = None
    upstream_component_pin: str | None = Field(
        default=None,
        description="Resolved SHA from configs/upstream.lock.yaml, so a Run records exactly "
        "which upstream produced it (dependency map anti-regression question 8).",
    )

    state: LifecycleState = LifecycleState.INSTALLABLE
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    failure_reason: str | None = None

    # **Liveness and activity are different questions, and one field cannot answer both.**
    #
    # `last_heartbeat_at` says the container is *up*. `heartbeat_interval_seconds` defaults to 30,
    # so a runtime whose heartbeats refreshed the idle clock would never look idle — and a
    # heartbeating, idle container is exactly what Phase 23 exists to stop. The heartbeat therefore
    # feeds `is_stale` and touches neither field below.
    last_activity_at: datetime | None = None
    """When this runtime last did *useful work* — dispatched, woken, or reporting `worker.*`
    progress. The idle detector reads this and never the heartbeat."""

    activity_generation: int = Field(default=0, ge=0)
    """Bumped by those same useful-work events, and by nothing else.

    **Arming an idle timer never bumps it**, and that restraint is the point: a self-re-arming
    poll that bumped on arming would invalidate its own next callback every time. A callback
    carries the value it observed as a stable snapshot, and the stop is a compare-and-swap against
    it — so a timer fired ten minutes late cannot stop a runtime that woke thirty seconds after it
    was armed.

    Deliberately **not** `version`: every write bumps that, bookkeeping included, so it would
    cancel every pending timer and idle-down would never fire at all."""

    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        LifecycleState.INSTALLABLE: {LifecycleState.INSTALLED, LifecycleState.FAILED},
        LifecycleState.INSTALLED: {
            LifecycleState.STARTING,
            LifecycleState.SLEEPING,
            LifecycleState.FAILED,
        },
        LifecycleState.SLEEPING: {LifecycleState.STARTING, LifecycleState.OFFLINE},
        LifecycleState.STARTING: {
            LifecycleState.READY,
            LifecycleState.FAILED,
            LifecycleState.OFFLINE,
        },
        LifecycleState.READY: {
            LifecycleState.BUSY,
            LifecycleState.SLEEPING,
            LifecycleState.DEGRADED,
            LifecycleState.OFFLINE,
            LifecycleState.FAILED,
        },
        LifecycleState.BUSY: {
            LifecycleState.READY,
            LifecycleState.DEGRADED,
            LifecycleState.FAILED,
            LifecycleState.OFFLINE,
        },
        LifecycleState.DEGRADED: {
            LifecycleState.READY,
            LifecycleState.FAILED,
            LifecycleState.OFFLINE,
        },
        LifecycleState.FAILED: {LifecycleState.INSTALLED, LifecycleState.OFFLINE},
        LifecycleState.OFFLINE: {LifecycleState.STARTING, LifecycleState.INSTALLED},
        LifecycleState.EXPERIMENTAL: {LifecycleState.READY, LifecycleState.FAILED},
    }

    def transition_to(self, requested: LifecycleState) -> Self:
        if requested not in self._TRANSITIONS.get(self.state, set()):
            raise StateTransitionError(
                entity="Runtime",
                current=self.state,
                requested=requested,
                allowed=set(self._TRANSITIONS.get(self.state, set())),
            )
        self.state = requested
        self.touch()
        return self

    def is_stale(self, *, ttl: timedelta) -> bool:
        """A runtime that stopped heartbeating is not available, whatever its state says."""
        if self.last_heartbeat_at is None:
            return self.state in {LifecycleState.READY, LifecycleState.BUSY}
        return utc_now() - self.last_heartbeat_at > ttl


class ModelEndpointState(StateMachine):
    """Where a managed model endpoint *is* right now — roadmap §23.1.

    **The other half of a cut Phase 21 already made.** `catalog.ModelEndpoint` is declarative:
    identity, price, reachability, context window — facts about a deployment that are true whether
    or not anything is running. Observed lifecycle is a different kind of fact with a different
    lifetime, and putting it on the declaration would mean a config file claiming to know whether a
    container is up. Phase 21 separated model identity from deployment endpoint for exactly this
    reason; this is that cut one level further in.

    **It cannot reuse `registry.runtimes`.** That table's `worker_id` is NOT NULL and
    `runtime_leases.run_id` is NOT NULL. A model server is neither a worker nor a run, and
    inventing one to satisfy a constraint is how a table stops meaning what its name says.

    **A row exists only because something observed it**, and that is the same rule definition 7
    applies to machines: a row with no probe reads UNKNOWN, never UP. `LifecycleState` has no
    UNKNOWN member and one is deliberately not added — instead, *absence of a row is the unknown*.
    Nothing here is ever written from an assumption about what a container is probably doing.

    **`SLEEPING` and `FAILED` are different states and the distinction is load-bearing.** SLEEPING
    is deliberately stopped and startable: selection wakes it and it is never reported down. A wake
    that *failed* lands in FAILED under a recorded `cooldown_until`, and that does eliminate the
    endpoint from selection. Collapsing the two would have the router retry a broken endpoint
    forever while reporting it merely asleep.

    **Whether the weights are resident is not stored here, on purpose.** Layer A — `ollama stop`,
    or the server's own `keep_alive` expiring — changes residency without telling PAOS anything, so
    a persisted copy would be a claim that goes stale on a timer nobody here controls. It is read
    from the server when it matters (`endpoint_lifecycle.loaded`) and never remembered. READY means
    the server process is up; the 3.3 GB is an optimization *within* READY, not a state of its own.
    """

    model_endpoint_id: str = Field(
        description="The key `configs/models/endpoints.yaml` already assigns. One observed-state "
        "row per declared endpoint; the declaration is the identity and this only records what "
        "happened to it."
    )

    state: LifecycleState = LifecycleState.SLEEPING
    started_at: datetime | None = None
    failure_reason: str | None = None

    cooldown_until: datetime | None = Field(
        default=None,
        description="Set with FAILED, so a transient wake failure is not permanent. A FAILED "
        "endpoint with no cooldown would be eliminated forever by one bad start.",
    )

    # Same three-clock discipline as `Runtime`, minus the one that has no writer here: an endpoint
    # has no heartbeat path, and adding `last_heartbeat_at` for a prober that does not exist would
    # be this phase's own defect in this phase's own table.
    last_activity_at: datetime | None = None
    """When this endpoint last *generated*. Not when it was asked, and not when it was started.

    A cold server answers a completion request with `done_reason: "load"` — a well-formed body with
    an empty response and no token counts — which `model_transport.complete_sealed` refuses. That
    exchange is a load, not work, and recording it as activity would let a failing wake postpone
    its own idle-down indefinitely.
    """

    activity_generation: int = Field(default=0, ge=0)
    """Compare-and-swap guard for the idle timer, bumped by generation and by nothing else.

    Identical in meaning to `Runtime.activity_generation`, including what it is not: never
    `version`, which every bookkeeping write bumps, and never bumped by arming a timer.
    """

    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        LifecycleState.SLEEPING: {LifecycleState.STARTING, LifecycleState.OFFLINE},
        LifecycleState.STARTING: {
            LifecycleState.READY,
            LifecycleState.FAILED,
            LifecycleState.OFFLINE,
        },
        LifecycleState.READY: {
            LifecycleState.BUSY,
            LifecycleState.SLEEPING,
            LifecycleState.FAILED,
            LifecycleState.OFFLINE,
        },
        LifecycleState.BUSY: {
            LifecycleState.READY,
            LifecycleState.FAILED,
            LifecycleState.OFFLINE,
        },
        # Back to STARTING rather than to READY: recovery from a failed wake is another *attempt*,
        # and an endpoint that moved FAILED -> READY would be asserting health nobody observed.
        LifecycleState.FAILED: {LifecycleState.STARTING, LifecycleState.OFFLINE},
        LifecycleState.OFFLINE: {LifecycleState.STARTING},
    }
    """Narrower than `Runtime`'s, because an endpoint has fewer real states.

    No INSTALLABLE/INSTALLED: a declared endpoint's server image is pulled by compose, not
    installed by PAOS. No DEGRADED: there is no partial-service reading to make — it generates or
    it does not. Reusing `LifecycleState` while declaring a different table is the point of the
    transition table being per-class.
    """

    def transition_to(self, requested: LifecycleState) -> Self:
        if requested not in self._TRANSITIONS.get(self.state, set()):
            raise StateTransitionError(
                entity="ModelEndpointState",
                current=self.state,
                requested=requested,
                allowed=set(self._TRANSITIONS.get(self.state, set())),
            )
        self.state = requested
        self.touch()
        return self

    def in_cooldown(self, *, now: datetime | None = None) -> bool:
        """Is a failed endpoint still serving its penalty?

        A FAILED endpoint whose cooldown has expired is eligible to be *tried* again, which is not
        the same as being available — the retry still has to succeed.
        """
        if self.cooldown_until is None:
            return False
        return (now or utc_now()) < self.cooldown_until


class WorkerPool(Aggregate):
    """Worker x provider + autoscaling policy — master spec §4.9, roadmap Phase 35."""

    pool_id: WorkerPoolId = Field(pattern=CONFIG_NAME_PATTERN)
    worker_id: WorkerId
    provider_id: ExecutionProviderId
    runtime_template: str | None = None
    template_version: str | None = None
    capability_bindings: list[str] = Field(default_factory=list[str])

    min_replicas: int = Field(default=0, ge=0)
    max_replicas: int = Field(default=1, ge=0)
    warm_target: int = Field(default=0, ge=0)
    idle_ttl_seconds: int = Field(default=900, ge=0)

    max_queue_depth: int | None = Field(default=None, ge=0)
    min_eval_score: float | None = Field(default=None, ge=0, le=1)
    per_task_budget: Money | None = None
    status: ProviderStatus = ProviderStatus.ACTIVE

    @model_validator(mode="after")
    def _replica_bounds_are_coherent(self) -> Self:
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas may not be below min_replicas")
        if self.warm_target > self.max_replicas:
            raise ValueError("warm_target may not exceed max_replicas")
        return self


class RuntimeLease(Aggregate):
    """Time-bounded allocation of a Runtime to a Run — master spec §4.9.

    The lease, not the runtime, is what a Run holds. That is what makes a sandbox dying
    mid-task recoverable: the lease expires, the Run is re-dispatched, and the effect
    idempotency key stops the external write from happening twice.

    **A lease is not a provisioning record, and conflating the two is the mistake this paragraph
    exists to prevent.** A lease says *this run occupied this worker's runtime for this period*.
    Provisioning says *PAOS created that runtime and is responsible for destroying it*. Both are
    true for a container PAOS starts; only the first is true for a worker that is already running
    somewhere else.

    That distinction is load-bearing in two directions, and it was found by breaking both:

    * **Cost.** The lease records the time a run occupies a worker for cost attribution.
    * **Teardown.** `external_ids` carries the provider's own handle and is empty exactly when
      nothing was provisioned. That emptiness is what tells the release path there is no container
      to destroy. Without it, releasing an externally-run worker's lease would ask Docker to
      remove something that never existed — noise at best, and at worst an attempt on a container
      that merely shares a name.

    So: **every run that occupies a worker gets a lease; only PAOS-provisioned runtimes get a
    provider handle.** `Worker.runtime_managed` decides which, and
    `docs/contracts/runtime_driver.md` carries the longer form.
    """

    @property
    def lease_id(self) -> RuntimeLeaseId:
        """Typed view of `id`. Identity lives in one field only."""
        return RuntimeLeaseId(self.id)

    run_id: RunId
    runtime_id: RuntimeId
    provider_id: ExecutionProviderId
    workspace_lease_id: WorkspaceLeaseId | None = None
    credential_grant_ids: list[CredentialGrantId] = Field(default_factory=list[CredentialGrantId])

    acquired_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    last_heartbeat_at: datetime | None = None
    heartbeat_interval_seconds: int = Field(default=30, ge=1)

    cancelled: bool = False
    destroyed: bool = False
    released_at: datetime | None = None

    @property
    def expired(self) -> bool:
        return utc_now() >= self.expires_at

    @property
    def active(self) -> bool:
        return not (self.cancelled or self.destroyed or self.expired)

    @model_validator(mode="after")
    def _expiry_after_acquisition(self) -> Self:
        if self.expires_at <= self.acquired_at:
            raise ValueError("RuntimeLease expires_at must be after acquired_at")
        return self


class SandboxProfile(PaosModel):
    """Network/filesystem/resource envelope requested of a provider — master spec §6.5.

    **Every field here is a claim a driver must be able to enforce and verify.** A field that is
    configurable but silently unenforced is worse than an absent one: the config reads as though
    the limit applies, and nothing anywhere reports that it does not.
    `adapters/execution/docker/enforcement.py` holds the map from each field to how it is applied
    and how it is checked, and a test asserts that map covers every field declared here — so
    adding a field to this model without deciding how it is enforced fails the build.
    """

    profile_id: str = Field(pattern=CONFIG_NAME_PATTERN)
    network_egress: str = "task_scoped"
    allowed_hosts: list[str] = Field(default_factory=list[str])
    filesystem: str = Field(
        default="isolated",
        description=(
            "Whether this worker's state may outlive its runtime. `isolated` — the default — "
            "means it dies with the runtime. `worker_persistent` means declared paths survive "
            "container recreation.\n\n"
            "Policy only. *Which* paths persist is a property of the worker, carried on "
            "`RuntimeRequest.persistent_paths` carries the worker's declared paths. The driver "
            "refuses a request carrying persistent paths under an `isolated` profile, which is "
            "what keeps this field a decision rather than a label."
        ),
    )

    capabilities: list[str] = Field(
        default_factory=list[str],
        description=(
            "Linux capabilities the runtime RETAINS. Empty — the default — means none: a driver "
            "must drop all of them, not merely refrain from adding any. The distinction is not "
            "pedantic. Docker grants every container a default set (CHOWN, DAC_OVERRIDE, SETUID, "
            "SETGID, NET_RAW, SYS_CHROOT and more), so `--cap-add` being absent leaves that set "
            "in force. `--cap-drop ALL` removes that default set."
        ),
    )

    max_cpu: float | None = Field(default=None, gt=0)
    max_memory_gb: float | None = Field(default=None, gt=0)
    max_wall_seconds: int | None = Field(default=None, gt=0)
    host_bridge_allowed: bool = False
