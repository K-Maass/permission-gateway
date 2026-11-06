"""Shared base types and the invariants every canonical record obeys.

Two rules are enforced structurally here rather than left to reviewer discipline:

1. **No provider vocabulary on canonical objects.** Sandbox ids, container ids and vendor
   handles live in `external_ids`, never as declared fields. This is anti-regression
   question 1 in `08_IMPLEMENTATION_DEPENDENCY_MAP.md` §7, and `tests/contracts` asserts it
   across every model in the package.

2. **Extra fields are rejected.** A worker adapter that invents a field gets a validation
   error instead of silently widening the canonical schema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from paos_domain.ids import PrincipalId, ProjectId, uuid7


def utc_now() -> datetime:
    return datetime.now(UTC)


class PaosModel(BaseModel):
    """Base for every canonical model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
        use_enum_values=False,
        ser_json_timedelta="iso8601",
    )


class DataClassification(StrEnum):
    """Master spec §6.3. Ordered; compare with `rank`, never by string equality."""

    PUBLIC = "PUBLIC"
    PERSONAL = "PERSONAL"
    SENSITIVE = "SENSITIVE"
    HIGHLY_SENSITIVE = "HIGHLY_SENSITIVE"

    @property
    def rank(self) -> int:
        return _CLASSIFICATION_RANK[self]

    def permits(self, other: DataClassification) -> bool:
        """True when a ceiling of `self` may carry data classified `other`."""
        return other.rank <= self.rank


_CLASSIFICATION_RANK: dict[DataClassification, int] = {
    DataClassification.PUBLIC: 0,
    DataClassification.PERSONAL: 1,
    DataClassification.SENSITIVE: 2,
    DataClassification.HIGHLY_SENSITIVE: 3,
}


class RiskClass(StrEnum):
    """Policy ceiling implied by a capability name. Mirrors configs/capabilities/taxonomy.yaml."""

    READ_ONLY = "read_only"
    SENSITIVE_READ = "sensitive_read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    HIGH_RISK = "high_risk"
    PAYMENT = "payment"

    @property
    def is_mutating(self) -> bool:
        return self is not RiskClass.READ_ONLY and self is not RiskClass.SENSITIVE_READ

    @property
    def is_committing(self) -> bool:
        """Crosses a system boundary, so it needs an idempotency key (master spec §5.3)."""
        return self in {RiskClass.EXTERNAL_WRITE, RiskClass.HIGH_RISK, RiskClass.PAYMENT}


class Locality(StrEnum):
    LOCAL = "local"
    LAN = "lan"
    CLOUD = "cloud"
    EDGE = "edge"


class RouteMode(StrEnum):
    FAST = "FAST"
    CHEAP = "CHEAP"
    PRIVATE = "PRIVATE"
    BEST = "BEST"
    CUSTOM = "CUSTOM"


class ExternalRef(PaosModel):
    """A provider-native identifier, quarantined.

    An E2B sandbox id, a Telegram message id, an A2A context id and a Git commit SHA all
    arrive here. None of them may become a business identifier — dependency map §5.
    """

    system: str = Field(description="Owning system, e.g. 'e2b', 'telegram', 'a2a', 'git'")
    kind: str = Field(description="Identifier kind within that system, e.g. 'sandbox_id'")
    value: str
    observed_at: datetime = Field(default_factory=utc_now)


class Aggregate(PaosModel):
    """Common shape for every mutable canonical record — master spec §4.1."""

    id: UUID = Field(default_factory=uuid7)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    version: int = Field(default=1, ge=1, description="Optimistic-concurrency counter")
    owner_principal_id: PrincipalId | None = None
    project_id: ProjectId | None = None

    # Free-form operator/adapter data. Anything provider-shaped belongs in `external_ids`
    # so it is greppable and provably not load-bearing.
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    external_ids: list[ExternalRef] = Field(default_factory=list[ExternalRef])

    def touch(self) -> Self:
        """Bump version/updated_at. The DB layer turns `version` into an optimistic lock."""
        self.updated_at = utc_now()
        self.version += 1
        return self

    def external(self, system: str, kind: str) -> str | None:
        for ref in self.external_ids:
            if ref.system == system and ref.kind == kind:
                return ref.value
        return None


class StateTransitionError(ValueError):
    """Raised when a state machine is asked to make an illegal move.

    Phase 2's acceptance gate requires contract tests over invalid transitions, so this is a
    distinct type rather than a bare ValueError.
    """

    def __init__(self, entity: str, current: str, requested: str, allowed: set[str]) -> None:
        self.entity = entity
        self.current = current
        self.requested = requested
        self.allowed = allowed
        super().__init__(
            f"{entity}: cannot move {current} -> {requested}; "
            f"allowed from {current}: {sorted(allowed) or '(terminal)'}"
        )


class StateMachine(Aggregate):
    """Aggregate whose `state` field is governed by an explicit transition table."""

    # Subclasses provide the transition table. It MUST be annotated `ClassVar`: Pydantic
    # turns an annotated underscore attribute into a per-instance ModelPrivateAttr, which
    # silently breaks every classmethod that reads it.
    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {}

    @classmethod
    def transitions(cls) -> dict[str, set[str]]:
        return cls._TRANSITIONS

    @classmethod
    def can_transition(cls, current: str, requested: str) -> bool:
        return requested in cls._TRANSITIONS.get(current, set())

    @classmethod
    def terminal_states(cls) -> set[str]:
        return {s for s, nxt in cls._TRANSITIONS.items() if not nxt}


Percentage = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegative = Annotated[float, Field(ge=0.0)]


class Money(PaosModel):
    """Explicit currency because a bare float invites a silent unit bug in the cost ledger."""

    amount: float = Field(ge=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: object) -> object:
        """Uppercase on the way IN, not after.

        This was an `after` validator that assigned `self.currency`. Every model here sets
        `validate_assignment=True`, so that assignment re-ran the validator, which assigned
        again — `Money` could not be constructed at all, at any currency. Nothing caught it
        because nothing had built one on a live path until the cost ledger did.

        A `before` field validator normalizes the input and never assigns, so the cycle cannot
        form. Any future normalization on these models belongs here for the same reason.
        """
        return value.upper() if isinstance(value, str) else value
