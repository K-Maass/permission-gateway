"""Agent Card discovery, caching, and capability normalization — roadmap 8.2.

A card is an agent's own claim about itself, fetched over the network from the agent. Two
consequences shape this file:

**It is untrusted input.** Skill names and tags are strings an agent author chose. They are
normalized into PAOS capabilities through an explicit table, never adopted verbatim, so a
remote agent cannot mint a capability the policy layer has never heard of — `configs/policies`
grants against capability names, and a worker that could name its own would be granting to
itself.

**It changes without warning.** An agent redeploys with new skills, so the cache has a TTL, and
a fetch failure serves the last good card rather than dropping the agent from routing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from a2a.client import A2ACardResolver
from a2a.types import AgentCard
from paos_domain.secrets import SecretUrl, as_url, cause_for, display_url, scrub

from paos_protocol.a2a.errors import A2ADiscoveryError

CAPABILITY_BY_TAG: dict[str, str] = {
    "code": "code.implement",
    "coding": "code.implement",
    "software": "code.implement",
    "review": "code.review",
    "test": "code.test",
    "testing": "code.test",
    "research": "research.web",
    "search": "research.web",
    "web": "research.web",
    "browse": "browse.web",
    "browser": "browse.web",
    "write": "write.longform",
    "writing": "write.longform",
    "summarize": "summarize.document",
    "summary": "summarize.document",
    "reason": "reason.general",
    "chat": "reason.general",
    "plan": "plan.decompose",
    "planning": "plan.decompose",
    "data": "data.analyze",
    "analysis": "data.analyze",
    "analyze": "data.analyze",
}
"""Agent-chosen tags mapped to PAOS capability names.

Deliberately small and explicit. The alternative — deriving a capability from the tag string —
would let any agent name its own capability just by tagging itself, and capability names are
what policy grants are written against.
"""

UNMAPPED_CAPABILITY = "reason.general"
"""What an agent with no recognizable tag is credited with.

The weakest capability in the vocabulary, on purpose: an agent whose skills this build does not
understand should be routable for general work and nothing more. Silently crediting it with
what its tags *sound* like is how an unreviewed agent ends up holding `code.implement`.
"""


def normalize_capabilities(card: AgentCard) -> list[str]:
    """PAOS capability names for what this agent *says* it can do.

    **Never authoritative for authorization or routing.** This is drift reporting, not a
    permission: what a worker may be asked to do comes from PAOS's curated
    `configs/workers/<id>.yaml`, and what it may do to the host comes from a verified runtime
    envelope (`kernel/readiness.py`). A card is the worker describing itself.

    Routing relies on the curated registry and verified runtime capabilities.

    Sorted and de-duplicated so a card that lists the same tag twice, or lists its skills in a
    different order after a redeploy, does not look like a different agent to the registry.
    """
    found: set[str] = set()
    for skill in card.skills:
        for tag in [*skill.tags, skill.id, skill.name]:
            mapped = CAPABILITY_BY_TAG.get(tag.strip().lower())
            if mapped:
                found.add(mapped)
    return sorted(found) if found else [UNMAPPED_CAPABILITY]


def unmapped_tags(card: AgentCard) -> list[str]:
    """Tags this build did not recognize.

    Surfaced rather than discarded: an agent advertising a genuinely new capability looks
    exactly like an agent advertising a typo, and only a human can tell them apart. Silently
    dropping both means the vocabulary never grows.
    """
    seen: set[str] = set()
    for skill in card.skills:
        for tag in skill.tags:
            cleaned = tag.strip().lower()
            if cleaned and cleaned not in CAPABILITY_BY_TAG:
                seen.add(cleaned)
    return sorted(seen)


@dataclass
class CachedCard:
    card: AgentCard
    fetched_at: float
    capabilities: list[str]
    unmapped: list[str] = field(default_factory=list[str])


class CardCache:
    """Cards by base URL, with a TTL and a last-good fallback.

    The clock is injected rather than read from `time`: cache expiry is exactly the kind of
    behaviour that otherwise gets tested with a `sleep`, and a test suite that sleeps is a test
    suite people stop running.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        http: httpx.AsyncClient | None = None,
        card_path: str | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._http = http
        self._card_path = card_path
        self._entries: dict[str, CachedCard] = {}

    async def get(self, base_url: str | SecretUrl, *, force: bool = False) -> CachedCard:
        now = self._clock()
        # Keyed on the revealed URL so two references to one agent share a cache entry; the
        # error path below still renders redacted.
        key = as_url(base_url)
        cached = self._entries.get(key)
        if cached and not force and now - cached.fetched_at < self._ttl:
            return cached

        try:
            card = await self._fetch(key)
        except Exception as exc:  # noqa: BLE001 — preserve the last good SDK discovery result
            if cached:
                # A stale card beats no agent. Dropping a known agent from routing because its
                # card endpoint blipped would turn a transient network fault into a capacity
                # loss, and the card describes what the agent *can* do — slow-changing by nature.
                return cached
            raise A2ADiscoveryError(
                f"no agent card at {display_url(base_url)}: {scrub(base_url, str(exc))}"
            ) from cause_for(base_url, exc)

        entry = CachedCard(
            card=card,
            fetched_at=now,
            capabilities=normalize_capabilities(card),
            unmapped=unmapped_tags(card),
        )
        self._entries[key] = entry
        return entry

    async def _fetch(self, base_url: str) -> AgentCard:
        """Fetch the card, from the path *this* agent serves it on.

        `A2ACardResolver` defaults to `/.well-known/agent-card.json`, the current A2A spelling.
        Older agents can declare a different discovery path in their worker record.

        `None` means the SDK default, so every worker that speaks the current spelling is untouched.
        """
        extra = {"agent_card_path": self._card_path} if self._card_path else {}
        client = self._http
        if client is not None:
            return await A2ACardResolver(client, base_url, **extra).get_agent_card()
        async with httpx.AsyncClient(timeout=15.0) as owned:
            return await A2ACardResolver(owned, base_url, **extra).get_agent_card()

    def peek(self, base_url: str | SecretUrl) -> CachedCard | None:
        """The cached entry without fetching — for diagnostics, not for the hot path."""
        return self._entries.get(as_url(base_url))


__all__ = [
    "CAPABILITY_BY_TAG",
    "UNMAPPED_CAPABILITY",
    "CachedCard",
    "CardCache",
    "normalize_capabilities",
    "unmapped_tags",
]
