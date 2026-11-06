"""The release fixture's two declared candidates and their recorded eligibility."""

import os
from typing import TypedDict
from uuid import UUID

from paos_domain import DataClassification, RouteCandidate, RouteMode, RoutePlan, Worker, WorkerKind
from paos_domain.ids import TaskId, WorkerId

from paos_core.memory import TASK_CLASSIFICATION

RELEASE_WORKER_ID = "release-worker"
DEFAULT_WORKER_URL = "http://127.0.0.1:9000"
_CAPABILITY = "summarize.document"


class RouteRuling(TypedDict):
    id: str
    reason: str


class RouteView(TypedDict):
    selected: RouteRuling
    excluded: list[RouteRuling]
    hard_constraints: dict[str, object]


def _workers(worker_url: str | None = None) -> tuple[Worker, Worker]:
    return (
        Worker(
            worker_id=WorkerId(RELEASE_WORKER_ID),
            kind=WorkerKind.A2A,
            role="Release summary",
            capabilities=[_CAPABILITY],
            allowed_data_classes=[DataClassification.PERSONAL],
            endpoint=worker_url or os.getenv("PAOS_WORKER_URL", DEFAULT_WORKER_URL),
            integrated=True,
        ),
        Worker(
            worker_id=WorkerId("public-preview-worker"),
            kind=WorkerKind.A2A,
            role="Public preview",
            capabilities=[_CAPABILITY],
            allowed_data_classes=[DataClassification.PUBLIC],
            endpoint=os.getenv("PAOS_PUBLIC_WORKER_URL", "http://127.0.0.1:9001"),
            integrated=True,
        ),
    )


def _plan(run_id: UUID, workers: tuple[Worker, Worker]) -> RoutePlan:
    candidates: list[RouteCandidate] = []
    for worker in workers:
        # Same hard constraints as the upstream PAOS registry search, before selection.
        reason = None
        if _CAPABILITY not in worker.capabilities:
            reason = f"missing capability {_CAPABILITY}"
        elif not worker.integrated or worker.dispatch_target is None:
            reason = "worker has no integrated dispatch target"
        elif not any(level.permits(TASK_CLASSIFICATION) for level in worker.allowed_data_classes):
            reason = f"cannot handle {TASK_CLASSIFICATION.value} data"
        candidates.append(
            RouteCandidate(
                worker_id=worker.worker_id, eliminated=reason is not None, elimination_reason=reason
            )
        )
    return RoutePlan(
        task_id=TaskId(run_id),
        mode=RouteMode.FAST,
        hard_constraints={
            "required_capabilities": [_CAPABILITY],
            "data_classification": TASK_CLASSIFICATION.value,
        },
        candidates=candidates,
        selected_worker_id=next((c.worker_id for c in candidates if not c.eliminated), None),
    )


def route_release(run_id: UUID | None = None) -> RouteView:
    plan = _plan(run_id or UUID(int=0), _workers())
    if plan.selected_worker_id is None:
        raise RuntimeError("No eligible release worker")
    return {
        "selected": {
            "id": plan.selected_worker_id,
            "reason": f"Supports {_CAPABILITY} and can handle {TASK_CLASSIFICATION.value} data",
        },
        "excluded": [
            {"id": candidate.worker_id, "reason": candidate.elimination_reason or ""}
            for candidate in plan.eliminated
        ],
        "hard_constraints": plan.hard_constraints,
    }


def dispatch_url(run_id: UUID, worker_url: str) -> str:
    """Resolve the eligible candidate before creating an HTTP client."""
    workers = _workers(worker_url)
    plan = _plan(run_id, workers)
    for worker in workers:
        if worker.worker_id == plan.selected_worker_id and (target := worker.dispatch_target):
            return target
    raise RuntimeError("No eligible release worker")
