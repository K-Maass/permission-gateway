"""Adapter errors, typed so the kernel's existing taxonomy classifies them correctly.

`durable/errors.py` decides what Restate retries, and its rule is unforgiving: an unclassified
exception is retryable, so a permanent failure raised as a bare exception becomes an infinite
retry loop that presents as a *hang* rather than an error. That bug has been paid for once in
this project already.

`packages/protocol` cannot import the kernel — the dependency runs the other way — so these
inherit from the standard exceptions the kernel already classifies. `ConnectionError` and
`TimeoutError` are in its RETRYABLE tuple; `ValueError` is mapped to a terminal error. Choosing
a base class here *is* choosing Restate's retry behaviour, which is why each says which it is.
"""

from __future__ import annotations


class A2ADiscoveryError(ConnectionError):
    """No agent card could be fetched, and none was cached.

    Retryable: an agent that is deploying, or a DNS entry that has not propagated, resolves on
    its own. Restate's backoff is the right response.
    """


class A2ATransportError(ConnectionError):
    """The call to the agent failed at the transport level. Retryable, same reasoning."""


class A2AProtocolError(ValueError):
    """The agent answered, but not with something this protocol version can read.

    Terminal: retrying an unparseable response produces the same unparseable response. Whoever
    is on the other end needs fixing, and a hot loop against them helps nobody.
    """


class A2ARejectedError(ValueError):
    """The agent refused the work — bad request, unsupported mode, failed authorization.

    Terminal. Retrying a refusal is how a rate limit becomes an outage.
    """


class A2AEndpointMismatchError(ValueError):
    """An agent card named a destination other than the endpoint PAOS is configured to dispatch to.

    **Terminal, and deliberately not retried.** The other errors here describe an agent that is
    unwell; this one describes an agent that is *lying about where it lives*, and a retry loop
    against it is a retry loop that keeps asking an attacker where to send the next payload.

    The card is untrusted input — `cards.py` says so and already table-maps skill tags for exactly
    that reason. This covers the one field that decides where packets go.
    """


__all__ = [
    "A2ADiscoveryError",
    "A2AEndpointMismatchError",
    "A2AProtocolError",
    "A2ARejectedError",
    "A2ATransportError",
]
