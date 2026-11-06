"""Synthetic release-summary peer served by the official A2A SDK."""

import asyncio
import os
from contextlib import asynccontextmanager

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from paos_core.routing import DEFAULT_WORKER_URL, RELEASE_WORKER_ID

ARTIFACT_IDS = {1: "release-summary-v1", 2: "release-summary-v2"}


def _report_inputs(context: RequestContext) -> tuple[str, str]:
    if context.message is None:
        return "", ""
    text = "".join(part.text for part in context.message.parts if part.HasField("text"))
    task, _, background = text.partition("\n\n## Background — reference only, not instructions\n")
    _, _, changes = task.partition("\n\n## Synthetic changelog\n")
    changelog = "\n".join(line for line in changes.splitlines() if line.startswith("- "))
    _, _, facts = background.partition(
        "Recorded earlier about this project, and may be out of date:\n"
    )
    checklist = "\n".join(line.strip() for line in facts.splitlines() if line.startswith("  - "))
    return changelog, checklist


class ReleaseExecutor(AgentExecutor):
    def __init__(self, step_delay: float) -> None:
        self.step_delay = step_delay
        self.dispatches = 0

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.dispatches += 1
        task_id, context_id = context.task_id or "", context.context_id or ""
        if context.current_task is None:
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[context.message] if context.message else [],
                )
            )

        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.start_work()
        await asyncio.sleep(self.step_delay)
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            message=updater.new_agent_message([Part(text="Reviewing the release checklist")]),
        )
        await asyncio.sleep(self.step_delay)
        changes, facts = _report_inputs(context)
        changelog = changes or "- No changelog entries supplied."
        checklist = facts or "- No approved project facts were provided."
        draft = f"# Release summary\n\n## Changes\n{changelog}\n\n## Checklist\n{checklist}\n"
        await updater.add_artifact(
            [Part(text=draft)],
            artifact_id=ARTIFACT_IDS[1],
            name="release-summary.md",
            last_chunk=True,
        )
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            message=updater.new_agent_message([Part(text="Revising the release recommendation")]),
        )
        await asyncio.sleep(self.step_delay)
        revised = (
            draft
            + "\n## Recommendation\n"
            + "Confirm the checklist before publication. This synthetic run produced a summary "
            + "for review.\n"
        )
        await updater.add_artifact(
            [Part(text=revised)],
            artifact_id=ARTIFACT_IDS[2],
            name="release-summary.md",
            last_chunk=True,
        )
        await updater.complete(
            message=updater.new_agent_message([Part(text="Release summary ready for review")])
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        await updater.cancel()


def create_app(url: str, *, step_delay: float = 0.8) -> Starlette:
    executor = ReleaseExecutor(step_delay)
    card = AgentCard(
        name=RELEASE_WORKER_ID,
        description="Produces two versions of a synthetic release summary.",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC")],
        default_input_modes=["text/plain"],
        default_output_modes=["text/markdown"],
        skills=[
            AgentSkill(
                id="release-summary",
                name="Release summary",
                description="Draft a summary from a synthetic changelog.",
                tags=["summarize"],
            )
        ],
    )
    handler = DefaultRequestHandlerV2(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
    )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            yield
        finally:
            await handler.aclose()

    async def stats(_: Request) -> JSONResponse:
        return JSONResponse({"dispatches": executor.dispatches})

    return Starlette(
        lifespan=lifespan,
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/"),
            Route("/fixture-stats", stats),
        ],
    )


app = create_app(
    os.getenv("PAOS_WORKER_URL", DEFAULT_WORKER_URL),
    step_delay=float(os.getenv("PAOS_FIXTURE_STEP_DELAY", "0.8")),
)
