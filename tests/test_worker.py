import asyncio
import socket
from contextlib import aclosing
from uuid import uuid4

import httpx
import pytest
import uvicorn


@pytest.fixture
def worker_socket():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        yield listener


@pytest.mark.parametrize("reattach_after_completion", [False, True])
def test_sdk_worker_resumes_existing_task_with_both_artifacts(
    worker_socket, reattach_after_completion
):
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types import GetTaskRequest, Role, TaskState

    from paos_core.memory import memory_view
    from paos_core.worker import create_app
    from paos_core.worker_client import run_release

    port = worker_socket.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    async def scenario():
        server = uvicorn.Server(uvicorn.Config(create_app(url, step_delay=0.25), log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[worker_socket]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)

            run_id = uuid4()
            facts = memory_view()["facts"]
            async with aclosing(
                run_release(
                    url,
                    run_id=run_id,
                    objective="Summarize\n\n## Synthetic changelog\n- Keep CSV headers in empty exports.",
                    facts=facts,
                )
            ) as updates:
                handle = await anext(updates)
                assert handle.kind == "handle"
                assert handle.external_task_id and handle.context_id
                progress = []
                async for update in updates:
                    if update.kind == "artifact":
                        assert update.version == 1
                        break
                    progress.append(update.kind)
                assert "progress" in progress

            async with httpx.AsyncClient() as http:
                card = await A2ACardResolver(http, url).get_agent_card()
                assert card.capabilities.streaming
                peer = ClientFactory(ClientConfig(httpx_client=http)).create(card)
                async with asyncio.timeout(5):
                    if reattach_after_completion:
                        while True:
                            snapshot = await peer.get_task(
                                GetTaskRequest(id=handle.external_task_id)
                            )
                            if snapshot.status.state == TaskState.TASK_STATE_COMPLETED:
                                break
                            await asyncio.sleep(0.01)

                    recovered = [
                        update
                        async for update in run_release(
                            url,
                            run_id=run_id,
                            objective="Reattachment must not send this",
                            facts=[],
                            external_task_id=handle.external_task_id,
                            context_id=handle.context_id,
                        )
                    ]
                    snapshot = await peer.get_task(
                        GetTaskRequest(id=handle.external_task_id, history_length=20)
                    )

                stats = await http.get(f"{url}/fixture-stats")
                assert stats.json() == {"dispatches": 1}

            assert recovered[-1].kind == "completed"
            artifacts = [(u.version, u.body) for u in recovered if u.kind == "artifact"]
            assert [version for version, _ in artifacts] == [1, 2]
            assert artifacts[0][1] != artifacts[1][1]
            assert b"Keep CSV headers in empty exports." in artifacts[1][1]
            assert b"accessibility review" in artifacts[1][1]
            received = [
                part.text
                for message in snapshot.history
                if message.role == Role.ROLE_USER
                for part in message.parts
                if part.HasField("text")
            ]
            assert len(received) == 1
            assert "accessibility review" in received[0]
            assert "canary rotation" not in received[0]
            assert "weekend rollout" not in received[0]
            assert "Reattachment must not send this" not in received[0]
        finally:
            server.should_exit = True
            await serving

    asyncio.run(scenario())
