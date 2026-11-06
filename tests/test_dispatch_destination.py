"""An untrusted A2A card cannot change where PAOS sends the task."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from a2a.types import AgentCard, AgentInterface
from paos_domain.drivers import WorkerInvocation
from paos_domain.ids import RunId, TaskId
from paos_protocol.a2a import A2AWorkerAdapter
from paos_protocol.a2a.adapter import pin_to_endpoint
from paos_protocol.a2a.errors import A2AEndpointMismatchError

ENROLLED = "http://worker.example"
FOREIGN = "http://other.example"


@pytest.mark.parametrize("urls", [[FOREIGN], [ENROLLED, FOREIGN]])
def test_foreign_card_interface_causes_no_task_request(urls):
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        return httpx.Response(
            200,
            json={
                "name": "test-worker",
                "description": "Fixture",
                "version": "1",
                "capabilities": {"streaming": True},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "skills": [],
                "supportedInterfaces": [
                    {"url": url, "protocolBinding": "JSONRPC"} for url in urls
                ],
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            adapter = A2AWorkerAdapter("test-worker", ENROLLED, http=http)
            invocation = WorkerInvocation(
                run_id=RunId(uuid4()), task_id=TaskId(uuid4()), objective="Synthetic input"
            )
            with pytest.raises(A2AEndpointMismatchError):
                await adapter.invoke(invocation)

    asyncio.run(scenario())
    assert requests == [("GET", ENROLLED + "/.well-known/agent-card.json")]


def test_matching_card_origin_is_pinned_and_rpc_path_preserved():
    card = AgentCard(
        supported_interfaces=[
            AgentInterface(url="http://worker.example:80/rpc?version=1", protocol_binding="JSONRPC")
        ]
    )

    pin_to_endpoint(card, ENROLLED)

    assert card.supported_interfaces[0].url == ENROLLED + "/rpc?version=1"
