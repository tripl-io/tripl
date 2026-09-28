"""Append baseline security headers to every HTTP response.

The middleware never overrides headers a downstream handler has already set,
so an endpoint that needs a custom CSP or X-Frame-Options can opt out by
setting its own value.
"""

from __future__ import annotations

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tripl.config import settings
from tripl.middleware.org_context import ORG_SCOPE_STATE_KEY, current_org
from tripl.services import org_storage_csp

#: Where GCS signed and public photo URLs point.
GCS_IMAGE_ORIGIN = "https://storage.googleapis.com"
_IMG_SRC = "img-src 'self' data: blob:"
#: The organization an SPA or API URL names (``/o/{org}/...``, ``/api/v1/orgs/{org}/...``).
_ORG_IN_PATH = re.compile(r"^/(?:o|api/v1/orgs)/([^/]+)(?:/|$)")

# Applied when the API serves the SPA itself (serve_frontend) and no explicit
# content_security_policy is configured, so the consolidated single container
# keeps the policy the standalone static tier used to set. Tuned for a Vite
# React SPA (Radix/Tailwind/recharts/codemirror need inline styles; scripts are
# bundled and same-origin).
_DEFAULT_SPA_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; "
    "connect-src 'self'; frame-src https://www.figma.com https://embed.figma.com; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def build_security_headers(*, gcs_images: bool = False) -> dict[str, str]:
    """The baseline header set, resolved from the current settings.

    Public because :class:`SecurityHeadersMiddleware` is not the only writer:
    the app's catch-all ``Exception`` handler answers from Starlette's
    ServerErrorMiddleware, which sits *outside* the whole user middleware stack,
    so a 500 never passes back through this middleware and has to attach the
    same headers itself (tripl-qu9m). Two hand-maintained copies of the list is
    exactly the drift that bug was — so both callers read this one function.

    Returns an empty mapping when security headers are disabled, so a 500 always
    carries precisely what a 200 on the same instance would.

    ``gcs_images`` admits GCS photo URLs in the default policy for a request
    whose organization stores photos in a bucket of its own (F20 PR11); the
    operator's GCS backend admits them for every request, as before.
    """
    if not settings.security_headers_enabled:
        return {}
    headers = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "strict-origin-when-cross-origin",
        # No camera, microphone, geolocation, payment APIs.
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }
    csp = settings.content_security_policy or (_DEFAULT_SPA_CSP if settings.serve_frontend else "")
    if not settings.content_security_policy and (
        settings.photo_storage_backend == "gcs" or gcs_images
    ):
        csp = csp.replace(_IMG_SRC, f"{_IMG_SRC} {GCS_IMAGE_ORIGIN}")
    if csp:
        headers["content-security-policy"] = csp
    if settings.hsts_enabled:
        headers["strict-transport-security"] = (
            f"max-age={settings.hsts_max_age_seconds}; includeSubDomains"
        )
    return headers


def _encoded(headers: dict[str, str]) -> list[tuple[bytes, bytes]]:
    return [(k.encode(), v.encode()) for k, v in headers.items()]


def wants_gcs_images(scope: Scope) -> bool:
    """Whether this request's page may show photos from an organization's GCS bucket.

    An API request is answered for its organization: the one the auth
    dependency bound, else the one the URL names. A page of the SPA admits them
    when ANY organization has its own GCS storage: the SPA moves between
    organizations without reloading the document, and the document's policy is
    the one the browser applies to every image it later shows.
    """
    path = str(scope.get("path") or "")
    if not path.startswith("/api/"):
        return org_storage_csp.any_org_uses_gcs()
    bound = current_org()
    if bound is not None:
        return org_storage_csp.org_uses_gcs(org_id=bound.id)
    state = scope.get("state")
    slug = state.get(ORG_SCOPE_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(slug, str):
        match = _ORG_IN_PATH.match(path)
        slug = match.group(1) if match else None
    return slug is not None and org_storage_csp.org_uses_gcs(slug=slug)


class SecurityHeadersMiddleware:
    """Inject security headers into every ``http.response.start`` message."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Encode once at construction: the ASGI message carries raw bytes and
        # the values are settings-derived. Two variants, because whether GCS
        # photo URLs are admitted depends on the request's organization (F20
        # PR11); when both are the same (an explicit policy, no policy, or the
        # operator on GCS) nothing is decided per request.
        self._headers = _encoded(build_security_headers())
        self._gcs_headers = _encoded(build_security_headers(gcs_images=True))
        self._per_org = self._headers != self._gcs_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self._per_org:
            await org_storage_csp.refresh_if_stale()

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Decided at response time: the auth dependency has bound the
                # request's organization by then.
                chosen = (
                    self._gcs_headers
                    if self._per_org and wants_gcs_images(scope)
                    else self._headers
                )
                existing = message.get("headers") or []
                existing_names = {name.lower() for name, _ in existing}
                merged = list(existing)
                for name, value in chosen:
                    if name not in existing_names:
                        merged.append((name, value))
                message["headers"] = merged
            await send(message)

        await self.app(scope, receive, send_with_headers)
