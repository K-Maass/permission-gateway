"""Effect, Approval and PaymentMandate — master spec §4.12/§4.13/§24.

The central safety claim of the whole architecture: **workers never commit consequential
external writes.** They propose Effects. The gateway runs
`PROPOSED → POLICY_CHECKED → APPROVAL_REQUIRED/APPROVED → EXECUTING → COMMITTED → VERIFIED`,
and every step is durable.

`idempotency_key` is the load-bearing field. Crash recovery, duplicate channel deliveries and
worker retries all converge on it: the same logical write attempted twice must commit once.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator

from paos_domain.base import (
    Aggregate,
    Money,
    PaosModel,
    RiskClass,
    StateMachine,
    StateTransitionError,
    utc_now,
)
from paos_domain.ids import (
    ApprovalId,
    EffectId,
    PrincipalId,
    ResourceId,
    RunId,
    TaskId,
    WorkerId,
)


class EffectState(StrEnum):
    PROPOSED = "PROPOSED"
    POLICY_CHECKED = "POLICY_CHECKED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMMITTED = "COMMITTED"
    VERIFIED = "VERIFIED"
    DENIED = "DENIED"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    MODIFIED = "modified"
    EXPIRED = "expired"


class PaymentMandate(Aggregate):
    """Master spec §24. Architected, disabled by default.

    PAOS never holds card data. A mandate authorizes a bounded class of payment, and the
    actual instrument stays with the payment provider or the human.
    """

    merchant_pattern: str
    amount_ceiling: Money
    currency: str = "USD"
    valid_from: datetime = Field(default_factory=utc_now)
    valid_until: datetime
    max_uses: int = Field(default=1, ge=1)
    uses: int = Field(default=0, ge=0)
    revoked_at: datetime | None = None

    @property
    def valid(self) -> bool:
        now = utc_now()
        return (
            self.revoked_at is None
            and self.valid_from <= now < self.valid_until
            and self.uses < self.max_uses
        )


class Effect(StateMachine):
    """A proposed external side effect — master spec §4.12."""

    @property
    def effect_id(self) -> EffectId:
        """Typed view of `id`. Identity lives in one field only."""
        return EffectId(self.id)

    task_id: TaskId
    run_id: RunId
    proposed_by_worker_id: WorkerId | None = None

    action: str = Field(description="e.g. 'send_email', 'git_merge', 'calendar_write'")
    resource: ResourceId = Field(description="e.g. 'mailto:person@example.com'")
    capability: str
    risk: RiskClass
    source_channel: str | None = None
    trace_id: str | None = None

    payload_hash: str = Field(description="Hash of the exact payload; the receipt binds to it")
    payload_preview: dict[str, Any] = Field(
        default_factory=dict,
        description="Human-readable diff/summary for the approval card. Never the secret.",
    )
    idempotency_key: str = Field(min_length=1)

    state: EffectState = EffectState.PROPOSED
    policy_decision: PolicyDecision | None = None
    policy_reason: str | None = None
    obligations: list[str] = Field(default_factory=list[str])
    approval_id: ApprovalId | None = None

    dry_run: bool = False
    committed_at: datetime | None = None
    verified_at: datetime | None = None
    receipt: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider evidence the write happened — message id, commit sha, event id.",
    )
    verification_error: str | None = None
    compensation_ref: str | None = None

    amount: Money | None = None
    payment_mandate_id: str | None = None

    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        EffectState.PROPOSED: {EffectState.POLICY_CHECKED, EffectState.DENIED},
        EffectState.POLICY_CHECKED: {
            EffectState.APPROVAL_REQUIRED,
            EffectState.APPROVED,
            EffectState.DENIED,
        },
        EffectState.APPROVAL_REQUIRED: {EffectState.APPROVED, EffectState.DENIED},
        EffectState.APPROVED: {EffectState.EXECUTING, EffectState.DENIED},
        EffectState.EXECUTING: {EffectState.COMMITTED, EffectState.FAILED},
        # Verification is separate from commit because a provider can accept a write and
        # still not have performed it. Master spec §5.3 wants reconciliation, not optimism.
        EffectState.COMMITTED: {EffectState.VERIFIED, EffectState.COMPENSATED, EffectState.FAILED},
        EffectState.VERIFIED: set(),
        EffectState.DENIED: set(),
        EffectState.FAILED: {EffectState.COMPENSATED},
        EffectState.COMPENSATED: set(),
    }

    def transition_to(self, requested: EffectState, *, reason: str | None = None) -> Self:
        if requested not in self._TRANSITIONS.get(self.state, set()):
            raise StateTransitionError(
                entity="Effect",
                current=self.state,
                requested=requested,
                allowed=set(self._TRANSITIONS.get(self.state, set())),
            )
        self.state = requested
        if requested is EffectState.COMMITTED:
            self.committed_at = utc_now()
        if requested is EffectState.VERIFIED:
            self.verified_at = utc_now()
        if reason is not None:
            self.policy_reason = reason
        self.touch()
        return self

    @property
    def is_terminal(self) -> bool:
        return not self._TRANSITIONS.get(self.state, set())

    @property
    def committed(self) -> bool:
        return self.state in {EffectState.COMMITTED, EffectState.VERIFIED}

    @model_validator(mode="after")
    def _dry_runs_never_commit(self) -> Self:
        if self.dry_run and self.state in {EffectState.COMMITTED, EffectState.VERIFIED}:
            raise ValueError("a dry-run Effect may never reach COMMITTED (master spec §22.1)")
        return self

    @model_validator(mode="after")
    def _payments_carry_a_mandate(self) -> Self:
        past_proposal = self.state is not EffectState.PROPOSED
        if self.risk is RiskClass.PAYMENT and past_proposal and self.payment_mandate_id is None:
            raise ValueError("a payment Effect past PROPOSED must reference a PaymentMandate")
        return self


class Approval(Aggregate):
    """A human decision on an Effect — master spec §4.13.

    Approvals are idempotent by construction: `decide` refuses to overwrite a decision, so a
    button pressed twice on a flaky phone connection resolves once.
    """

    @property
    def approval_id(self) -> ApprovalId:
        """Typed view of `id`. Identity lives in one field only."""
        return ApprovalId(self.id)

    effect_id: EffectId
    task_id: TaskId
    run_id: RunId

    policy_reason: str
    requested_action: str = Field(description="One-line summary shown on the card")
    payload_diff: str = Field(default="", description="Meaningful diff/preview for the human")
    requester_worker_id: WorkerId | None = None

    expires_at: datetime
    allowed_modifiers: list[str] = Field(
        default_factory=list,
        description="Fields the human may edit before approving, e.g. ['recipient', 'body']",
    )
    response_channel: str | None = None

    decision: ApprovalDecision | None = None
    decided_by: PrincipalId | None = None
    decided_at: datetime | None = None
    modifications: dict[str, Any] = Field(default_factory=dict[str, Any])

    @property
    def expired(self) -> bool:
        return self.decision is None and utc_now() >= self.expires_at

    @property
    def pending(self) -> bool:
        return self.decision is None and not self.expired

    def decide(
        self,
        decision: ApprovalDecision,
        *,
        by: PrincipalId,
        modifications: dict[str, Any] | None = None,
    ) -> Self:
        if self.decision is not None:
            raise ValueError(
                f"approval {self.approval_id} already decided as {self.decision}; "
                "approvals are single-shot so duplicate channel deliveries are safe"
            )
        if self.expired:
            raise ValueError(f"approval {self.approval_id} expired at {self.expires_at}")
        if modifications:
            illegal = set(modifications) - set(self.allowed_modifiers)
            if illegal:
                raise ValueError(f"modifications not permitted for: {sorted(illegal)}")
            self.modifications = modifications
        self.decision = decision
        self.decided_by = by
        self.decided_at = utc_now()
        self.touch()
        return self


class PolicyRequest(PaosModel):
    """The input assembled for OPA — mirrors configs/policies/effects.rego."""

    risk: RiskClass
    capability: str
    principal: dict[str, Any]
    project: dict[str, Any]
    task: dict[str, Any]
    run: dict[str, Any]
    effect: dict[str, Any]
    provider: dict[str, Any]
    workspace_lease: dict[str, Any] = Field(default_factory=dict[str, Any])
    payment_mandate: dict[str, Any] | None = None
    settings: dict[str, Any] = Field(default_factory=dict[str, Any])
    dry_run: bool = False


class PolicyResponse(PaosModel):
    """OPA's decision, as consumed by the effect gateway."""

    effect: PolicyDecision
    reason: str
    obligations: list[str] = Field(default_factory=list[str])
