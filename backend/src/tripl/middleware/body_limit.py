"""Bound request bodies before FastAPI parses them or checks dependencies."""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tripl import extensions
from tripl.config import settings
from tripl.services import event_photo_service

_PHOTO_UPLOAD = re.compile(r"^/api/v1/projects/[^/]+/events/[^/]+/photos/?$")
_MIB = 1024 * 1024


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        photo_upload = scope["method"] == "POST" and _PHOTO_UPLOAD.fullmatch(scope["path"])
        # A photo upload is held to the operator's file limit plus multipart
        # framing (the route applies the organization's own, lower one).
        limit = (
            event_photo_service.upload_body_limit_bytes()
            if photo_upload
            else settings.max_request_body_mb * _MIB
        )
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length", b"").strip()
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send)
            return

        # An SSE GET has no body and must reach the router immediately. Requests
        # that can carry a body are buffered only up to the cap *before* the
        # multipart/JSON parser runs. Raising from receive inside Starlette's
        # multipart parser turns the error into a 400 instead of our 413.
        if scope["method"] in {"GET", "HEAD", "OPTIONS"} and not (
            declared or b"transfer-encoding" in headers
        ):
            await self.app(scope, receive, send)
            return

        received = 0
        buffered: list[Message] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    await self._reject(scope, receive, send)
                    return
            buffered.append(message)
            if message["type"] != "http.request" or not message.get("more_body", False):
                break

        pending = iter(buffered)

        async def replay_receive() -> Message:
            try:
                return next(pending)
            except StopIteration:
                return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        detail = "Request body too large"
        # An extension may own the path's error format (SCIM: RFC 7644 §3.12).
        own = extensions.error_response(str(scope["path"]), "too_large", 413, detail)
        if own is not None:
            await own(scope, receive, send)
            return
        await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
