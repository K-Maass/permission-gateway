"""A2A client adapter — roadmap Phase 8.

Import surface is deliberately narrow: the kernel gets `A2AWorkerAdapter` and nothing from the
`a2a` SDK, which is the boundary `WorkerEvent`'s docstring asks for.
"""

from paos_protocol.a2a.adapter import A2AWorkerAdapter, RunHandle
from paos_protocol.a2a.cards import CachedCard, CardCache, normalize_capabilities, unmapped_tags
from paos_protocol.a2a.errors import (
    A2ADiscoveryError,
    A2AProtocolError,
    A2ARejectedError,
    A2ATransportError,
)
from paos_protocol.a2a.projection import render_invocation

__all__ = [
    "A2ADiscoveryError",
    "A2AProtocolError",
    "A2ARejectedError",
    "A2ATransportError",
    "A2AWorkerAdapter",
    "CachedCard",
    "CardCache",
    "RunHandle",
    "normalize_capabilities",
    "render_invocation",
    "unmapped_tags",
]
