"""A fixed synthetic project using PAOS's structured memory selection."""

from datetime import UTC, datetime
from typing import TypedDict
from uuid import UUID

from paos_domain import ClaimStatus, DataClassification, MemoryClaim
from paos_domain.ids import ProjectId
from paos_domain.knowledge import ProjectionScope

TASK_CLASSIFICATION = DataClassification.PERSONAL
_PROJECT = ProjectId(UUID("7d66a0d1-7918-44cf-994f-4a1f947b06b1"))
_REVIEWED_AT = datetime(2026, 1, 5, tzinfo=UTC)
_DEMO_AS_OF = datetime(2026, 1, 6, tzinfo=UTC)


class Fact(TypedDict):
    id: str
    statement: str
    evidence: str


class Withheld(TypedDict):
    id: str
    reason: str


class MemoryView(TypedDict):
    task_classification: str
    facts: list[Fact]
    withheld: list[Withheld]
    policy: str


def _fixture_claim(
    id: str,
    predicate: str,
    object_value: str,
    evidence: str,
    *,
    status: ClaimStatus = ClaimStatus.ACCEPTED,
    sensitivity: DataClassification = DataClassification.PERSONAL,
) -> MemoryClaim:
    return MemoryClaim(
        id=UUID(id),
        project_id=_PROJECT,
        subject="project:demo-release",
        predicate=predicate,
        object_value=object_value,
        source_message_ref=evidence,
        observed_at=_REVIEWED_AT,
        asserted_from=_REVIEWED_AT if status is ClaimStatus.ACCEPTED else None,
        status=status,
        sensitivity=sensitivity,
    )


_FIXTURES = (
    _fixture_claim(
        "95915115-2ce2-4154-a49d-d708d5668c41",
        "requires",
        "an accessibility review before publication.",
        "Synthetic release checklist, item 3",
    ),
    _fixture_claim(
        "48f383ba-c343-48f9-8e3d-d2b6fc628340",
        "may use",
        "a weekend rollout.",
        "Synthetic planning notes, item 4",
        status=ClaimStatus.PROPOSED,
    ),
    _fixture_claim(
        "09297cb3-428b-4532-9891-941baf86e76a",
        "uses",
        "the confidential canary rotation schedule.",
        "Synthetic restricted runbook, item 2",
        sensitivity=DataClassification.SENSITIVE,
    ),
)


def _withheld_reason(claim: MemoryClaim) -> str | None:
    # In-memory port of paos_db.repo.memory_claims._eligible for the automatic projection path.
    if claim.project_id != _PROJECT:
        return "Outside this project"
    if not TASK_CLASSIFICATION.permits(claim.sensitivity):
        return "Claim classification exceeds the task classification"
    if claim.status is ClaimStatus.PROPOSED:
        return "Claim is awaiting review"
    if claim.status is not ClaimStatus.ACCEPTED:
        return "Claim was not accepted"
    if not claim.believed_at(_DEMO_AS_OF) or not claim.valid_at(_DEMO_AS_OF):
        return "Claim is outside the current belief or validity interval"
    scope = claim.projection_scope
    if scope is None and claim.fact_type is not None:
        scope = claim.fact_type.default_projection
    if scope is ProjectionScope.PRIVATE:
        return "Claim is private to the operator"
    return None


def memory_view() -> MemoryView:
    """Serialize the task's view. A withheld record exposes only its ID and policy reason."""
    facts: list[Fact] = []
    withheld: list[Withheld] = []
    for claim in _FIXTURES:
        claim_id = str(claim.id)
        reason = _withheld_reason(claim)
        if reason:
            withheld.append({"id": claim_id, "reason": reason})
        else:
            # The PAOS memory_index.render contract excludes the raw free-text claim body.
            statement = " ".join(
                part for part in (claim.subject, claim.predicate, claim.object_value) if part
            )
            facts.append(
                {"id": claim_id, "statement": statement, "evidence": claim.source_message_ref or ""}
            )
    return {
        "task_classification": TASK_CLASSIFICATION.value,
        "facts": facts,
        "withheld": withheld,
        "policy": "Structured selection of accepted, current, projectable claims "
        "at or below the task's classification.",
    }
