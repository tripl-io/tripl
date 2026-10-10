"""What a warehouse driver's failure says, read without repeating it.

Driver exceptions name the host, the port and often the user, which only the
organization's owners and admins may see, so no user-facing message quotes a
failure tripl did not write. These readers name its kind instead, for the
caller to word:

* :func:`probe_failure_kind` reads a failure to connect, or of a connection
  probe. No statement of the user's ran, so a bare mention of a password or a
  permission can only be about the sign-in.
* :func:`statement_failure_kind` reads a failure while a user's statement ran.
  There the engine's own diagnosis ("permission denied for table orders",
  "Unrecognized name: author_id") is what the user needs to see, so only text
  no SQL diagnostic contains is matched, and ``None`` means "the engine's
  verdict: show it". Some drivers (Trino's, BigQuery's) connect on the first
  query, which is why the connect-shaped strings are read here too.

``worker.tasks._errors`` keeps lists of its own: they also decide which failed
alert deliveries are retried, a different question.
"""

from __future__ import annotations

from typing import Literal

FailureKind = Literal["auth", "timeout", "tls", "unreachable"]

_TIMEOUT_HINTS = ("timed out", "timeout")
# psycopg's refusal when the SSL mode demands TLS and the server offers none. It
# carries psycopg's "connection failed:" prefix too, so it is read before the
# unreachable hints: a fresh local PostgreSQL has no TLS, and "could not reach"
# sends the operator to check a network that works.
_TLS_HINTS = ("does not support ssl",)
# Text that only ever describes failing to reach the warehouse.
_UNREACHABLE_HINTS = (
    "connection refused",
    "connection reset",
    # psycopg: 'connection failed: connection to server at "<host>", port <port> failed: ...'
    "connection failed:",
    "could not connect",
    "could not translate host name",
    # snowflake-connector: "Failed to connect to DB: <host>:<port>. ..."
    "failed to connect to db",
    # urllib3, under the HTTP drivers (Trino, Databricks)
    "failed to establish a new connection",
    "max retries exceeded",
    "failed to resolve",
    "getaddrinfo",
    "host is unreachable",
    "name or service not known",
    "network is unreachable",
    "network unreachable",
    "no route to host",
)
# Sign-in refusals worded so that no SQL diagnostic contains them.
_AUTH_HINTS = (
    "access denied",
    "authentication failed",
    "incorrect username or password",
    "invalid credentials",
    "invalid_grant",
    "password authentication",
    "pg_hba.conf",
)
# A probe runs no statement of the user's, so broader words are safe there.
_PROBE_AUTH_HINTS = (*_AUTH_HINTS, "auth", "password", "credential", "permission")
_PROBE_UNREACHABLE_HINTS = (*_UNREACHABLE_HINTS, "refused", "connection failed")


def _kind(
    exc: BaseException, *, auth: tuple[str, ...], unreachable: tuple[str, ...]
) -> FailureKind | None:
    # Sign-in first: a refused password arrives inside a "connection failed"
    # or "failed to connect" sentence, and it is the password that needs fixing.
    text = str(exc).lower()
    if any(hint in text for hint in auth):
        return "auth"
    if any(hint in text for hint in _TIMEOUT_HINTS):
        return "timeout"
    if any(hint in text for hint in _TLS_HINTS):
        return "tls"
    if any(hint in text for hint in unreachable):
        return "unreachable"
    return None


def probe_failure_kind(exc: BaseException) -> FailureKind | None:
    """The kind of a failure to connect, or of a connection probe; None when unknown."""
    return _kind(exc, auth=_PROBE_AUTH_HINTS, unreachable=_PROBE_UNREACHABLE_HINTS)


def statement_failure_kind(exc: BaseException) -> FailureKind | None:
    """The kind of a failure while a user's statement ran; None for the engine's verdict."""
    return _kind(exc, auth=_AUTH_HINTS, unreachable=_UNREACHABLE_HINTS)
