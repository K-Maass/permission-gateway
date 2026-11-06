"""Values that must not print themselves.

**The problem this solves is not "remember to redact".** Agent Zero authenticates A2A with a
token in the *URL path* — `http://host:port/a2a/t-{TOKEN}` — so the credential is part of the
endpoint every layer handles: the adapter's error messages, httpx's own exception text, run
metadata, telemetry attributes, and any log line that mentions where a worker lives. Redacting at
each of those sites is a rule somebody has to keep applying, and the failure mode is silent: the
token appears in a log nobody reads until they do.

So the safe rendering is the default and revealing is the exception. `str()` and `repr()` are
redacted, which covers f-strings, `%s` formatting, `print`, logging, pydantic serialisation and
tracebacks. Getting the real value requires calling `.reveal()`, which is explicit, greppable, and
reviewable — the same shape as `approvals.resolution_for` being unable to express a standing
grant, rather than a convention asking people not to.
"""

from __future__ import annotations

from urllib.parse import urlsplit

REDACTED = "[redacted]"

MIN_REDACTABLE = 4
"""Below this a "secret" is short enough that redacting it would scrub unrelated text.

A two-character token would turn every incidental occurrence of those characters into
`[redacted]`, producing unreadable logs and — worse — hiding the fact that redaction is doing
something absurd. Refusing is better than silently mangling.
"""


class SecretTooShortError(ValueError):
    """A secret short enough that redacting it would corrupt surrounding text."""


class SecretUrl:
    """A URL carrying a credential in it, which renders redacted unless explicitly revealed.

    Equality and hashing are over the *revealed* value, so two of these compare as the URLs they
    are; only the rendering is redacted. That matters because these get used as cache keys.
    """

    __slots__ = ("_secret", "_url")

    def __init__(self, url: str, *, secret: str) -> None:
        if len(secret) < MIN_REDACTABLE:
            raise SecretTooShortError(
                f"refusing to treat a {len(secret)}-character value as a redactable secret: "
                f"scrubbing it would replace unrelated text (minimum {MIN_REDACTABLE})"
            )
        self._url = url
        self._secret = secret

    def reveal(self) -> str:
        """The real URL. Every call site is a deliberate decision to expose the credential."""
        return self._url

    def redact(self, text: str) -> str:
        """Scrub the secret from arbitrary text.

        For the values this type cannot control: httpx embeds the request URL in its exception
        messages, so wrapping one without passing it through here would defeat the whole point.
        """
        return text.replace(self._secret, REDACTED)

    @property
    def display(self) -> str:
        return self.redact(self._url)

    def __str__(self) -> str:
        return self.display

    def __repr__(self) -> str:
        return f"SecretUrl({self.display!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretUrl):
            return self._url == other._url
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._url)


def as_url(value: str | SecretUrl) -> str:
    """The revealed URL, whether or not it carries a secret.

    One helper so a call site that genuinely needs the real value reads the same either way, and
    so `.reveal()` does not have to be guarded by an isinstance check at every use.
    """
    return value.reveal() if isinstance(value, SecretUrl) else value


def display_url(value: str | SecretUrl) -> str:
    """The safe rendering, whether or not it carries a secret."""
    return str(value)


def scrub(value: str | SecretUrl, text: str) -> str:
    """Redact `value`'s secret from `text`, if it has one."""
    return value.redact(text) if isinstance(value, SecretUrl) else text


def origin_of(value: str | SecretUrl) -> tuple[str, str, int | None]:
    """`(scheme, host, port)` — the part of a URL that decides *where packets go*.

    Deliberately excludes the path. A destination differing only in path reaches the same process
    over the same connection; a destination differing in scheme, host or port does not, and that is
    the distinction every caller here is actually making.

    **Ports are normalised against the scheme**, so `http://h:80` and `http://h` compare equal. A
    comparison that missed that would reject a legitimate endpoint for being spelled differently,
    and a check that fires on correct input is one somebody eventually removes.

    Safe on a `SecretUrl`: Agent Zero carries its credential in the URL *path*, which is exactly
    what this drops — so the returned tuple never contains the secret and can be interpolated into
    an error message.
    """
    parsed = urlsplit(as_url(value))
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return (scheme, (parsed.hostname or "").lower(), port)


def same_origin(left: str | SecretUrl, right: str | SecretUrl) -> bool:
    """Whether two URLs address the same process. See `origin_of` for what that excludes."""
    return origin_of(left) == origin_of(right)


def cause_for(endpoint: str | SecretUrl, exc: BaseException) -> BaseException | None:
    """The exception to chain, or `None` when chaining it would leak a credential.

    A chained cause is rendered in full by `traceback.format_exception`, and httpx puts the
    request URL in its own message — so for an endpoint carrying a path token, the cause reprints
    the credential in every traceback of a connection failure, after the wrapper carefully
    redacted it.

    Nothing diagnostic is lost: the wrapper's message already carries the cause's text, scrubbed.
    Only the raw duplicate is dropped, and only for endpoints that have a secret to lose — a plain
    endpoint keeps its chain, because there the cause is useful and harmless.
    """
    return None if isinstance(endpoint, SecretUrl) else exc
