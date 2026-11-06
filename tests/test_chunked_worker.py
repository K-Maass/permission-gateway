import asyncio
import socket
from contextlib import aclosing, asynccontextmanager
from uuid import uuid4

import pytest
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from starlette.applications import Starlette

from paos_core.worker_client import run_release


class ArtifactExecutor(AgentExecutor):
    def __init__(self, *, chunked: bool, repeat: str = "") -> None:
        self.chunked = chunked
        self.repeat = repeat
        self.resume = asyncio.Event()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.start_work()
        await updater.add_artifact(
            [Part(text="draft")], artifact_id="release-summary-v1", last_chunk=not self.chunked
        )
        if self.chunked:
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                message=updater.new_agent_message([Part(text="prefix persisted")]),
            )
            await self.resume.wait()
            await updater.add_artifact(
                [Part(text=" complete")],
                artifact_id="release-summary-v1",
                append=True,
                last_chunk=True,
            )
        elif self.repeat:
            await updater.add_artifact(
                [Part(text=self.repeat)], artifact_id="release-summary-v1", last_chunk=True
            )
        await updater.add_artifact(
            [Part(text="revision")], artifact_id="release-summary-v2", last_chunk=True
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


@asynccontextmanager
async def serve_peer(executor):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        card = AgentCard(
            name="chunked-fixture",
            version="1.0.0",
            description="Exercises chunked and repeated artifacts.",
            capabilities=AgentCapabilities(streaming=True),
            supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC")],
        )
        handler = DefaultRequestHandlerV2(
            agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
        )

        @asynccontextmanager
        async def lifespan(_):
            try:
                yield
            finally:
                await handler.aclose()

        app = Starlette(
            routes=[
                *create_agent_card_routes(card),
                *create_jsonrpc_routes(handler, rpc_url="/"),
            ],
            lifespan=lifespan,
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
                yield url
        finally:
            executor.resume.set()
            server.should_exit = True
            await serving


def test_reattach_waits_for_complete_bytes_before_publishing_a_chunked_artifact():
    async def scenario():
        executor = ArtifactExecutor(chunked=True)
        run_id = uuid4()
        async with serve_peer(executor) as url:
            async with aclosing(
                run_release(url, run_id=run_id, objective="Release summary", facts=[])
            ) as initial:
                handle = await anext(initial)
                async for update in initial:
                    assert update.kind != "artifact"
                    if update.message == "prefix persisted":
                        break

            artifacts = {}
            async with aclosing(
                run_release(
                    url,
                    run_id=run_id,
                    objective="Release summary",
                    facts=[],
                    external_task_id=handle.external_task_id,
                    context_id=handle.context_id,
                )
            ) as resumed:
                snapshot = await anext(resumed)
                assert snapshot.kind == "progress"
                assert snapshot.message == "prefix persisted"
                executor.resume.set()
                async for update in resumed:
                    if update.kind == "artifact":
                        assert update.version not in artifacts
                        artifacts[update.version] = update.body
                assert update.kind == "completed"
            assert artifacts == {1: b"draft complete", 2: b"revision"}

    asyncio.run(scenario())


def test_finalized_version_with_changed_bytes_fails_the_run():
    async def scenario():
        async with (
            serve_peer(ArtifactExecutor(chunked=False, repeat="replaced")) as url,
            aclosing(
                run_release(url, run_id=uuid4(), objective="Release summary", facts=[])
            ) as updates,
        ):
            with pytest.raises(RuntimeError, match="changed after finalization"):
                async for update in updates:
                    assert update.kind != "completed"

    asyncio.run(scenario())


def test_identical_repeated_finalized_version_is_published_once():
    async def scenario():
        async with serve_peer(ArtifactExecutor(chunked=False, repeat="draft")) as url:
            updates = [
                update
                async for update in run_release(
                    url, run_id=uuid4(), objective="Release summary", facts=[]
                )
            ]
        assert updates[-1].kind == "completed"
        assert [(u.version, u.body) for u in updates if u.kind == "artifact"] == [
            (1, b"draft"),
            (2, b"revision"),
        ]

    asyncio.run(scenario())
