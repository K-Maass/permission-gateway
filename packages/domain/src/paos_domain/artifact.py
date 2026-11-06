"""Artifact, ArtifactVersion, Resource and ResourceLock — master spec §4.11/§4.14.

Artifact is logical and mutable; ArtifactVersion is immutable bytes. Every version records
the run that produced it and the artifacts it derived from, so lineage is answerable without
reconstructing history from logs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from paos_domain.base import Aggregate, DataClassification, PaosModel, utc_now
from paos_domain.ids import (
    ArtifactId,
    ArtifactVersionId,
    ResourceId,
    ResourceLockId,
    RunId,
    TaskId,
    WorkerId,
)


class ValidationStatus(StrEnum):
    UNVALIDATED = "UNVALIDATED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ValidatorResult(PaosModel):
    validator: str
    status: ValidationStatus
    detail: str = ""
    checked_at: datetime = Field(default_factory=utc_now)


class ArtifactVersion(Aggregate):
    """Immutable bytes. Never mutated in place — a change produces a new version."""

    @property
    def version_id(self) -> ArtifactVersionId:
        """Typed view of `id`. Identity lives in one field only."""
        return ArtifactVersionId(self.id)

    artifact_id: ArtifactId
    sequence: int = Field(ge=1)

    content_hash: str = Field(
        min_length=1,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="Content address; also the dedupe key. `sha256:<hex>` — see content_address()",
    )
    storage_uri: str
    size_bytes: int = Field(ge=0)
    mime_type: str

    produced_by_run_id: RunId | None = None
    produced_by_worker_id: WorkerId | None = None
    trace_id: str | None = None
    parent_version_ids: list[ArtifactVersionId] = Field(default_factory=list[ArtifactVersionId])
    derived_from_artifact_ids: list[ArtifactId] = Field(default_factory=list[ArtifactId])

    validator_results: list[ValidatorResult] = Field(default_factory=list[ValidatorResult])
    preview_uri: str | None = None

    @property
    def validation_status(self) -> ValidationStatus:
        if not self.validator_results:
            return ValidationStatus.UNVALIDATED
        if any(r.status is ValidationStatus.FAILED for r in self.validator_results):
            return ValidationStatus.FAILED
        if all(r.status is ValidationStatus.SKIPPED for r in self.validator_results):
            return ValidationStatus.SKIPPED
        return ValidationStatus.PASSED


def content_address(data: bytes) -> str:
    """The canonical content address for some bytes: `sha256:<hex>`.

    One definition, because `content_hash` is the **dedupe key** — `collect_output`
    discriminates artifact versions on it. Two encodings of the same bytes are two artifacts
    that should have been one, and nothing raises: the duplicate simply appears, looking like
    ordinary work.

    The prefix is not decoration. It names the algorithm, so the day a second one is needed the
    stored values remain unambiguous rather than requiring a length heuristic.
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


class Artifact(Aggregate):
    """The logical file — master spec §4.11."""

    @property
    def artifact_id(self) -> ArtifactId:
        """Typed view of `id`. Identity lives in one field only."""
        return ArtifactId(self.id)

    logical_name: str
    kind: str
    mime_type: str
    classification: DataClassification = DataClassification.PERSONAL

    current_version_id: ArtifactVersionId | None = None
    version_count: int = Field(default=0, ge=0)

    source_task_id: TaskId | None = None
    source_run_id: RunId | None = None
    source_worker_id: WorkerId | None = None

    # Where copies of this artifact were sent (a Telegram message, an email). External refs,
    # so deleting a delivery record never orphans the artifact.
    delivery_refs: list[str] = Field(default_factory=list[str])


class ResourceKind(StrEnum):
    GIT_BRANCH = "git_branch"
    WORKBOOK = "workbook"
    EMAIL_THREAD = "email_thread"
    CALENDAR_EVENT = "calendar_event"
    EXTERNAL_ACCOUNT = "external_account"
    FILE_DIRECTORY = "file_directory"
    BROWSER_SESSION = "browser_session"
    MACHINE_DESKTOP = "machine_desktop"


class Resource(Aggregate):
    """A stateful thing two workers must not edit at once — master spec §4.14."""

    resource_id: ResourceId
    kind: ResourceKind
    uri: str
    classification: DataClassification = DataClassification.PERSONAL


class ResourceLock(Aggregate):
    """Lease-based mutual exclusion, held by a Run.

    Lease-based rather than held-forever: a worker that dies must not deadlock the resource,
    so the lock expires and Restate's ResourceLock service re-grants it.
    """

    @property
    def lock_id(self) -> ResourceLockId:
        """Typed view of `id`. Identity lives in one field only."""
        return ResourceLockId(self.id)

    resource_id: ResourceId
    run_id: RunId
    task_id: TaskId

    acquired_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    released_at: datetime | None = None
    exclusive: bool = True

    @property
    def expired(self) -> bool:
        return utc_now() >= self.expires_at

    @property
    def held(self) -> bool:
        return self.released_at is None and not self.expired

    def conflicts_with(self, other: ResourceLock) -> bool:
        if self.lock_id == other.lock_id or self.resource_id != other.resource_id:
            return False
        if not (self.held and other.held):
            return False
        return self.exclusive or other.exclusive

    def renew(self, extension: timedelta) -> Self:
        if self.released_at is not None:
            raise ValueError("cannot renew a released lock")
        self.expires_at = max(self.expires_at, utc_now()) + extension
        self.touch()
        return self

    @model_validator(mode="after")
    def _expiry_after_acquisition(self) -> Self:
        if self.expires_at <= self.acquired_at:
            raise ValueError("ResourceLock expires_at must be after acquired_at")
        return self
