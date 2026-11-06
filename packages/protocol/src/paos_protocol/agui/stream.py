"""SSE framing and reconnect for the AG-UI projection.

Two decisions live here, and both were made against the *installed* client rather than the
protocol's prose.

**1. One SSE frame per PAOS `events.seq`, carrying a batch — inside a PAOS envelope.**

The projection is one-to-many: `task.submitted` becomes a `RUN_STARTED` and a `STATE_DELTA`.
SSE offers no transactional multi-frame delivery, so emitting those as separate frames means a
connection dying between them leaves the browser holding half a projection — and giving both
frames `id: 417` makes `Last-Event-ID` ambiguous, since the browser can report a seq it did not
fully receive. One frame per source event makes the atomicity boundary real: parsing one
complete SSE event yields the whole batch, and a frame torn mid-flight was never committed, so
the client resumes from the previous seq.

That batch is **not** standard AG-UI on the wire. `@ag-ui/client` parses each `data:` payload
with `EventSchemas`, a `z.discriminatedUnion("type", ...)` over single events, and errors the
whole stream when validation fails — an array has no `type`. So this endpoint speaks **AG-UI
event semantics over PAOS transport framing**, and is deliberately not described as a standard
AG-UI wire endpoint. The browser unwraps the envelope in a thin transport adapter before events
reach any AG-UI machinery; an external AG-UI client would need the same adapter.

**2. Reconnect resumes after the snapshot's high-water mark, not the client's cursor.**

The obvious contract — snapshot current truth, then replay from the client's last seq — corrupts
rather than errors. If the browser last committed 417, disconnects, and PAOS advances to 425, a
current-truth snapshot *already contains* 418-425; replaying them applies those changes twice.
Some JSON patches are harmlessly idempotent, some produce wrong state, and some no longer apply
at all. So the snapshot carries its own high-water mark and the resume keys off that.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ag_ui.core import BaseEvent, StateSnapshotEvent

SSE_EVENT_NAME = "paos.agui"
"""The single SSE `event:` name.

One name rather than distinguishing snapshot frames from delta frames: the envelope already
says which is which by its contents, and a client that had to register two listeners would have
two code paths where the protocol has one.
"""


class EventReader(Protocol):
    """How the caller supplies events. Keeps `paos_db` out of this package."""

    def latest_seq(self) -> int: ...

    def since(self, after_seq: int, limit: int) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class Envelope:
    """One source event's projection, addressed by the seq it came from.

    `seq` is the durable position and the SSE frame id. Everything about resuming this stream
    reduces to that one integer, which is why it is the envelope's first field rather than
    something derivable from the events inside.
    """

    seq: int
    events: list[BaseEvent]

    def to_wire(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "events": [event.model_dump(by_alias=True, exclude_none=True) for event in self.events],
        }

    def to_frame(self) -> str:
        """The SSE frame.

        `id:` carries the seq so a reconnect's `Last-Event-ID` is exact, and the frame is only
        committed by the browser once its terminating blank line arrives — which is the whole
        reason the batch is one frame.
        """
        body = json.dumps(self.to_wire(), separators=(",", ":"))
        return f"id: {self.seq}\nevent: {SSE_EVENT_NAME}\ndata: {body}\n\n"


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """What a reconnecting client needs, decided before anything is sent.

    Computed as a value rather than performed inline so the decision is testable on its own —
    the double-apply bug this design exists to prevent lives entirely in getting this wrong.
    """

    needs_snapshot: bool
    base_seq: int
    """The high-water mark. The snapshot describes state *through* this seq, and streaming
    starts strictly after it — never from the client's older cursor."""

    reason: str


def plan_resume(last_seen_seq: int | None, high_water_seq: int) -> ResumePlan:
    """Decide how to bring a client up to date.

    `last_seen_seq` is advisory. It cannot be used as the resume point when a snapshot is sent,
    because the snapshot already incorporates everything up to the high-water mark; its only job
    is to let a brief blip skip the snapshot entirely.
    """
    if last_seen_seq is None:
        # A fresh connection wants what happens next plus a picture of now — not a replay of
        # months of history to render one screen.
        return ResumePlan(True, high_water_seq, "first connection")
    if last_seen_seq >= high_water_seq:
        # Nothing was missed. The common case for a phone that dropped WiFi for a second, and
        # the one case where the client's own cursor is the correct resume point.
        return ResumePlan(False, last_seen_seq, "caught up — no snapshot needed")
    return ResumePlan(
        True,
        high_water_seq,
        f"missed {high_water_seq - last_seen_seq} event(s) — snapshot then resume after"
        f" {high_water_seq}",
    )


def snapshot_envelope(base_seq: int, document: dict[str, Any]) -> Envelope:
    """The snapshot frame, tagged with the seq it describes state through."""
    return Envelope(seq=base_seq, events=[StateSnapshotEvent(snapshot=document)])


def cursor_comment(plan: ResumePlan) -> str:
    """An SSE comment recording the resume decision.

    A comment rather than a frame: it must not look like an event to the client, and it keeps
    the connection's opening decision visible in a network log — where a stream that resumed
    from the wrong place is otherwise invisible until the state is already wrong.
    """
    return f": resume base_seq={plan.base_seq} snapshot={plan.needs_snapshot} {plan.reason}\n\n"


__all__ = [
    "SSE_EVENT_NAME",
    "Envelope",
    "EventReader",
    "ResumePlan",
    "cursor_comment",
    "plan_resume",
    "snapshot_envelope",
]
