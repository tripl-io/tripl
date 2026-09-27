"""Serve ``/api/v1/orgs/{org}/<rest>`` by the legacy ``/api/v1/<rest>`` routes.

Every project-scoped route is reachable under an org-qualified URL without a
second copy of the route table: this pure ASGI middleware rewrites the path
before routing and leaves the org slug in ``scope["state"]`` for the auth
dependency, which resolves and checks it (see
:mod:`tripl.middleware.org_context`). OpenAPI and the route count are unchanged;
the rewrite is transparent.

Only the prefixes in :data:`~tripl.middleware.org_context.ORG_REWRITE_PREFIXES`
are rewritten. Anything else under ``/api/v1/orgs/`` — ``settings`` and any real
``/orgs`` route added later — passes through untouched and gets the router's
own answer.

The org is deliberately NOT checked here: an unknown slug answers 404 from the
auth dependency after authentication, so an anonymous caller gets 401 like on
any other protected route and cannot probe which organizations exist.

Registered between ``BodyLimitMiddleware`` and ``CORSMiddleware`` in
``tripl.main`` so that BodyLimit's upload-path match and Brotli's
``excluded_handlers`` (the SSE stream) both see the rewritten path.
"""

from __future__ import annotations

import re

from starlette.types import ASGIApp, Receive, Scope, Send

from tripl.middleware.org_context import (
    ORG_ORIGINAL_PATH_STATE_KEY,
    ORG_REWRITE_PREFIXES,
    ORG_SCOPE_STATE_KEY,
    _org_var,
)

_PREFIX_ALTERNATION = "|".join(re.escape(p) for p in ORG_REWRITE_PREFIXES)
_ORG_PATH = re.compile(
    rf"^/api/v1/orgs/(?P<org>[^/]+)/(?P<rest>(?:{_PREFIX_ALTERNATION})(?:/.*)?)$",
    re.DOTALL,
)
_ORG_RAW_PATH = re.compile(
    rf"^/api/v1/orgs/(?P<org>[^/]+)/(?P<rest>(?:{_PREFIX_ALTERNATION})(?:/.*)?)$".encode(),
    re.DOTALL,
)


def rewrite_org_path(path: str) -> tuple[str, str] | None:
    """``(org_slug, legacy_path)`` for an org-qualified path, else ``None``."""
    match = _ORG_PATH.match(path)
    if match is None:
        return None
    return match["org"], f"/api/v1/{match['rest']}"


class OrgPathRewriteMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        # Fence the organization per request: nothing bound before this request
        # (a test's own binding, a previous request on the same task) is visible
        # inside it, and nothing bound inside it outlives it.
        token = _org_var.set(None)
        try:
            if scope["type"] == "http":
                scope = _rewritten(scope)
            await self.app(scope, receive, send)
        finally:
            _org_var.reset(token)


def _rewritten(scope: Scope) -> Scope:
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "") or ""
    # Starlette keeps root_path inside ``path`` when mounted under one.
    base = root_path if root_path and path.startswith(root_path) else ""
    rewritten = rewrite_org_path(path[len(base) :])
    if rewritten is None:
        return scope
    org_slug, legacy = rewritten
    new_scope = dict(scope)
    new_scope["path"] = base + legacy
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes):
        raw_base = base.encode()
        raw_tail = raw_path[len(raw_base) :] if raw_path.startswith(raw_base) else raw_path
        raw_match = _ORG_RAW_PATH.match(raw_tail)
        new_scope["raw_path"] = (
            raw_base + b"/api/v1/" + raw_match["rest"]
            if raw_match is not None
            else new_scope["path"].encode()
        )
    state = dict(scope.get("state") or {})
    state[ORG_SCOPE_STATE_KEY] = org_slug
    state[ORG_ORIGINAL_PATH_STATE_KEY] = path
    new_scope["state"] = state
    return new_scope
