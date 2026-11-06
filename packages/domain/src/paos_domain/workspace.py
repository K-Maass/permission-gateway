"""Workspace and WorkspaceLease — master spec §4.9, roadmap Phase 2.3.

Any Run that mutates files, code, browser state or a desktop holds a lease on a workspace.
This exists before the first coding worker rather than alongside Phase 38's parallel
coordinator, because retrofitting isolation onto workers that assumed a shared checkout is
exactly the rework the dependency map is written to prevent.

The invariant `no two mutating Runs share a mutable view` is enforced in three places:
here (`conflicts_with`), in policy (`configs/policies/effects.rego` denies a mutating effect
without a lease), and in the database (a partial unique index in Phase 3).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from paos_domain.base import Aggregate, PaosModel, utc_now
from paos_domain.ids import RunId, WorkspaceId, WorkspaceLeaseId


class WorkspaceKind(StrEnum):
    REPOSITORY = "repository"
    ARTIFACT_STAGING = "artifact_staging"
    BROWSER_PROFILE = "browser_profile"
    DESKTOP_PROJECT = "desktop_project"
    REMOTE_VOLUME = "remote_volume"


class LeaseMode(StrEnum):
    READ_ONLY = "read_only"
    MUTABLE = "mutable"


class RepositoryView(PaosModel):
    """Git isolation metadata, carried from the first coding run — master spec §4.9.

    Recording `base_commit` is what makes a merge reviewable and a stale worktree
    detectable; recording `branch` is what keeps two agents off each other's history.
    """

    repository: str
    base_commit: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    merge_target: str | None = None
    dirty_state_policy: str = "reject"


class Workspace(Aggregate):
    """A logical mutable work area."""

    @property
    def workspace_id(self) -> WorkspaceId:
        """Typed view of `id`. Identity lives in one field only."""
        return WorkspaceId(self.id)

    kind: WorkspaceKind
    name: str
    root_uri: str | None = None
    repository: RepositoryView | None = None
    delete_after_terminal: bool = False

    @model_validator(mode="after")
    def _repository_workspaces_describe_a_repository(self) -> Self:
        if self.kind is WorkspaceKind.REPOSITORY and self.repository is None:
            raise ValueError("a repository workspace must carry RepositoryView metadata")
        return self


class WorkspaceLease(Aggregate):
    """One Run's view of one Workspace."""

    @property
    def lease_id(self) -> WorkspaceLeaseId:
        """Typed view of `id`. Identity lives in one field only."""
        return WorkspaceLeaseId(self.id)

    workspace_id: WorkspaceId
    run_id: RunId
    mode: LeaseMode = LeaseMode.READ_ONLY

    # The isolated view handed to this run — a worktree path, a browser profile directory, a
    # sandbox volume. Two mutable leases on one workspace must never share this.
    view_uri: str | None = None
    repository: RepositoryView | None = None

    acquired_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    released_at: datetime | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and utc_now() >= self.expires_at

    @property
    def active(self) -> bool:
        return self.released_at is None and not self.expired

    def conflicts_with(self, other: WorkspaceLease) -> bool:
        """True when both leases are live, mutable, and on the same workspace.

        Two readers never conflict; a reader and a writer do not conflict here because the
        reader gets a snapshot view. Two writers always do.
        """
        if self.lease_id == other.lease_id:
            return False
        if self.workspace_id != other.workspace_id:
            return False
        if not (self.active and other.active):
            return False
        return self.mode is LeaseMode.MUTABLE and other.mode is LeaseMode.MUTABLE

    @model_validator(mode="after")
    def _mutable_lease_has_an_isolated_view(self) -> Self:
        if self.mode is LeaseMode.MUTABLE and not (self.view_uri or self.repository):
            raise ValueError(
                "a mutable WorkspaceLease must name the isolated view it grants "
                "(view_uri or repository); otherwise runs would share a checkout"
            )
        return self
