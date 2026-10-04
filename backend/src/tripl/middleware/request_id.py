"""Per-request UUID propagated via header and contextvar.

Adds an inbound/outbound ``X-Request-ID`` header (configurable name) and makes
the value available to loggers via :func:`current_request_id`. If the client
sent a header value, we honor it after a light sanity check; otherwise we
generate a fresh UUID4.

The id is also mirrored onto the ASGI scope, for the one caller that runs after
the contextvar has been reset — see :func:`request_id_from_scope`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tripl.config import settings

_request_id_var: ContextVar[str | None] = ContextVar("tripl_request_id", default=None)

# Cap the inbound id to bound memory in pathological inputs and keep log lines tidy.
_MAX_LEN: Final[int] = 128

# The contextvar has a hard lifetime limit: it is reset in the ``finally`` below,
# and Starlette's ServerErrorMiddleware — which serves the app's catch-all
# ``Exception`` handler — wraps this middleware from the outside. So the handler
# for the one error class the request id exists to diagnose used to see None and
# log/echo the "-" placeholder. The ASGI scope is the one carrier
# that outlives the reset: it is the same dict object ServerErrorMiddleware
# builds its ``Request`` from, so the id is mirrored there on the way in and read
# back with :func:`request_id_from_scope`.
_SCOPE_KEY: Final[str] = "tripl_request_id"


def current_request_id() -> str | None:
    """Return the request id for the current async task, or None outside a request."""
    return _request_id_var.get()


def request_id_from_scope(scope: Scope) -> str | None:
    """Return the id stashed on an ASGI scope, or None if this middleware never ran.

    None is a real outcome, not just a type-checker concession: an exception
    raised by a middleware layered outside this one (CORS, Brotli) unwinds
    before the scope is ever stamped.
    """
    value = scope.get(_SCOPE_KEY)
    return value if isinstance(value, str) else None


@contextmanager
def bound_request_id(request_id: str) -> Iterator[None]:
    """Re-bind the contextvar for a block that runs outside the middleware.

    Needed because ``logging_config._RequestIDFilter`` stamps every record from
    :func:`current_request_id` and would overwrite an explicit
    ``extra={"request_id": ...}``. Binding the recovered id around the log call
    is therefore the only way to get the real id onto a log line emitted from
    ServerErrorMiddleware.
    """
    token = _request_id_var.set(request_id)
    try:
        yield
    finally:
        _request_id_var.reset(token)


def _is_safe_inbound(value: str) -> bool:
    if not value or len(value) > _MAX_LEN:
        return False
    return all(c.isalnum() or c in "-_." for c in value)


class RequestIDMiddleware:
    """Set/read ``X-Request-ID`` and bind it to a contextvar for the request scope."""

    def __init__(self, app: ASGIApp, header_name: str | None = None) -> None:
        self.app = app
        self.header_name = (header_name or settings.request_id_header).lower().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        inbound = headers.get(self.header_name, b"").decode("latin-1", errors="replace")
        request_id = inbound if _is_safe_inbound(inbound) else uuid.uuid4().hex

        scope[_SCOPE_KEY] = request_id
        token = _request_id_var.set(request_id)
        try:

            async def send_with_header(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers_list = list(message.get("headers") or [])
                    headers_list.append((self.header_name, request_id.encode()))
                    message["headers"] = headers_list
                await send(message)

            await self.app(scope, receive, send_with_header)
        finally:
            _request_id_var.reset(token)
