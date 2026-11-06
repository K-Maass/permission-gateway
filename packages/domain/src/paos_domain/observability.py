"""TraceRef, CostRecord, Evaluation and UpstreamComponent — master spec §4.16, §15, §21."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, model_validator

from paos_domain.base import Aggregate, Money, PaosModel, Percentage, utc_now
from paos_domain.ids import (
    CONFIG_NAME_PATTERN,
    CostRecordId,
    EvalCaseId,
    EvalResultId,
    EvaluationId,
    ExecutionProviderId,
    ModelEndpointId,
    ProjectId,
    RunId,
    TaskId,
    UpstreamComponentId,
    WorkerId,
)


class TraceRef(PaosModel):
    """A W3C trace context, generated before any protocol boundary is crossed.

    Master spec §15.1 and dependency map §2: the id must exist *before* the A2A/ACP/MCP call,
    or the worker's spans arrive as an orphan tree.
    """

    trace_id: str = Field(min_length=1)
    span_id: str | None = None
    parent_span_id: str | None = None
    sampled: bool = True

    def traceparent(self) -> str:
        span = self.span_id or "0" * 16
        flags = "01" if self.sampled else "00"
        return f"00-{self.trace_id}-{span}-{flags}"


class CostCategory(StrEnum):
    MODEL_API = "model_api"

    LOCAL_COMPUTE = "local_compute"
    """Hardware the operator owns. Zero marginal cash, and NOT an entitlement.

    Both are 0.00 and they are different facts, which matters to the Phase 22 router:

        local compute            consumes hardware       exhaustion = contention, recovers
        subscription entitlement consumes a third        exhaustion = hard stop until the
                                 party's quota           period resets

    Collapsed into one category, the router cannot learn "prefer local when the Claude quota
    is nearly spent" — which is the single most valuable thing the distinction buys.
    """

    SUBSCRIPTION_ENTITLEMENT = "subscription_entitlement"
    CLOUD_SANDBOX = "cloud_sandbox"
    CLOUD_MACHINE_TIME = "cloud_machine_time"
    BROWSER_SERVICE = "browser_service"
    STORAGE = "storage"
    OTHER = "other"


class CostRecord(Aggregate):
    """One attributable cost — master spec §6.3 of the roadmap.

    `subscription_entitlement` records usage covered by a flat-rate plan at zero cash cost.
    Without it, native Codex/Claude routes would look free and the router would over-prefer
    them relative to their real opportunity cost.
    """

    @property
    def cost_record_id(self) -> CostRecordId:
        """Typed view of `id`. Identity lives in one field only."""
        return CostRecordId(self.id)

    category: CostCategory
    amount: Money

    project_id: ProjectId | None = None
    task_id: TaskId | None = None
    run_id: RunId | None = None
    worker_id: WorkerId | None = None
    provider_id: ExecutionProviderId | None = None
    model_endpoint_id: ModelEndpointId | None = None

    covered_by_subscription: bool = False
    occurred_at: datetime = Field(default_factory=utc_now)
    trace_id: str | None = None

    # Roadmap 6.3 asks for tokens to be *normalized*, not merely recorded. They are a quantity
    # rather than a price, and they belong in columns rather than in `meta`: tokens are the one
    # number every future router and every budget report wants, and a JSONB field is not
    # something you can sum or index cheaply.
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    price_basis: date | None = None
    """The date of the price list this amount was computed from — roadmap §21.

    A column rather than `meta`, by the same argument the token fields make above: this is the
    field you filter on when a provider changes its prices and you need every row computed from
    the old ones. A basis you cannot query is a basis nobody uses.

    `None` where no price applied — a local model has no cash cost, and recording a date for a
    calculation that never happened would be worse than recording nothing."""

    @property
    def total_tokens(self) -> int | None:
        """Derived, never stored — a stored total is a third number that can disagree."""
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @model_validator(mode="after")
    def _entitlement_usage_is_free_in_cash(self) -> Self:
        if self.covered_by_subscription and self.amount.amount != 0:
            raise ValueError(
                "usage covered by a subscription has no marginal cash cost; record the "
                "entitlement, not a synthetic price"
            )
        return self


class EvalCase(Aggregate):
    """One reusable test of a capability — master spec §4.16."""

    case_id: EvalCaseId = Field(pattern=CONFIG_NAME_PATTERN)
    suite: str
    capability: str
    prompt: str
    expected_criteria: list[str] = Field(default_factory=list[str])
    fixtures: dict[str, Any] = Field(default_factory=dict[str, Any])


class EvalResult(Aggregate):
    """One run of one case against one route."""

    @property
    def result_id(self) -> EvalResultId:
        """Typed view of `id`. Identity lives in one field only."""
        return EvalResultId(self.id)

    case_id: EvalCaseId
    evaluation_id: EvaluationId

    worker_id: WorkerId | None = None
    provider_id: ExecutionProviderId | None = None
    model_endpoint_id: ModelEndpointId | None = None
    run_id: RunId | None = None

    score_dimensions: dict[str, float] = Field(default_factory=dict[str, float])
    human_score: Percentage | None = None
    verifier_score: Percentage | None = None
    cost: Money | None = None
    latency_seconds: float | None = Field(default=None, ge=0)
    interventions: int = Field(default=0, ge=0)
    failure_class: str | None = None

    # Reproducibility: a score is meaningless without the environment that produced it
    # (dependency map anti-regression question 8).
    environment_versions: dict[str, str] = Field(default_factory=dict[str, str])

    @property
    def overall(self) -> float | None:
        if self.human_score is not None:
            return self.human_score
        if self.verifier_score is not None:
            return self.verifier_score
        if not self.score_dimensions:
            return None
        return sum(self.score_dimensions.values()) / len(self.score_dimensions)


class Evaluation(Aggregate):
    """A batch of eval results — the router's empirical knowledge base."""

    @property
    def evaluation_id(self) -> EvaluationId:
        """Typed view of `id`. Identity lives in one field only."""
        return EvaluationId(self.id)

    suite: str
    triggered_by: str = "manual"
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    result_ids: list[EvalResultId] = Field(default_factory=list[EvalResultId])


class UpstreamTreatment(StrEnum):
    DEPLOY = "deploy"
    DEPLOY_IMAGE = "deploy_image"
    DEPLOY_BINARY = "deploy_binary"
    DEPLOY_WITH_ADAPTER = "deploy_with_adapter"
    SDK_PROTOCOL = "sdk_protocol"
    OPTIONAL_CLOUD_PROVIDER = "optional_cloud_provider"
    REFERENCE_ONLY = "reference_only"


class UpstreamComponent(Aggregate):
    """A pinned external dependency — master spec §21.1.

    Mirrors configs/upstream.lock.yaml. `resolved_sha` is mandatory for anything deployed:
    a moving reference is not a pin.
    """

    component_id: UpstreamComponentId
    repo: str
    treatment: UpstreamTreatment
    role: str | None = None

    pin_intent: str
    resolved_tag: str | None = None
    resolved_sha: str | None = None
    resolved_at: datetime | None = None
    license_spdx: str | None = None

    candidate_tag: str | None = None
    candidate_sha: str | None = None
    candidate_status: str | None = None
    previous_sha: str | None = None

    @property
    def deployed(self) -> bool:
        return self.treatment in {
            UpstreamTreatment.DEPLOY,
            UpstreamTreatment.DEPLOY_IMAGE,
            UpstreamTreatment.DEPLOY_BINARY,
            UpstreamTreatment.DEPLOY_WITH_ADAPTER,
        }

    @model_validator(mode="after")
    def _deployed_components_are_pinned(self) -> Self:
        if self.deployed and not self.resolved_sha:
            raise ValueError(
                f"{self.component_id!r} is deployed but has no resolved SHA; "
                "policy deploy_moving_branches is false"
            )
        return self
