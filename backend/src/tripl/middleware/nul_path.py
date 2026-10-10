"""Answer 404 to a request whose path holds a NUL, before anything routes it.

The server percent-decodes the path, so ``/api/v1/projects/a%00b/events``
reaches the app as a slug with a U+0000 in it, and Starlette's ``{slug}``
convertor (``[^/]+``) matches it. A path parameter goes into a query as is,
and PostgreSQL refuses a NUL in text: asyncpg raised inside the request and
the caller got a 500 — anonymously, on routes such as an organization's
single sign-on start. Query parameters are guarded per field
(``schemas.text_filters``); a path is checked here, once, for every route
an app or an extension mounts.

No route can name a path with a NUL in it, so the answer is the router's
own 404 for an unknown path. An extension that owns the path's error format
(SCIM) answers in it instead.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from tripl import extensions
from tripl.middleware.security_headers import build_security_headers

_DETAIL = "Not Found"


class NulPathMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or "\x00" not in scope["path"]:
            await self.app(scope, receive, send)
            return
        path = str(scope["path"])
        own = extensions.error_response(path, "http", 404, _DETAIL)
        # Answered outside SecurityHeadersMiddleware, so it attaches the same
        # headers itself, as the app's 500 handler does.
        if own is None:
            own = JSONResponse(
                {"detail": _DETAIL}, status_code=404, headers=build_security_headers()
            )
        await own(scope, receive, send)
