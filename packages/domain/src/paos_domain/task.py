"""Task and TaskEdge — master spec §4.5/§4.6.

Task is the durable unit of intent. Everything else in the system exists to move a Task
through its state machine: a Run is one attempt at it, a RoutePlan explains why that attempt
went where it did, an Effect is a consequence it proposed.

The state machine is data, not scattered `if` statements, because Phase 2's acceptance gate
requires contract tests over invalid transitions and Phase 4 requires the same table to hold
after a crash-and-resume.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import Field, model_validator

from paos_domain.base import (
    Aggregate,
    DataClassification,
    Money,
    PaosModel,
    RouteMode,
    StateMachine,
    StateTransitionError,
)
from paos_domain.ids import ArtifactId, ConversationId, TaskId


class TaskState(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    ROUTING = "ROUTING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskEdgeType(StrEnum):
    REQUIRES = "requires"
    PRODUCES_FOR = "produces_for"
    REVIEWS = "reviews"
    VALIDATES = "validates"
    WAITS_FOR = "waits_for"
    BLOCKS = "blocks"
    FALLBACK_OF = "fallback_of"


class EffectBudget(PaosModel):
    """Ceiling on what a Task may do to the outside world.

    Both a count and a spend limit, because "ten emails" and "ten dollars" fail differently.
    """

    max_effects: int | None = Field(default=None, ge=0)
    effects_committed: int = Field(default=0, ge=0)
    max_spend: Money | None = None
    spent: Money | None = None

    @property
    def remaining(self) -> int | None:
        if self.max_effects is None:
            return None
        return max(0, self.max_effects - self.effects_committed)

    @property
    def exhausted(self) -> bool:
        if self.max_effects is not None and self.effects_committed >= self.max_effects:
            return True
        if self.max_spend is not None and self.spent is not None:
            return self.spent.amount >= self.max_spend.amount
        return False


class Task(StateMachine):
    """Master spec §4.5."""

    parent_task_id: TaskId | None = None
    conversation_id: ConversationId | None = None

    title: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list[str])

    required_capabilities: list[str] = Field(default_factory=list[str])
    input_artifact_ids: list[ArtifactId] = Field(default_factory=list[ArtifactId])
    desired_output_spec: dict[str, object] = Field(default_factory=dict[str, object])

    mode: RouteMode = RouteMode.FAST
    effect_budget: EffectBudget = Field(default_factory=EffectBudget)
    data_classification: DataClassification = DataClassification.PERSONAL

    # The W3C trace this task and everything descended from it belongs to. Minted at
    # submission, not at Run creation: roadmap 6's gate is "followed from **incoming request**",
    # and a trace that starts at the Run cannot answer for routing, policy or approval.
    trace_id: str | None = Field(
        default=None,
        description="32 lowercase hex characters. Inherited by every Run, Effect, Artifact "
        "and event, so one id follows the whole task.",
    )
    deadline: datetime | None = None
    priority: int = Field(default=50, ge=0, le=100)

    state: TaskState = TaskState.DRAFT
    failure_reason: str | None = None

    # A Task carries no channel-specific fields. Where results go is a separate concern
    # resolved through ReturnRoute records keyed by task id, which is what lets a task
    # started on a phone finish hours later in the cloud (Phase 2 acceptance gate).

    @property
    def spec_hash(self) -> str:
        """What this task *asks for*, as one comparable value — roadmap §22.

        §22's gate ends "without changing task semantics", and a re-route has to prove that
        between two plans. `RoutePlan` freezes this value at decision time, so the comparison is
        between two immutable records rather than against this row — which is mutable and
        `version`-counted, and would show agreement while the spec drifted underneath.

        **The ceiling, never the counters.** `effect_budget` mixes the two: `max_effects` and
        `max_spend` are what the task was permitted, while `effects_committed` and `spent` move
        every time an effect commits. Hashing the budget whole would change this value during
        ordinary execution, and a perfectly normal re-route would then read as "the semantics
        changed" — which is the assertion inverted into noise.

        Everything else that moves is out for the same reason: `state`, `version`, `updated_at`,
        `failure_reason`, `trace_id`.
        """
        spec = {
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_capabilities": sorted(self.required_capabilities),
            "desired_output_spec": self.desired_output_spec,
            "mode": self.mode.value,
            "data_classification": self.data_classification.value,
            "max_effects": self.effect_budget.max_effects,
            "max_spend": (
                None
                if self.effect_budget.max_spend is None
                else [
                    str(self.effect_budget.max_spend.amount),
                    self.effect_budget.max_spend.currency,
                ]
            ),
        }
        # `sort_keys` and a fixed separator, because two encodings of one spec would be two
        # hashes that should have been one — the defect `content_address` exists to prevent for
        # artifact bytes, one layer up.
        canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        TaskState.DRAFT: {TaskState.QUEUED, TaskState.CANCELLED},
        TaskState.QUEUED: {TaskState.ROUTING, TaskState.PAUSED, TaskState.CANCELLED},
        TaskState.ROUTING: {
            TaskState.RUNNING,
            TaskState.QUEUED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        },
        TaskState.RUNNING: {
            TaskState.WAITING_INPUT,
            TaskState.WAITING_APPROVAL,
            TaskState.PAUSED,
            TaskState.RETRYING,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        },
        TaskState.WAITING_INPUT: {
            TaskState.RUNNING,
            # Symmetric with WAITING_APPROVAL below, and for the same reason: a prompt nobody
            # answered before its TTL is an ordinary event, not a failure. Hours of work must
            # not die because the operator was asleep, so the task drops back to RETRYING and
            # the question can be re-asked.
            TaskState.RETRYING,
            TaskState.FAILED,
            TaskState.CANCELLED,
        },
        TaskState.WAITING_APPROVAL: {
            TaskState.RUNNING,
            # An approval that expires unanswered re-asks rather than killing the task. On a
            # phone-first system a missed notification is ordinary, not exceptional, and hours
            # of work should not die because a prompt scrolled away. ApprovalBroker issues a
            # fresh Approval on this edge.
            TaskState.RETRYING,
            TaskState.FAILED,
            TaskState.CANCELLED,
        },
        TaskState.PAUSED: {TaskState.RUNNING, TaskState.QUEUED, TaskState.CANCELLED},
        TaskState.RETRYING: {
            TaskState.ROUTING,
            TaskState.RUNNING,
            TaskState.FAILED,
            TaskState.CANCELLED,
        },
        # Terminal. A retry is a new Run under the same Task while it is still RETRYING;
        # reopening finished work is a new Task, so history stays immutable.
        TaskState.SUCCEEDED: set(),
        TaskState.FAILED: set(),
        TaskState.CANCELLED: set(),
    }

    def transition_to(self, requested: TaskState, *, reason: str | None = None) -> Self:
        if requested not in self._TRANSITIONS.get(self.state, set()):
            raise StateTransitionError(
                entity="Task",
                current=self.state,
                requested=requested,
                allowed=set(self._TRANSITIONS.get(self.state, set())),
            )
        self.state = requested
        if reason is not None:
            self.failure_reason = reason
        self.touch()
        return self

    @property
    def is_terminal(self) -> bool:
        return not self._TRANSITIONS.get(self.state, set())

    @property
    def is_waiting(self) -> bool:
        return self.state in {TaskState.WAITING_INPUT, TaskState.WAITING_APPROVAL}

    @model_validator(mode="after")
    def _private_mode_implies_classification(self) -> Self:
        """PRIVATE is a promise about egress, so it may not be paired with PUBLIC data.

        Catching this at construction stops a task from claiming privacy the router will not
        actually enforce.
        """
        if self.mode is RouteMode.PRIVATE and self.data_classification is DataClassification.PUBLIC:
            raise ValueError("PRIVATE mode with PUBLIC classification is contradictory")
        return self


class TaskEdge(Aggregate):
    """A dynamic-DAG edge — master spec §4.6.

    Workers may *propose* subtasks and edges; only the orchestrator commits them, which is
    why `proposed_by_run_id` and `committed` are recorded separately.
    """

    from_task_id: TaskId
    to_task_id: TaskId
    edge_type: TaskEdgeType
    committed: bool = False
    proposed_by_run_id: str | None = None

    @model_validator(mode="after")
    def _no_self_edge(self) -> Self:
        if self.from_task_id == self.to_task_id:
            raise ValueError("a TaskEdge may not connect a task to itself")
        return self
