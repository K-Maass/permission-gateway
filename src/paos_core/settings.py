"""Explicit runtime settings for the local services."""

import os


def database_url() -> str:
    dsn = os.environ.get("PAOS_DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("PAOS_DATABASE_URL is required")
    return dsn


def operator_token() -> str:
    token = os.environ.get("PAOS_OPERATOR_TOKEN", "")
    if (
        not 32 <= len(token) <= 256
        or not token.isascii()
        or not token.isprintable()
        or len(set(token)) < 8
        or any(char.isspace() for char in token)
    ):
        raise RuntimeError(
            "PAOS_OPERATOR_TOKEN must be a strong random token of at least 32 characters"
        )
    return token


def approval_seconds() -> int:
    try:
        seconds = int(os.environ.get("PAOS_APPROVAL_SECONDS", "300"))
    except ValueError:
        seconds = 0
    if not 1 <= seconds <= 86400:
        raise RuntimeError("PAOS_APPROVAL_SECONDS must be an integer between 1 and 86400")
    return seconds
