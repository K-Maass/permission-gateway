"""MemoryClaim, Skill, Workflow — master spec §4.15, §23.

Canonical memory is evidence-based. A claim without a source event, artifact or message is
not admissible: derived stores (Honcho, Graphiti, Letta) are projections and may never become
the source of truth. That constraint is what keeps a worker's confident hallucination from
quietly becoming a durable fact about the operator's life.

Extraction can repeat instructions embedded in untrusted input. Every extracted claim
starts as PROPOSED and requires a governance decision.

**Two time axes, and they answer different questions.** `valid_from`/`valid_to` say when a fact was
true *in the world*; `asserted_from`/`asserted_to` say when PAOS *believed* it. "What is true now?"
needs the first. "What did we believe on 1 March?" needs the second, and no amount of valid-time
detail substitutes for it.

**A third axis arrives with `MemorySource`, and conflating it with the other two is the mistake
H2 exists to avoid.** Whether the *evidence* is still available is not whether the claim is still
believed, and neither is whether the fact is still true. Deleting a document for retention reasons
and declaring the fact it supported false are different acts; a claim supported by three sources
does not stop being grounded when one is erased. So erasure tombstones a source and **nothing is
retracted automatically** — `grounded_by` derives the answer, and no column is rewritten, for the
same reason `ClaimStatus.SUPERSEDED` is never written.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Self
from uuid import UUID

from pydantic import Field, model_validator

from paos_domain.base import Aggregate, DataClassification, PaosModel, Percentage, utc_now
from paos_domain.ids import (
    CLAIM_SUBJECT_PATTERN,
    CONFIG_NAME_PATTERN,
    ArtifactId,
    ConversationId,
    EntityId,
    EventId,
    MemoryClaimId,
    MemorySourceId,
    ProjectId,
    RunId,
    SkillId,
    TaskId,
    WorkflowId,
)


class ClaimStatus(StrEnum):
    """The governance verdict a claim was born with.

    **`SUPERSEDED` is never written.** It is listed because master spec §4.15 names it and because
    any row that ever carried it must still resolve — but supersession is expressed in transaction
    time (`asserted_to`), not by rewriting this column. Mutating `status` in place would destroy
    the gate: on the date in question we believed that claim was `ACCEPTED`, and overwriting the
    field makes that unrecoverable. `is_superseded` derives the display value instead.
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class FactType(StrEnum):
    """What kind of statement a claim is — Hardening Phase H4, frozen at Stage 0.

    **The line is stated versus inferred, not self versus other.** An earlier draft made every
    third-party fact unpromotable, which would have left PAOS unable to remember that *"Alice owns
    the SAP migration workstream"* — the durable, work-shaped facts a meeting actually produces,
    and the ones a reviewer is best placed to confirm. Refusing those buys almost no privacy,
    because a human already has to accept them, and costs ambient capture most of its point.

    What stays refused is the class human review is *worst* at catching: an attribute of a person
    that **nobody stated**, derived by a model from how they spoke, arriving already looking
    reviewed. Speculation is refused for the reason `PredicateSpec` refuses temporal-and-promotable
    — a hedge that becomes a fact has lost the hedge.

    **Nothing here grants promotion.** Ambient extraction is `Actor.EXTRACTOR`, so
    `may_auto_promote` fails on its second conjunct whatever the fact type says. This decides what
    a *human* may then accept, and under which floor.
    """

    SELF_FACT = "self_fact"
    """The operator about themselves. Ordinary review."""

    WORLD_FACT = "world_fact"
    """Durable and not about a person. Ordinary review."""

    DECISION = "decision"
    """An outcome a conversation settled on."""

    COMMITMENT = "commitment"
    """Someone undertaking to do something by some time."""

    THIRD_PARTY_PERSONAL = "third_party_personal"
    """**Stated** about someone other than the operator. Human-promotable under a SENSITIVE floor,
    and `PRIVATE` for projection until a reviewer says otherwise — remembering a colleague's role
    should not put it in every worker's context by default."""

    TRANSIENT = "transient"
    """True now and not for long — "today", "right now". Carries a `valid_to`, because a fact that
    expires must not be believed without the review that would have caught it expiring."""

    SPECULATION = "speculation"
    """Hedged or conditional. **Never promotable, by a human either.**"""

    PERSONAL_INFERENCE = "personal_inference"
    """An attribute of a person nobody stated, derived by a model. **Never promotable.** SENSITIVE
    floor. This is profiling, and the fact that it reads plausibly is the danger rather than a
    mitigation."""

    @property
    def promotable_by_review(self) -> bool:
        """Whether a human may accept this at all."""
        return self not in {FactType.SPECULATION, FactType.PERSONAL_INFERENCE}

    @property
    def classification_floor(self) -> DataClassification:
        """The lowest classification a claim of this type may carry."""
        if self in {FactType.THIRD_PARTY_PERSONAL, FactType.PERSONAL_INFERENCE}:
            return DataClassification.SENSITIVE
        return DataClassification.PERSONAL

    @property
    def needs_resolved_speaker(self) -> bool:
        """Whether accepting this requires knowing who said it.

        A reviewer must resolve the speaker before accepting a stated fact about a named person.
        """
        return self is FactType.THIRD_PARTY_PERSONAL

    @property
    def default_projection(self) -> ProjectionScope:
        """Where this type goes by default when nobody has said."""
        if self is FactType.THIRD_PARTY_PERSONAL:
            return ProjectionScope.PRIVATE
        return ProjectionScope.PROJECT


class ProjectionScope(StrEnum):
    """Whether a claim may reach a worker automatically — H4.

    **`PRIVATE` means "not projected automatically", never "hidden from canonical memory".** The
    operator's own Memory API and Control Web still return it: governance you cannot see is not
    governance. The filter lives at the one automatic boundary — `projection._known_facts` — and
    not in `claims_at` or `eligible_scope`, which every authenticated read shares.
    """

    PROJECT = "project"
    """Projectable into worker context, within its project and its ceiling."""

    PRIVATE = "private"
    """Retrievable by the operator, never projected automatically."""


class MemoryClaim(Aggregate):
    """One evidence-backed assertion — master spec §4.15."""

    @property
    def claim_id(self) -> MemoryClaimId:
        """Typed view of `id`. Identity lives in one field only."""
        return MemoryClaimId(self.id)

    # Constrained, not free text — see `CLAIM_SUBJECT_PATTERN`. Without embeddings an
    # unspellable subject is write-only data that looks stored.
    subject: str = Field(pattern=CLAIM_SUBJECT_PATTERN)
    predicate: str
    object_value: str | None = None
    # Named for its column. The free-text form of the claim, and the one field that could carry a
    # message body — which is why `kernel/projection.py` renders subject/predicate/object and
    # never this.
    claim_text: str | None = None

    # Provenance. At least one source is mandatory; see the validator below.
    source_event_id: EventId | None = None
    source_artifact_id: ArtifactId | None = None
    source_message_ref: str | None = None
    source_run_id: RunId | None = None
    conversation_id: ConversationId | None = None

    # Valid time: when the fact was true in the world.
    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    # Transaction time: when PAOS believed it. `asserted_to is None` means "still believed".
    # Half-open `[from, to)` on both axes, so a supersession leaves no gap and no overlap.
    asserted_from: datetime | None = None
    asserted_to: datetime | None = None

    confidence: Percentage = 0.5

    status: ClaimStatus = ClaimStatus.PROPOSED
    sensitivity: DataClassification = DataClassification.PERSONAL
    extraction_model: str | None = None
    extraction_version: str | None = None
    supersedes_claim_id: MemoryClaimId | None = None
    fact_type: FactType | None = None
    projection_scope: ProjectionScope | None = None
    """Where this claim may go automatically. `None` means *use the fact type's default*, which
    keeps every claim written before H4 projecting exactly as it did with no backfill."""

    @property
    def is_superseded(self) -> bool:
        """Derived, never stored. See `ClaimStatus`."""
        return self.asserted_to is not None

    def valid_at(self, moment: datetime) -> bool:
        """Was the fact true in the world at `moment`?"""
        if self.valid_from is not None and moment < self.valid_from:
            return False
        return not (self.valid_to is not None and moment >= self.valid_to)

    def believed_at(self, moment: datetime) -> bool:
        """Did PAOS hold this claim at `moment`?

        A claim with no `asserted_from` has never been asserted — a candidate that governance has
        not yet seen — so the answer is no rather than "always".
        """
        if self.asserted_from is None or moment < self.asserted_from:
            return False
        return not (self.asserted_to is not None and moment >= self.asserted_to)

    @property
    def currently_valid(self) -> bool:
        """Accepted, still believed, and true now — all three.

        **The `believed_at` conjunct is load-bearing.** Without it a superseded claim still reads as
        currently valid, because supersession deliberately does not touch `status`, and the context
        projection would hand a worker a fact PAOS stopped believing.

        Previously this derived `now` from `observed_at.tzinfo`, taking its timezone from an
        unrelated field; `utc_now()` is what every other clock in the domain uses.
        """
        now = utc_now()
        return self.status is ClaimStatus.ACCEPTED and self.believed_at(now) and self.valid_at(now)

    @model_validator(mode="after")
    def _claims_carry_provenance(self) -> Self:
        sources = (
            self.source_event_id,
            self.source_artifact_id,
            self.source_message_ref,
            self.source_run_id,
        )
        if not any(s is not None for s in sources):
            raise ValueError(
                "a MemoryClaim must cite at least one source (event, artifact, message or "
                "run); unsourced claims cannot become canonical truth"
            )
        return self

    @model_validator(mode="after")
    def _validity_window_is_ordered(self) -> Self:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self

    @model_validator(mode="after")
    def _belief_window_is_ordered(self) -> Self:
        if self.asserted_from and self.asserted_to and self.asserted_to <= self.asserted_from:
            raise ValueError("asserted_to must be after asserted_from")
        return self

    @model_validator(mode="after")
    def _a_claim_does_not_supersede_itself(self) -> Self:
        """A self-edge in the one structure the lineage walk follows.

        `artifacts.parent_versions` learned this the same way: a retried write that finds its own
        row can hand a record its own id, and the reader loops for ever.
        """
        if self.supersedes_claim_id is not None and self.supersedes_claim_id == self.claim_id:
            raise ValueError("a claim cannot supersede itself")
        return self


class ClaimDraft(PaosModel):
    """Everything a proposer may say about a claim, and nothing about belief — roadmap §20.

    **The load-bearing property is a field that does not exist.** `PaosModel` sets
    `extra="forbid"`, so a request body carrying `"status": "accepted"` is a visible 422 rather
    than a silent coercion. That is a stronger guarantee than a coercion in the repository,
    because a coercion is one `**{**_to_row(x), ...}` away from being overridden — which is
    literally how Phase 20's defect came to exist: `supersede` spread `_to_row(successor)` and
    then forced `asserted_from`, and `_to_row` copied `status` verbatim from the wire.

    So `status`, `asserted_from`, `asserted_to` and `supersedes_claim_id` are **absent**. Three of
    them are governance's to write. The fourth is authority too: which claim a candidate replaces
    comes from the curator's object key, so a proposer cannot aim its candidate at a claim it was
    not invited to replace.

    **`id` is absent for the same reason.** `MemoryCurator/{claim_id}/propose` is already keyed by
    the claim, and a body that could name a different one would have the step ledger serialising
    writes to a row it is not protecting. A successor's id is minted by the curator from
    `ctx.uuid()` — deterministic across replays, and not the proposer's to choose.
    """

    subject: str = Field(pattern=CLAIM_SUBJECT_PATTERN)
    predicate: str
    object_value: str | None = None
    claim_text: str | None = None

    project_id: ProjectId | None = None

    # Provenance. At least one is mandatory, and `to_claim` is where that is enforced — by
    # building a `MemoryClaim`, whose validator has said so since the first migration.
    source_event_id: EventId | None = None
    source_artifact_id: ArtifactId | None = None
    source_message_ref: str | None = None
    source_run_id: RunId | None = None
    conversation_id: ConversationId | None = None

    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    confidence: Percentage = 0.5
    sensitivity: DataClassification = DataClassification.PERSONAL
    extraction_model: str | None = None
    extraction_version: str | None = None
    fact_type: FactType | None = Field(
        default=None,
        description="What kind of statement this is — H4. Decides the classification floor,"
        " whether a human may accept it and whether it projects. `None` for the pre-H4 tiers,"
        " which the database's ambient-is-typed CHECK permits only for non-ambient extractors.",
    )

    def to_claim(
        self, claim_id: MemoryClaimId, *, supersedes: MemoryClaimId | None = None
    ) -> MemoryClaim:
        """The aggregate this draft proposes: `PROPOSED`, asserted at no moment.

        Both belief fields are written here as constants rather than defaulted, so that reading
        this method answers "what does a proposal become?" without also reading `MemoryClaim`'s
        field defaults. A claim nobody has accepted was never believed.
        """
        return MemoryClaim(
            id=claim_id,
            subject=self.subject,
            predicate=self.predicate,
            object_value=self.object_value,
            claim_text=self.claim_text,
            project_id=self.project_id,
            source_event_id=self.source_event_id,
            source_artifact_id=self.source_artifact_id,
            source_message_ref=self.source_message_ref,
            source_run_id=self.source_run_id,
            conversation_id=self.conversation_id,
            observed_at=self.observed_at,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            confidence=self.confidence,
            sensitivity=self.sensitivity,
            extraction_model=self.extraction_model,
            extraction_version=self.extraction_version,
            fact_type=self.fact_type,
            # **Derived, never chosen by the proposer.** A tier that could pick its own projection
            # scope could put a third-party fact into worker context by saying so.
            projection_scope=self.fact_type.default_projection if self.fact_type else None,
            status=ClaimStatus.PROPOSED,
            asserted_from=None,
            asserted_to=None,
            supersedes_claim_id=supersedes,
        )

    @classmethod
    def of(cls, claim: MemoryClaim) -> Self:
        """The proposable half of an existing claim — what a producer would have sent.

        Here rather than at the call sites because the field list is the whole point: a
        hand-written conversion that forgets `sensitivity` downgrades a claim's classification
        silently, and one that remembers `status` defeats the type.
        """
        return cls(
            **claim.model_dump(
                include={
                    "subject",
                    "predicate",
                    "object_value",
                    "claim_text",
                    "project_id",
                    "source_event_id",
                    "source_artifact_id",
                    "source_message_ref",
                    "source_run_id",
                    "conversation_id",
                    "observed_at",
                    "valid_from",
                    "valid_to",
                    "confidence",
                    "sensitivity",
                    "extraction_model",
                    "extraction_version",
                }
            )
        )


class EntityKind(StrEnum):
    """What sort of thing an entity is.

    **Small and closed on purpose.** An open vocabulary would let each document introduce its own
    notion of what exists, which is the free-text subject problem moved one level up. These four
    are what the seed rules and the document tier can actually distinguish; a fifth is a decision
    somebody makes, not a string somebody types.
    """

    PERSON = "person"
    ORGANIZATION = "organization"
    PRODUCT = "product"
    PLACE = "place"


class EntityStatus(StrEnum):
    """Governance's verdict on an entity, mirroring `ClaimStatus` deliberately.

    An extractor proposes entities. Review establishes the canonical subject that later
    claims can attach to.
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Entity(Aggregate):
    """A thing in the operator's world that several names refer to — H2 Stage 5.

    **The only claim subject that is not a PAOS record.** `project:`, `task:`, `run:` and the rest
    name rows the kernel created and can therefore be trusted to exist and to mean one thing. An
    entity names something outside PAOS, introduced by evidence, and nothing else vouches for it —
    which is exactly why it is governed rather than minted.
    """

    @property
    def entity_id(self) -> EntityId:
        """Typed view of `id`. Identity lives in one field only."""
        return EntityId(self.id)

    kind: EntityKind
    display_name: str = Field(min_length=1, max_length=200)
    status: EntityStatus = EntityStatus.PROPOSED

    @property
    def subject(self) -> str:
        """How a claim refers to this entity.

        Built here rather than at call sites so the one place that knows the format is the type
        that owns it — the same reason `CLAIM_SUBJECT_PATTERN` is derived from its own tuple.
        """
        return f"entity:{self.id}"

    @property
    def canonical(self) -> bool:
        """Accepted, and therefore something a claim may attach to."""
        return self.status is EntityStatus.ACCEPTED


class EntityAlias(PaosModel):
    """One surface form that refers to an entity, and where that was learned.

    **Normalisation is stored, not computed at read time.** A lookup that lowercased on the way in
    but not on the way out is a lookup that misses, and the miss looks like "no such entity" rather
    than like a bug. `normalize` is the single definition, and the column holds its output so the
    unique index means what it says.
    """

    entity_id: EntityId
    surface: str = Field(min_length=1, max_length=200)
    normalized: str = Field(min_length=1, max_length=200)
    # Which evidence introduced this alias. An alias with no provenance is a claim about identity
    # that nobody made — the same rule `MemoryClaim` applies to facts.
    source_id: MemorySourceId | None = None

    @classmethod
    def of(
        cls, entity_id: EntityId, surface: str, *, source_id: MemorySourceId | None = None
    ) -> Self:
        return cls(
            entity_id=entity_id,
            surface=surface,
            normalized=normalize_surface(surface),
            source_id=source_id,
        )


def normalize_surface(surface: str) -> str:
    """The form an alias is looked up by.

    **Deliberately crude, and that is the design rather than a first cut.** Case folding and
    whitespace collapsing are reversible facts about typography; anything cleverer — stripping
    "AG", "Ltd", "the" — is a judgement about what two names mean, and a judgement that silently
    merged two real organisations would be indistinguishable from a correct merge. Fuzzy matching
    belongs where it can be measured against a question set, which is H3.
    """
    return " ".join(surface.split()).casefold()


class SubjectOutcome(StrEnum):
    """What an extractor did about a subject — H2 Definition 6's four outcomes.

    **`BIND` and `PROPOSE` are the two an entity-aware tier reaches**; the alias is the *mechanism*
    for binding rather than a separate outcome, which is the one place this enum reads differently
    from the definition that named it. `CONTEXT` and `REFUSE` are the other two, and both are
    outcomes a tier can take without an entity layer at all.
    """

    BIND = "bind"
    """A declared alias resolved to exactly one accepted entity. The claim attaches to it."""

    PROPOSE = "propose"
    """Nothing matched. A candidate entity is proposed alongside the claim, and **both** wait for
    governance — a claim about a subject nobody has accepted is not a fact about anything yet."""

    CONTEXT = "context"
    """The tier does not resolve subjects; the caller supplies one from context. What the marker
    extractor does today, and the only outcome that needs no entity layer."""

    REFUSE = "refuse"
    """The surface form matched more than one accepted entity. **Refused rather than guessed**: a
    wrong merge attaches one thing's claims to another, and unpicking that later means knowing
    which claims were wrong, which is precisely what was lost."""


class SubjectResolution(PaosModel):
    """One decision about what a claim is about, and why.

    Carries the reason because a review queue that says only "proposed" gives an operator nothing
    to decide with — the useful question is *which* surface form was unrecognised.
    """

    outcome: SubjectOutcome
    subject: str | None = None
    entity_id: EntityId | None = None
    surface: str = ""
    detail: str = ""


def resolve_subject(
    surface: str,
    *,
    matches: Sequence[Entity],
) -> SubjectResolution:
    """Decide what a claim about `surface` is about, given the entities its alias matched.

    **Pure, and the lookup is the caller's.** The repository knows how to find candidate entities;
    this knows what to do with what it found. Keeping the decision out of the query is what lets
    every outcome be a unit test rather than a fixture.

    **Only accepted entities can be bound to.** A proposed entity is a candidate nobody has agreed
    exists, and binding a claim to one would make acceptance of the entity retroactively decide
    what a pile of claims were about.
    """
    canonical = [entity for entity in matches if entity.canonical]
    if len(canonical) == 1:
        entity = canonical[0]
        return SubjectResolution(
            outcome=SubjectOutcome.BIND,
            subject=entity.subject,
            entity_id=entity.entity_id,
            surface=surface,
            detail=f"alias resolved to {entity.display_name!r}",
        )
    if len(canonical) > 1:
        names = ", ".join(sorted(entity.display_name for entity in canonical))
        return SubjectResolution(
            outcome=SubjectOutcome.REFUSE,
            surface=surface,
            detail=f"{surface!r} matches {len(canonical)} accepted entities: {names}",
        )
    return SubjectResolution(
        outcome=SubjectOutcome.PROPOSE,
        surface=surface,
        detail=f"no accepted entity is known by {surface!r}",
    )


class SourceKind(StrEnum):
    """Where a piece of evidence already lives.

    **There is no `INLINE` member, and that absence is the design.** PAOS already stores every
    kind of evidence it has: a channel turn's text is `core.tasks.objective` (Phase 10 put it
    there and `_title_of`'s docstring says so — *"the objective keeps the full text"*), an
    uploaded document is a content-addressed `artifacts.artifact_versions` row (Phase 12), and a
    repository file stays in the repository. A source that carried bytes of its own would make
    PAOS the second owner of its own evidence, which is the duplicate-source-of-truth this phase
    is written to avoid.
    """

    ARTIFACT_VERSION = "artifact_version"
    """A stored document. The locator is an artifact version id; the bytes are content-addressed."""

    TASK_OBJECTIVE = "task_objective"
    """An inbound message. The locator is a task id; `objective` holds the full text already."""

    EXTERNAL = "external"
    """Something PAOS does not store — a repository file at a commit. The locator is a URI, and
    `content_hash` is what makes a citation into it checkable rather than merely plausible."""


class RetentionClass(StrEnum):
    """How long evidence may be kept, decided when it is recorded rather than when it is swept.

    Declared per source because retention is a property of *what the evidence is*, not of how old
    it has become — the same reasoning `StructuralRule.sensitivity` uses for classification.
    """

    EPHEMERAL = "ephemeral"
    """Erasable as soon as nothing needs it. Transcripts and scratch captures."""

    STANDARD = "standard"
    """The default. Kept until the operator says otherwise."""

    PERMANENT = "permanent"
    """Never swept. For evidence behind claims that must stay auditable."""


class MemorySource(Aggregate):
    """One piece of evidence PAOS can cite, recorded by reference — H2.

    **This row owns no bytes.** It says *where* the evidence is, *what classification it carries*,
    and *whether it is still available*. See `SourceKind` for why that is structural rather than
    an optimisation.

    **Tombstoning is not deletion of the row.** The row survives with its locator, its
    classification and its provenance; what goes is the availability of what it points at. That is
    what lets an erasure be honoured *and* recorded — a source that vanished entirely would leave
    a claim citing nothing, which reads identically to a claim that never had evidence.
    """

    @property
    def source_id(self) -> MemorySourceId:
        """Typed view of `id`. Identity lives in one field only."""
        return MemorySourceId(self.id)

    kind: SourceKind

    # Exactly one of these is set, and which one is fixed by `kind` — see the validator below.
    # Neither uuid is a foreign key, for the reason `MemoryClaim.source_artifact_id` already
    # states: an operational row can legitimately be removed, and deleting one must not take the
    # evidence record that cited it.
    artifact_version_id: UUID | None = None
    task_id: TaskId | None = None
    external_ref: str | None = None

    content_hash: str | None = Field(
        default=None,
        description="sha256 of the evidence where PAOS can compute one. What makes a citation"
        " checkable: the same locator with different content is a different source.",
    )

    classification: DataClassification = DataClassification.PERSONAL
    retention: RetentionClass = RetentionClass.STANDARD
    observed_at: datetime

    tombstoned_at: datetime | None = None
    tombstone_reason: str | None = None

    _LOCATOR_FOR: ClassVar[dict[str, str]] = {
        SourceKind.ARTIFACT_VERSION: "artifact_version_id",
        SourceKind.TASK_OBJECTIVE: "task_id",
        SourceKind.EXTERNAL: "external_ref",
    }

    @property
    def available(self) -> bool:
        """Is the evidence still there to be read?"""
        return self.tombstoned_at is None

    def tombstone(self, reason: str) -> Self:
        """Record that the evidence is gone. **Says nothing about any claim that cited it.**

        Idempotent: erasing twice is one erasure, and the first reason is the true one.
        """
        if self.tombstoned_at is None:
            self.tombstoned_at = utc_now()
            self.tombstone_reason = reason
            self.touch()
        return self

    @model_validator(mode="after")
    def _exactly_the_locator_its_kind_names(self) -> Self:
        """One locator, and the right one.

        Checked rather than trusted because a source with two locators has no single answer to
        "what evidence is this?", and one with none is a citation pointing nowhere — both of which
        would only be discovered by a reader much later.
        """
        expected = self._LOCATOR_FOR[self.kind]
        present = {
            name
            for name in ("artifact_version_id", "task_id", "external_ref")
            if getattr(self, name) is not None
        }
        if present != {expected}:
            raise ValueError(
                f"a {self.kind.value} source must set exactly {expected!r}; got {sorted(present)}"
            )
        return self


class Citation(PaosModel):
    """One claim's reference into one source, and where inside it.

    **The span is what makes a claim citable.** "This came from that document" is not something an
    operator can check; "this came from characters 1,204-1,281 of that version" is. A claim derived
    from an event carries no citation at all — `MemoryClaim.source_event_id` is already exact, and
    the event log is append-only so it can never be tombstoned.
    """

    claim_id: MemoryClaimId
    source_id: MemorySourceId
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    quote: str | None = Field(
        default=None,
        max_length=500,
        description="The cited text as it read when the claim was made. A convenience for the"
        " reviewer and never the evidence itself — the span into the source is that.",
    )

    speaker_label: str | None = Field(
        default=None,
        max_length=120,
        description="The speaker label from the capture source, pending identity resolution.",
    )
    speaker_ref: str | None = Field(default=None, max_length=200)
    speaker_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How much that label is worth. A ceiling rather than a guess — a bare"
        " SPEAKER_01 scores zero, and a third-party fact cannot be accepted until it is resolved.",
    )

    @model_validator(mode="after")
    def _span_is_ordered_and_whole(self) -> Self:
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("a span needs both ends or neither")
        if (
            self.span_start is not None
            and self.span_end is not None
            and (self.span_end <= self.span_start)
        ):
            raise ValueError("span_end must be after span_start")
        return self


def grounded_by(sources: Sequence[MemorySource]) -> bool:
    """Does this evidence still support a claim?

    **Pure, and derived rather than stored** — the same discipline as `MemoryClaim.is_superseded`,
    and for the same reason its docstring gives: writing the answer into a column destroys the
    record of what was true when. A stored `ungrounded` flag would also have to be recomputed on
    every tombstone, and the one that was missed would be invisible.

    An empty sequence is **grounded**, and that is not a special case being waved through: a claim
    with no citations is one whose provenance is `source_event_id`, and the event log is
    append-only with no delete path, so that evidence cannot become unavailable.

    One surviving source is enough. Three sources supporting a claim do not make it three times
    grounded, and erasing one of them does not make it two-thirds true.
    """
    return not sources or any(source.available for source in sources)


class SkillStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class Skill(Aggregate):
    """A learned, reusable procedure — master spec §23.

    Skills are stored canonically and *generated into* native worker formats, rather than
    being authored per-worker. That is what stops the same knowledge from forking into five
    incompatible copies.
    """

    skill_id: SkillId = Field(pattern=CONFIG_NAME_PATTERN)
    name: str
    description: str
    capabilities: list[str] = Field(default_factory=list[str])
    instructions: str = ""
    status: SkillStatus = SkillStatus.DRAFT

    derived_from_run_ids: list[RunId] = Field(default_factory=list[RunId])
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)

    @property
    def success_rate(self) -> float | None:
        total = self.success_count + self.failure_count
        return None if total == 0 else self.success_count / total


class Workflow(Aggregate):
    """A deterministic automation — master spec §10.4.

    The graduation path is trajectory -> skill -> workflow: once a procedure is stable enough
    to be deterministic, it stops paying for agent reasoning on every run.
    """

    workflow_id: WorkflowId = Field(pattern=CONFIG_NAME_PATTERN)
    name: str
    engine: str = "activepieces"
    external_flow_id: str | None = None
    capabilities: list[str] = Field(default_factory=list[str])
    graduated_from_skill_id: SkillId | None = None
    enabled: bool = True


class Trajectory(Aggregate):
    """A recorded execution path, mined for skills and evals — master spec §23."""

    task_id: TaskId
    run_ids: list[RunId] = Field(default_factory=list[RunId])
    steps: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    succeeded: bool = False
    human_interventions: int = Field(default=0, ge=0)
    promoted_to_skill_id: SkillId | None = None
