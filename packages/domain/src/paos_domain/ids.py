"""Canonical identifiers — master spec §4.1.

UUIDv7 (RFC 9562) so ids sort by creation time, which makes event streams, run histories and
paginated task lists naturally ordered without a secondary index.

Generation is implemented here rather than pulled from a dependency: it is fifteen lines, it
sits underneath every other package, and a foundational contract should not acquire a
supply-chain edge for something this small.

Each aggregate gets a distinct `NewType`. In a model with thirty entity types, passing a
`RunId` where a `TaskId` belongs is the most likely mistake anyone will make, and the type
checker should be the one to catch it rather than a foreign-key violation at 2am.
"""

from __future__ import annotations

import secrets
import time
from typing import Final, NewType
from uuid import UUID

_UNIX_TS_MS_BITS: Final = 48
_VERSION: Final = 7
_VARIANT: Final = 0b10


def uuid7(timestamp_ms: int | None = None) -> UUID:
    """Generate a UUIDv7.

    Layout (most significant bit first): 48-bit millisecond timestamp, 4-bit version,
    12 bits of randomness, 2-bit variant, 62 bits of randomness.

    `timestamp_ms` is injectable so tests can assert ordering deterministically.
    """
    ts = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
    rand = secrets.randbits(74)
    rand_a = rand >> 62
    rand_b = rand & ((1 << 62) - 1)

    value = (ts & ((1 << _UNIX_TS_MS_BITS) - 1)) << 80
    value |= _VERSION << 76
    value |= rand_a << 64
    value |= _VARIANT << 62
    value |= rand_b
    return UUID(int=value)


def uuid7_timestamp_ms(value: UUID) -> int:
    """Recover the millisecond timestamp embedded in a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"{value} is not a UUIDv7 (version={value.version})")
    return value.int >> 80


# --- typed identifiers ---------------------------------------------------------

PrincipalId = NewType("PrincipalId", UUID)
ProjectId = NewType("ProjectId", UUID)
ConversationId = NewType("ConversationId", UUID)
TaskId = NewType("TaskId", UUID)
RunId = NewType("RunId", UUID)
RoutePlanId = NewType("RoutePlanId", UUID)

WorkerId = NewType("WorkerId", str)
RuntimeId = NewType("RuntimeId", UUID)
MachineId = NewType("MachineId", str)
ExecutionProviderId = NewType("ExecutionProviderId", str)
WorkerPoolId = NewType("WorkerPoolId", str)
RuntimeLeaseId = NewType("RuntimeLeaseId", UUID)

WorkspaceId = NewType("WorkspaceId", UUID)
WorkspaceLeaseId = NewType("WorkspaceLeaseId", UUID)

CredentialRefId = NewType("CredentialRefId", str)
CredentialGrantId = NewType("CredentialGrantId", UUID)

TriggerId = NewType("TriggerId", UUID)
TriggerSubscriptionId = NewType("TriggerSubscriptionId", UUID)
ReturnRouteId = NewType("ReturnRouteId", UUID)
NotificationId = NewType("NotificationId", UUID)

ArtifactId = NewType("ArtifactId", UUID)
ArtifactVersionId = NewType("ArtifactVersionId", UUID)
EffectId = NewType("EffectId", UUID)
ApprovalId = NewType("ApprovalId", UUID)
ResourceId = NewType("ResourceId", str)
ResourceLockId = NewType("ResourceLockId", UUID)

MemoryClaimId = NewType("MemoryClaimId", UUID)
# One piece of evidence a claim can cite — H2. Distinct from MemoryClaimId because a source
# outlives the claims that cite it and is cited by many: passing one where the other belongs
# is the error a NewType exists to make unspellable.
MemorySourceId = NewType("MemorySourceId", UUID)
# A thing in the operator's world that several names refer to — H2 Stage 5. The only claim
# subject that is not a PAOS record, which is why it is governed rather than minted.
EntityId = NewType("EntityId", UUID)
EvaluationId = NewType("EvaluationId", UUID)
EvalCaseId = NewType("EvalCaseId", str)
EvalResultId = NewType("EvalResultId", UUID)

ModelEndpointId = NewType("ModelEndpointId", str)
MCPServerId = NewType("MCPServerId", str)
ToolId = NewType("ToolId", str)
SkillId = NewType("SkillId", str)
WorkflowId = NewType("WorkflowId", str)

EventId = NewType("EventId", UUID)
CostRecordId = NewType("CostRecordId", UUID)
UpstreamComponentId = NewType("UpstreamComponentId", str)
SandboxProfileId = NewType("SandboxProfileId", str)


# Identifiers that are strings rather than UUIDs are *stable configuration names*
# (`local-worker`, `local-docker`, `reason.general`). They come from configs the operator
# writes and must stay readable in policy, routes and logs. Everything created at runtime
# gets a UUIDv7.
CAPABILITY_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"
CONFIG_NAME_PATTERN: Final = r"^[a-z][a-z0-9-]*$"

# What a MemoryClaim may be *about* — master spec §4.15, roadmap §19.
#
# Constrained rather than free text, and the reason is retrieval rather than tidiness. Phase 19
# ships no embeddings, so finding a claim is an index scan on `(subject, predicate)`. A subject
# nobody can spell twice is stored, costs nothing to write, and is never found again — it fails
# *silently*, which is the worst way for a memory system to fail. Enforced here and as a database
# CHECK: a validator protects one process, a constraint protects the data.
#
# `entity` arrived with H2 Stage 5 and is the only kind that is not a PAOS record: the others name
# a row the kernel created, while an entity names a *thing in the operator's world* — a person, an
# organisation, a product — that documents refer to by several names. It is governed like a claim
# precisely because nothing else vouches for it.
CLAIM_SUBJECT_KINDS: Final = (
    "project",
    "task",
    "run",
    "worker",
    "artifact",
    "conversation",
    "entity",
)
# **Derived, not transcribed.** These two were written side by side and kept in step by hand, which
# is the shape of every drift this project has found: adding a kind to the tuple and forgetting the
# pattern would admit a subject the database then refuses, with the failure landing at the write
# rather than at the edit. The database CHECK is still spelled out in its own migration, because a
# constraint protects the data where a validator protects one process.
CLAIM_SUBJECT_PATTERN: Final = rf"^({'|'.join(CLAIM_SUBJECT_KINDS)}):[a-z0-9-]+$"
