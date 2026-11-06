"""Run and RoutePlan — master spec §4.7/§4.8.

A Run is one attempt at a Task under one route plan. A RoutePlan is a frozen audit record of
*why* that route was chosen — including the candidates that were eliminated and the reason.
Keeping the rejections is what later lets learned routing (Phase 27) ask "was that the right
call?" instead of only "what happened?".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import Field, model_validator

from paos_domain.base import (
    Aggregate,
    Money,
    PaosModel,
    Percentage,
    RouteMode,
    StateMachine,
    StateTransitionError,
    utc_now,
)
from paos_domain.ids import (
    ExecutionProviderId,
    MachineId,
    ModelEndpointId,
    RoutePlanId,
    RunId,
    RuntimeId,
    RuntimeLeaseId,
    TaskId,
    WorkerId,
    WorkspaceLeaseId,
)


class RunStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class FailureClass(StrEnum):
    """Coarse enough to route on, specific enough to learn from."""

    NONE = "none"
    ROUTING = "routing"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    WORKER_ERROR = "worker_error"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    POLICY_DENIED = "policy_denied"
    APPROVAL_EXPIRED = "approval_expired"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"
    UNKNOWN = "unknown"


class TokenUsage(PaosModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens


class RouteCandidate(PaosModel):
    """One considered route, kept whether or not it won."""

    worker_id: WorkerId
    provider_id: ExecutionProviderId | None = None
    model_endpoint_id: ModelEndpointId | None = None

    eliminated: bool = False
    elimination_reason: str | None = None

    score: float | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict[str, float])
    expected_cost: Money | None = None
    expected_latency_seconds: float | None = Field(default=None, ge=0)
    expected_quality: Percentage | None = None
    risk_note: str | None = None

    @model_validator(mode="after")
    def _elimination_is_explained(self) -> Self:
        if self.eliminated and not self.elimination_reason:
            raise ValueError("an eliminated route candidate must record why")
        return self


class RoutePlan(Aggregate):
    """Frozen audit record of a routing decision — master spec §4.8."""

    @property
    def route_plan_id(self) -> RoutePlanId:
        """Typed view of `id`. Identity lives in one field only."""
        return RoutePlanId(self.id)

    task_id: TaskId
    mode: RouteMode

    # Hard constraints are applied before any scoring (master spec §13.2). Recorded so a
    # denial is explainable without re-running the router.
    hard_constraints: dict[str, object] = Field(default_factory=dict[str, object])
    candidates: list[RouteCandidate] = Field(default_factory=list[RouteCandidate])

    selected_worker_id: WorkerId | None = None
    selected_provider_id: ExecutionProviderId | None = None
    selected_model_endpoint_id: ModelEndpointId | None = None
    fallback_worker_ids: list[WorkerId] = Field(default_factory=list[WorkerId])
    reviewer_worker_ids: list[WorkerId] = Field(default_factory=list[WorkerId])

    policy_snapshot_hash: str | None = Field(
        default=None,
        description="Hash of the policy bundle in force. A decision is only reproducible if "
        "you know which rules produced it.",
    )
    task_spec_hash: str | None = Field(
        default=None,
        description="`Task.spec_hash` at decision time — roadmap §22. Frozen here so that "
        "'without changing task semantics' is a comparison between two immutable plans rather "
        "than against a mutable Task row, which would agree while the spec drifted underneath.",
    )
    decided_at: datetime = Field(default_factory=utc_now)

    @property
    def eliminated(self) -> list[RouteCandidate]:
        return [c for c in self.candidates if c.eliminated]

    @property
    def viable(self) -> list[RouteCandidate]:
        return [c for c in self.candidates if not c.eliminated]

    @model_validator(mode="after")
    def _selection_is_among_viable_candidates(self) -> Self:
        if self.selected_worker_id is None:
            return self
        if self.selected_worker_id not in {c.worker_id for c in self.viable}:
            raise ValueError(
                f"selected worker {self.selected_worker_id!r} is not a viable candidate in "
                "this plan"
            )
        return self


class Run(StateMachine):
    """One execution of a Task under one RoutePlan — master spec §4.7."""

    @property
    def run_id(self) -> RunId:
        """Typed view of `id`. Identity lives in one field only."""
        return RunId(self.id)

    task_id: TaskId
    attempt: int = Field(default=1, ge=1)
    route_plan_id: RoutePlanId | None = None

    worker_id: WorkerId | None = None
    runtime_id: RuntimeId | None = None
    machine_id: MachineId | None = None
    provider_id: ExecutionProviderId | None = None
    model_endpoint_id: ModelEndpointId | None = None

    runtime_lease_id: RuntimeLeaseId | None = None
    workspace_lease_id: WorkspaceLeaseId | None = None

    started_at: datetime | None = None
    ended_at: datetime | None = None
    trace_id: str | None = Field(
        default=None,
        description="Generated before crossing any protocol boundary, so A2A/ACP/MCP/model "
        "spans all join up (dependency map §2).",
    )

    cost: Money | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)

    status: RunStatus = RunStatus.PENDING
    failure_class: FailureClass = FailureClass.NONE
    failure_detail: str | None = None

    # The worker's own handles. External metadata, so replacing the worker does not migrate
    # a single canonical record.
    worker_external_task_id: str | None = None
    worker_context_id: str | None = None

    context_projection_hash: str | None = Field(
        default=None,
        description=(
            "Content address of the context PAOS rendered for this run — roadmap 11.4. A hash "
            "rather than the text: the projection is largely attacker-authored (a channel task's "
            "objective IS the inbound message), and storing a second copy in canonical state "
            "would add a place for it to live without answering a question the first copy "
            "cannot. What this answers is 'did what we told the worker change between these two "
            "runs', which is the forensic question worth having."
        ),
    )

    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        RunStatus.PENDING: {RunStatus.DISPATCHING, RunStatus.CANCELLED, RunStatus.FAILED},
        RunStatus.DISPATCHING: {
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        },
        RunStatus.RUNNING: {
            RunStatus.SUSPENDED,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        },
        # A suspended run is one parked on a durable promise — waiting for an approval or for
        # human input. It may sit here for hours; Restate owns the waiting.
        RunStatus.SUSPENDED: {
            RunStatus.RUNNING,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
        },
        RunStatus.SUCCEEDED: set(),
        RunStatus.FAILED: set(),
        RunStatus.CANCELLED: set(),
        RunStatus.TIMED_OUT: set(),
    }

    def transition_to(
        self,
        requested: RunStatus,
        *,
        failure_class: FailureClass | None = None,
        detail: str | None = None,
    ) -> Self:
        if requested not in self._TRANSITIONS.get(self.status, set()):
            raise StateTransitionError(
                entity="Run",
                current=self.status,
                requested=requested,
                allowed=set(self._TRANSITIONS.get(self.status, set())),
            )
        self.status = requested
        if requested is RunStatus.RUNNING and self.started_at is None:
            self.started_at = utc_now()
        if not self._TRANSITIONS.get(requested, set()):
            self.ended_at = utc_now()
        if failure_class is not None:
            self.failure_class = failure_class
        if detail is not None:
            self.failure_detail = detail
        self.touch()
        return self

    @property
    def is_terminal(self) -> bool:
        return not self._TRANSITIONS.get(self.status, set())

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()
