"""Request-scoped organization context (F20 PR2, GH #273).

Every project lives in exactly one organization, and a project slug is only a
name inside that organization. So a slug is never resolved on its own: it is
resolved against the organization this request is acting in, which this module
carries from the one place that decides it to every place that needs it.

* The one binder is the auth dependency (:func:`tripl.api.deps.get_current_user`
  and ``_resolve_api_key_user``), which resolves the organization through
  :func:`tripl.services.org_resolution.resolve_request_org` BEFORE anything
  resolves a slug — the project-bound key fence included (critique #2).
* The readers are :mod:`tripl.services.project_lookup` (``resolve_project``,
  ``resolve_project_id``, ``project_slug_clause``) and whatever later needs the
  organization id itself.

An unbound organization is a programming error, not a default:
:func:`require_org_id` raises :class:`OrgContextMissing` (a 500), so a code path
that forgot to resolve one fails loudly instead of quietly reading the default
organization's projects. Celery tasks have no request organization and resolve
projects by id; a script that needs a slug lookup binds one with
:func:`bound_org`.

The fence between requests is :class:`tripl.middleware.org_path_rewrite.
OrgPathRewriteMiddleware`, which sets the variable to ``None`` on entry and
resets it on exit. That reset is load-bearing for the same reason as
:func:`tripl.middleware.branch_context.bound_branch`'s: the suite drives the app
through ``httpx.ASGITransport``, which awaits it in the CALLER's task, so an
unfenced ``set()`` would bleed one request's organization into the next — and a
test's own binding into a request that failed to resolve one.

The organization may also be named in the URL: ``/api/v1/orgs/{org}/<rest>`` is
rewritten to ``/api/v1/<rest>`` for the prefixes in :data:`ORG_REWRITE_PREFIXES`,
with the slug left in the ASGI scope state under :data:`ORG_SCOPE_STATE_KEY`
(:func:`path_org_slug` reads it back).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

from starlette.requests import HTTPConnection


@dataclass(frozen=True)
class OrgRef:
    """The organization a request acts in. Id and slug travel together.

    ``step_in_user_id`` is set when the caller acts in it through a platform
    admin's read-only step-in (F20 PR14) rather than a membership: that user's
    id. ``services.project_access`` reads it (:func:`stepped_in`) to answer
    organization role ``member`` and project role ``viewer``; ``api.deps``
    refuses every write under it.
    """

    id: uuid.UUID
    slug: str
    step_in_user_id: uuid.UUID | None = None


_org_var: ContextVar[OrgRef | None] = ContextVar("tripl_org", default=None)

#: The first path segment after ``/api/v1/`` that the org-qualified form of a
#: URL may carry. ``settings`` is deliberately absent — per-organization settings
#: get real routes of their own under ``/api/v1/orgs/{org}/settings`` — and so
#: are ``project-templates`` (instance-wide), ``auth`` (identity, not tenancy)
#: and ``orgs`` itself.
ORG_REWRITE_PREFIXES: tuple[str, ...] = (
    "projects",
    "activity",
    "audit",
    "data-sources",
    "users",
    "me",
)

#: Key in ``scope["state"]`` (so ``request.state``) holding the org slug taken
#: from an org-qualified URL.
ORG_SCOPE_STATE_KEY = "org_slug"
#: Key in ``scope["state"]`` holding the path as the client sent it.
ORG_ORIGINAL_PATH_STATE_KEY = "org_original_path"


class OrgContextMissing(RuntimeError):
    """A slug was resolved with no organization bound.

    A programming error — some path skipped org resolution — so it surfaces as a
    500 and is never answered with a default organization.
    """


def current_org() -> OrgRef | None:
    """The organization this request acts in, or ``None`` outside one."""
    return _org_var.get()


def current_org_id() -> uuid.UUID | None:
    ref = _org_var.get()
    return None if ref is None else ref.id


def stepped_in(user_id: uuid.UUID, org_id: uuid.UUID | None = None) -> bool:
    """Whether ``user_id`` acts in the bound organization through a step-in.

    ``org_id`` narrows it to that organization: a step-in never reaches another
    one, whatever the caller passes.
    """
    ref = _org_var.get()
    if ref is None or ref.step_in_user_id is None or ref.step_in_user_id != user_id:
        return False
    return org_id is None or ref.id == org_id


def require_org_id() -> uuid.UUID:
    """The bound organization's id; raises :class:`OrgContextMissing` if unbound."""
    ref = _org_var.get()
    if ref is None:
        raise OrgContextMissing(
            "No organization is bound: resolve one (api.deps) or bind one with bound_org()"
        )
    return ref.id


def bind_org(ref: OrgRef | None) -> Token[OrgRef | None]:
    """Bind ``ref`` for the current context; pair with :func:`reset_org`."""
    return _org_var.set(ref)


def reset_org(token: Token[OrgRef | None]) -> None:
    _org_var.reset(token)


@contextmanager
def bound_org(ref: OrgRef) -> Iterator[None]:
    """Bind the organization for the enclosed block, then unbind it.

    For scripts and tests; request code is bound by the auth dependency. Bare
    ``try``/``finally`` for the reason given in ``bound_branch``.
    """
    token = _org_var.set(ref)
    try:
        yield
    finally:
        _org_var.reset(token)


def path_org_slug(conn: HTTPConnection) -> str | None:
    """The org slug an org-qualified URL named, or ``None`` for a legacy URL."""
    state = conn.scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(ORG_SCOPE_STATE_KEY)
    return value if isinstance(value, str) else None
