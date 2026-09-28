import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.dependencies.models import Dependant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.database import get_session
from tripl.middleware.branch_context import bound_branch
from tripl.middleware.org_context import (
    OrgRef,
    bind_org,
    current_org,
    current_org_id,
    path_org_slug,
    require_org_id,
)
from tripl.models.domain_enums import OrganizationRole
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import (
    api_key_service,
    email_verification_service,
    org_service,
    project_access,
    project_service,
)
from tripl.services._plan_branch_locks import (
    hold_branch_for_plan_write,
    hold_main_plan_for_write,
    locks_rows,
)
from tripl.services.auth_service import get_user_by_session_token
from tripl.services.org_resolution import ORG_NOT_FOUND, resolve_request_org, suspended_error
from tripl.services.project_lookup import (
    PROJECT_NOT_FOUND,
    project_slug_clause,
    resolve_project_id,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _resolve_api_key_user(request: Request, session: AsyncSession) -> User | None:
    """Resolve ``Authorization: Bearer <token>`` to a User.

    Returns ``None`` when no Bearer header is present (caller falls back to
    cookie auth). Raises 401 on a malformed / revoked / expired token so we
    don't silently downgrade an explicit-but-bad token to anonymous.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    raw_token = auth.removeprefix("Bearer ").strip()
    if not raw_token:
        return None
    api_key = await api_key_service.verify_and_touch(session, raw_token)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
        )
    key_user = await session.get(User, api_key.user_id)
    if key_user is not None:
        _refuse_unverified(request, key_user)
    # Stash on request.state so role/scope checks downstream can tell whether
    # the caller is a session user or an API-key client.
    request.state.api_key_scope = api_key.scope
    request.state.api_key_project_id = api_key.project_id
    request.state.api_key_org_id = api_key.organization_id
    user = key_user
    if user is not None:
        # The key's organization, bound before get_current_user runs the
        # project-bound key fence: that fence resolves a slug (critique #2).
        await _bind_request_org(request, session, user, key_org_id=api_key.organization_id)
    return user


#: Routes that act in no organization, so a cookie session skips org resolution
#: there. Identity routes: signing out or reading one's own account must work for
#: a hosted user who belongs to several organizations (or none). Instance-wide
#: routes (``/settings``, ``/project-templates``) have no org-qualified form
#: (they are not in ``ORG_REWRITE_PREFIXES``), so resolving an org for them
#: would lock that same user out with a permanent 400. None of them resolves a
#: project slug. An API key still binds its own organization everywhere.
_ORG_FREE_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v1/auth/",
    "/api/v1/settings",
    # The operator console (F20 PR9): instance-wide, no organization.
    "/api/v1/platform",
    "/api/v1/project-templates",
    # The organization management API (F20 PR6): ``GET/POST /orgs`` act in no
    # organization, and ``/orgs/{org}/...`` resolves the organization its path
    # names through its own gate (:func:`_resolve_path_org`), which holds a
    # session to a membership of it. Org-qualified project URLs are rewritten to
    # their legacy path before routing, so they never match this prefix.
    "/api/v1/orgs",
)


def _app_path(request: Request) -> str:
    """The routed path without any ``root_path`` prefix."""
    path: str = request.scope.get("path", "")
    root_path: str = request.scope.get("root_path", "") or ""
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :]
    return path


#: What an account with an unverified address may still reach on a hosted
#: instance: identity routes — who am I, sign out, verify/resend, preview an
#: invitation.
_UNVERIFIED_ALLOWED_PREFIX = "/api/v1/auth/"


def _refuse_unverified(request: Request, user: User) -> None:
    """The hosted email-verification gate (F20 hosted sign-up).

    Enforced only when ``DEPLOYMENT_MODE=hosted``: an account that has not
    verified its address gets 403 on every authenticated route outside
    ``/api/v1/auth/``. Checked before the organization is resolved, so the
    answer never depends on the account's memberships. An API key cannot be
    minted behind this gate; it is refused all the same, in case one exists.
    """
    if not email_verification_service.is_blocked(user):
        return
    if _app_path(request).startswith(_UNVERIFIED_ALLOWED_PREFIX):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=email_verification_service.EMAIL_NOT_VERIFIED_MESSAGE,
    )


def _is_org_free(request: Request) -> bool:
    path = _app_path(request)
    return any(
        path == prefix or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
        for prefix in _ORG_FREE_PATH_PREFIXES
    )


async def _bind_request_org(
    request: Request,
    session: AsyncSession,
    user: User,
    *,
    key_org_id: uuid.UUID | None,
) -> None:
    """Resolve the request's organization and bind it for the rest of the request.

    No reset here: the request's fence is ``OrgPathRewriteMiddleware``, which
    unbinds whatever this bound when the request ends
    (:mod:`tripl.middleware.org_context`).
    """
    if key_org_id is None and _is_org_free(request):
        return
    org = await resolve_request_org(
        session,
        user=user,
        key_org_id=key_org_id,
        path_org_slug=path_org_slug(request),
    )
    refuse_step_in_writes(request, org)
    bind_org(org)


STEP_IN_READ_ONLY = "Step-in is read-only"

#: The two read-shaped POSTs a step-in may send (F20 PR14, owner decision):
#: the signal and window-metric batches carry their ids in a body because they
#: outgrow a query string, and write nothing. Every other non-GET/HEAD/OPTIONS
#: request in a stepped-in organization is refused. Named by handler for the
#: reason given at ``_DERIVED_DATA_HANDLERS``; test_platform_step_in fails if a
#: name stops matching a route.
STEP_IN_READ_HANDLERS = frozenset(
    {
        "tripl.api.v1.metrics.query_active_signals",
        "tripl.api.v1.metrics.get_events_window_metrics",
    }
)


def _handler_of(request: Request) -> str:
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    return f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__qualname__', '')}"


def refuse_step_in_writes(request: Request, org: OrgRef) -> None:
    """403 "Step-in is read-only" for a write in an organization bound through a step-in.

    The fence of the read-only step-in (F20 PR14): keyed on the method, not on
    the route's gates, so a route that forgot its write gate is closed too. The
    only exceptions are :data:`STEP_IN_READ_HANDLERS`. Org settings, member
    management, API keys and deletion are all writes, so a step-in never
    reaches them; their reads still take an owner/admin role, which a step-in
    (organization role ``member``) does not hold.
    """
    if org.step_in_user_id is None or request.method in _SAFE_METHODS:
        return
    if _handler_of(request) in STEP_IN_READ_HANDLERS:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=STEP_IN_READ_ONLY)


async def _ensure_request_org(request: Request, session: AsyncSession, user: User) -> None:
    """Bind the organization when authentication did not, e.g. an overridden user dependency.

    ``get_current_user`` binds it on every real request; this keeps the project
    gates correct when the user comes from somewhere else (a test override, a
    route mounted with its own user dependency) instead of failing with
    ``OrgContextMissing``. The key's organization is only known to the key
    path, so a missing binding is resolved as a cookie session would be.
    """
    if current_org() is None:
        await _bind_request_org(request, session, user, key_org_id=None)


async def _enforce_project_scope(
    request: Request, session: AsyncSession, project_id: uuid.UUID
) -> None:
    """A project-bound API key may only touch its own ``/projects/{slug}/...``.

    Routes without a ``slug`` path param (``/me/...``, ``/users``, ...) are
    off-limits to a project-scoped key — it exists to fence an agent into one
    project, so anything instance-wide is rejected rather than silently allowed.

    Another project's slug answers the same 404 "Project not found" an unknown
    slug gets, never a 403: a fenced key must not be an oracle for which slugs
    exist on the instance (the membership gate holds session users to the same
    rule).
    """
    slug = request.path_params.get("slug")
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is scoped to a single project",
        )
    # An unknown slug 404s inside the lookup with the same detail.
    if await resolve_project_id(session, slug) != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PROJECT_NOT_FOUND)


async def get_current_user(request: Request, session: SessionDep) -> User:
    # Bearer first — agents shouldn't need to send cookies.
    api_user = await _resolve_api_key_user(request, session)
    if api_user is not None:
        project_id = getattr(request.state, "api_key_project_id", None)
        if project_id is not None:
            await _enforce_project_scope(request, session, project_id)
        return api_user

    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    user = await get_user_by_session_token(session, session_token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    _refuse_unverified(request, user)
    await _bind_request_org(request, session, user, key_org_id=None)
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


_ORG_ROLE_UNSET = object()

#: 403 detail of every organization owner/admin gate.
ORG_ADMIN_REQUIRED = "Organization owner or admin role required"
ORG_MEMBERSHIP_REQUIRED = "Organization membership required"
PLATFORM_ADMIN_REQUIRED = "Platform admin required"


async def request_org_role(
    request: Request, session: AsyncSession, user: User
) -> OrganizationRole | None:
    """The caller's role in the request's bound organization; ``None`` for a non-member.

    Also ``None`` on an org-free route (``/settings``, ``/auth/...``) where a
    cookie session binds no organization. Cached on ``request.state.org_role``
    so the gates of one request query it once.
    """
    cached = getattr(request.state, "org_role", _ORG_ROLE_UNSET)
    if cached is not _ORG_ROLE_UNSET:
        return cast(OrganizationRole | None, cached)
    org_id = current_org_id()
    role = None if org_id is None else await project_access.org_role_of(session, user.id, org_id)
    request.state.org_role = role
    return role


async def require_org_member(
    session: AsyncSession, user: User, org_id: uuid.UUID | None = None
) -> None:
    """403 unless ``user`` belongs to ``org_id`` (default: the bound organization).

    The self-hosted default organization is bound for every session, members or
    not (``org_resolution`` rule 4), so this is what keeps an account without a
    membership from acting in it. Any organization role passes; what the caller
    may do inside a project is still decided by the project role.
    """
    target = org_id if org_id is not None else require_org_id()
    if await project_access.org_role_of(session, user.id, target) is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_MEMBERSHIP_REQUIRED)


def require_write_scope(request: Request) -> None:
    """API keys carry a scope; ``read`` keys are blocked from mutation endpoints.

    Session-authenticated users have no scope tag on request.state and so
    bypass this check — their role is the only gate.
    """
    scope = getattr(request.state, "api_key_scope", None)
    if scope == "read":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key has read-only scope",
        )


async def get_write_user(request: Request, user: CurrentUserDep) -> User:
    require_write_scope(request)
    return user


async def require_project_membership(
    request: Request, session: SessionDep, user: CurrentUserDep
) -> None:
    """Hide a project from everyone who is not its member (404, not 403).

    Mounted next to :func:`get_current_user` in ``api.v1.router``'s
    ``protected_dependencies``, so it runs on every authenticated route before
    the route's own dependencies and handler. On a route whose path carries a
    ``slug`` it resolves the caller's project role
    (:func:`tripl.services.project_access.require_project_access`): a
    non-member gets the same "Project not found" an unknown slug gets, and a
    member's role is stashed on ``request.state.project_role`` for the write
    gate. Routes without a ``slug`` pass untouched; the lists and feeds filter
    by membership themselves.

    API keys act as the user who minted them, so a key reaches exactly the
    projects its user is a member of.
    """
    await _ensure_request_org(request, session, user)
    await project_access.require_project_access(request, session, user)


async def _project_role(
    request: Request, session: AsyncSession, user: User
) -> project_access.ProjectRole | None:
    """The caller's role in the path's project, from the membership gate's stash.

    Falls back to resolving it (with the same 404 for a non-member) when the
    request did not pass :func:`require_project_membership`, e.g. a route
    mounted outside ``protected_dependencies``.
    """
    role: project_access.ProjectRole | None = getattr(request.state, "project_role", None)
    if role is not None:
        return role
    await _ensure_request_org(request, session, user)
    return await project_access.require_project_access(request, session, user)


async def get_project_role(
    request: Request, session: SessionDep, user: CurrentUserDep
) -> project_access.ProjectRole | None:
    """The caller's role in the path's project; ``None`` on a route without a slug.

    The public, injectable form of :func:`_project_role`: a non-member gets the
    membership gate's 404.
    """
    return await _project_role(request, session, user)


ProjectRoleDep = Annotated[project_access.ProjectRole | None, Depends(get_project_role)]


async def require_project_mutation_access(
    request: Request, session: AsyncSession, user: User
) -> None:
    """Demand an editing project role on a route whose path carries a ``slug``.

    Every route whose path carries a project ``slug`` has to answer "may they
    edit *this* project", otherwise a member of one project could rewrite the
    tracking plan of every project (tripl-jfm3.19). The answer is the caller's
    project role (:mod:`tripl.services.project_access`): an owner or admin of
    the project's organization, or a member whose membership role is
    ``editor``. A viewer member gets 403; a non-member never gets this far, the
    membership gate has already answered 404.

    Hooked into :func:`get_editor_user` rather than sprinkled over ~20 routers on
    purpose: the mutation surface is exactly the set of slug-scoped routes that
    already carry the editor gate (no ``GET`` uses it), so doing it here closes
    the whole surface at once and keeps future routes closed by default. Routes
    without a ``slug`` (``/projects``, ``/me/...``) are unaffected.
    """
    if not request.path_params.get("slug"):
        return
    if project_access.can_edit(await _project_role(request, session, user)):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Editor access to this project is required",
    )


def can_mutate_project(
    request: Request, user: User, scope: project_service.ProjectMutationScope
) -> bool:
    """Whether :func:`get_editor_user` would admit this caller on the project's routes.

    The non-raising form of the same checks, reused rather than restated, so
    ``ProjectResponse.can_mutate`` cannot drift from the gate it predicts: a
    ``read``-scope API key and a caller whose project role
    (:class:`~tripl.services.project_service.ProjectMutationScope`) is not an
    editing one are both ``False``. The project-bound key fence
    (``_enforce_project_scope``) is not repeated: a key that fails it never
    reaches a project's response at all. ``user`` is kept for the call sites;
    the project role already carries everything the user contributes.
    """
    del user
    try:
        require_write_scope(request)
    except HTTPException:
        return False
    return scope.allows()


async def get_editor_user(request: Request, session: SessionDep, user: CurrentUserDep) -> User:
    """The write gate for plan edits and for creating things in the organization.

    With a ``slug`` in the path: an editing role in that project. Without one
    (``POST /projects``, the demo routes, a data source's schema): membership of
    the bound organization, any role.
    """
    require_write_scope(request)
    if request.path_params.get("slug"):
        await require_project_mutation_access(request, session, user)
    else:
        await _ensure_request_org(request, session, user)
        await require_org_member(session, user)
    return user


async def get_org_member_user(request: Request, session: SessionDep, user: CurrentUserDep) -> User:
    """Any member of the bound organization (no scope check)."""
    await _ensure_request_org(request, session, user)
    await require_org_member(session, user)
    return user


async def _owner_gate(
    request: Request, session: AsyncSession, user: User, *, key_reachable: bool
) -> User:
    """The whole owner rule, in one place, with exactly one flag to differ on.

    Both owner gates below call this: they must never drift on the role or scope
    checks, only on whether a Bearer token is admitted at all.

    "Owner" means owner OR admin of the request's bound organization
    (``organization_members``); ``users.role`` is not read. With a ``slug`` in
    the path the caller must also hold project role ``owner`` in that project.
    That role only comes from owner/admin of the PROJECT's own organization,
    and the slug is resolved inside the bound organization, so the path
    project's organization is the request's: this is what keeps
    :data:`PROJECT_SCOPED_GATES` sound.
    """
    require_write_scope(request)
    await _ensure_request_org(request, session, user)
    if not project_access.is_org_admin_role(await request_org_role(request, session, user)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_ADMIN_REQUIRED)
    if not key_reachable and getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner session required",
        )
    if (
        request.path_params.get("slug")
        and await _project_role(request, session, user) != project_access.OWNER
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_ADMIN_REQUIRED)
    return user


async def get_owner_user(request: Request, session: SessionDep, user: CurrentUserDep) -> User:
    """The strict owner gate: org owner/admin **and** an interactive browser session.

    An API key is refused whatever its scope and whoever owns it, because
    "owner-only" here means security and organization administration — minting
    users and invitations, warehouse credentials, the audit feed, authoring the
    SQL a scan runs, deleting a project. A leaked ``tk_w_`` must not be able to
    invite a member or point a warehouse credential at a new query, so those
    stay browser-only.

    This is the default owner gate; :func:`get_key_reachable_owner_user` is the
    narrow, enumerated exception. Reach for this one unless the owner has
    explicitly decided a specific route is agent-safe.
    """
    return await _owner_gate(request, session, user, key_reachable=False)


async def get_key_reachable_owner_user(
    request: Request, session: SessionDep, user: CurrentUserDep
) -> User:
    """The owner gate an agent can pass: org owner/admin, ``write`` scope, key OK.

    Same role and scope demands as :func:`get_owner_user` — it only drops the
    "must be a cookie session" clause, so a member's key and any ``read`` key are
    still 403. Added for the bounded metrics replay (tripl-cj5z): no Bearer client
    could trigger one, which is why tripl-mcp ships no replay tool and the CLI
    dropped ``tripl scans replay``.

    Which routes take this gate is a security decision per route, not a
    convenience: ``test_owner_key_gates.py`` enumerates them from the live app and
    fails the build when a new route picks it up, so the exception list cannot
    grow by copy-paste.
    """
    return await _owner_gate(request, session, user, key_reachable=True)


async def require_platform_admin(request: Request, user: CurrentUserDep) -> User:
    """The operator gate: ``users.is_platform_admin`` in an interactive session.

    For the instance-wide operator settings, the ``system`` block and the
    platform console (F20 PR14). A platform admin gets no organization or
    project access from the flag — only a live read-only step-in, which the
    console opens with a reason and a time limit, lets them read one
    organization — and an organization owner gets no operator access from their
    org role.
    """
    require_write_scope(request)
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin session required",
        )
    if not user.is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLATFORM_ADMIN_REQUIRED)
    return user


async def require_org_creator(request: Request, user: CurrentUserDep) -> User:
    """Who may create an organization (``POST /orgs``). Never an API key.

    Self-hosted: a platform admin only (owner decision 4). Hosted: any signed-in
    browser session; the hosted email-verification gate in
    :func:`get_current_user` has already refused unverified accounts.
    """
    if settings.deployment_mode != DEPLOYMENT_HOSTED:
        return await require_platform_admin(request, user)
    require_write_scope(request)
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="A browser session is required"
        )
    return user


_LEGACY_SETTINGS_ORG_STATE_KEY = "legacy_settings_org_id"


async def legacy_settings_org_id(
    request: Request, session: AsyncSession, user: User
) -> uuid.UUID | None:
    """The organization the legacy ``/settings`` acts in for its org fields (F20 PR9).

    ``/settings`` is org-free (no organization is bound for it), so it resolves
    one itself, with the legacy-path rule: the default organization on a
    self-hosted instance, the user's only organization on a hosted one (an API
    key: its own), and ``None`` when a hosted user has none or several — never
    a fallback. Cached on the request.
    """
    cached = getattr(request.state, _LEGACY_SETTINGS_ORG_STATE_KEY, _ORG_ROLE_UNSET)
    if cached is not _ORG_ROLE_UNSET:
        return cast(uuid.UUID | None, cached)
    org_id: uuid.UUID | None
    try:
        org = await resolve_request_org(
            session,
            user=user,
            key_org_id=getattr(request.state, "api_key_org_id", None),
            path_org_slug=None,
        )
    except HTTPException:
        org_id = None
    else:
        org_id = org.id
    setattr(request.state, _LEGACY_SETTINGS_ORG_STATE_KEY, org_id)
    return org_id


async def is_settings_admin(request: Request, session: AsyncSession, user: User) -> bool:
    """Whether ``user`` may use the legacy combined ``/settings``.

    A platform admin, or an owner/admin of the organization the legacy route
    acts in (:func:`legacy_settings_org_id`): the default organization when
    self-hosted, the user's only organization when hosted.
    """
    if user.is_platform_admin:
        return True
    org_id = await legacy_settings_org_id(request, session, user)
    if org_id is None:
        return False
    return project_access.is_org_admin_role(
        await project_access.org_role_of(session, user.id, org_id)
    )


async def get_settings_admin_user(
    request: Request, session: SessionDep, user: CurrentUserDep
) -> User:
    """The gate of ``/settings``: :func:`is_settings_admin`, session only.

    Operator fields inside a settings write additionally need
    :func:`require_platform_admin`'s check, and organization fields an admin
    role in the resolved organization; the route applies both to the payload.
    """
    require_write_scope(request)
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner session required",
        )
    if not await is_settings_admin(request, session, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_ADMIN_REQUIRED)
    return user


# ── the organization management gates (F20 PR6) ─────────────────────────────

ORG_OWNER_REQUIRED = "Organization owner role required"
_MANAGED_ORG_STATE_KEY = "managed_org"


async def _resolve_path_org(
    request: Request, session: AsyncSession, user: User
) -> org_service.ManagedOrg:
    """The organization a ``/orgs/{org}/...`` path names, if the caller may see it.

    404 "Organization not found" for an unknown slug, a ``deleting``
    organization, a non-member and an API key of another organization alike,
    and always BEFORE any scope or role check, so a 403 never tells a stranger
    that the organization exists. A member of a suspended organization then
    gets 403 "This organization is suspended". A platform admin's live
    read-only step-in reads it as a member, and every write under it is
    refused (:func:`refuse_step_in_writes`). Binds the organization for the
    rest of the request (the audit rows it files belong to it) and caches the
    result.
    """
    cached = getattr(request.state, _MANAGED_ORG_STATE_KEY, None)
    if isinstance(cached, org_service.ManagedOrg):
        return cached
    key_org_id = getattr(request.state, "api_key_org_id", None)
    try:
        org = await org_service.resolve_managed_org(
            session,
            slug=str(request.path_params.get("org", "")),
            user_id=user.id,
            key_org_id=key_org_id,
            platform_admin=bool(user.is_platform_admin) and key_org_id is None,
        )
    except org_service.OrgNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORG_NOT_FOUND) from None
    except org_service.OrgSuspendedError:
        raise suspended_error() from None
    ref = OrgRef(id=org.id, slug=org.slug, step_in_user_id=user.id if org.step_in else None)
    refuse_step_in_writes(request, ref)
    bind_org(ref)
    request.state.org_role = org.role
    setattr(request.state, _MANAGED_ORG_STATE_KEY, org)
    return org


def _refuse_api_keys(request: Request) -> None:
    require_write_scope(request)
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner session required")


async def get_path_org_member_user(
    request: Request, session: SessionDep, user: CurrentUserDep
) -> User:
    """Any member of the path's organization; an API key of that organization too."""
    await _resolve_path_org(request, session, user)
    return user


async def get_path_org_admin_user(
    request: Request, session: SessionDep, user: CurrentUserDep
) -> User:
    """An owner or admin of the path's organization, from a browser session."""
    org = await _resolve_path_org(request, session, user)
    _refuse_api_keys(request)
    if not project_access.is_org_admin_role(org.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_ADMIN_REQUIRED)
    return user


async def get_path_org_owner_user(
    request: Request, session: SessionDep, user: CurrentUserDep
) -> User:
    """An owner of the path's organization, from a browser session."""
    org = await _resolve_path_org(request, session, user)
    _refuse_api_keys(request)
    if org.role != OrganizationRole.owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_OWNER_REQUIRED)
    return user


def get_managed_org(request: Request) -> org_service.ManagedOrg:
    """What the route's path-org gate resolved; declare it AFTER that gate."""
    org = getattr(request.state, _MANAGED_ORG_STATE_KEY, None)
    if not isinstance(org, org_service.ManagedOrg):  # pragma: no cover - wiring error
        raise RuntimeError("get_managed_org needs a path-org gate declared before it")
    return org


PathOrgMemberUserDep = Annotated[User, Depends(get_path_org_member_user)]
PathOrgAdminUserDep = Annotated[User, Depends(get_path_org_admin_user)]
PathOrgOwnerUserDep = Annotated[User, Depends(get_path_org_owner_user)]
ManagedOrgDep = Annotated[org_service.ManagedOrg, Depends(get_managed_org)]

WriteUserDep = Annotated[User, Depends(get_write_user)]
EditorUserDep = Annotated[User, Depends(get_editor_user)]
OwnerUserDep = Annotated[User, Depends(get_owner_user)]
KeyReachableOwnerUserDep = Annotated[User, Depends(get_key_reachable_owner_user)]
OrgMemberUserDep = Annotated[User, Depends(get_org_member_user)]
PlatformAdminUserDep = Annotated[User, Depends(require_platform_admin)]
SettingsAdminUserDep = Annotated[User, Depends(get_settings_admin_user)]

_WriteGate = Callable[..., Awaitable[User]]
_GateReplay = Callable[[Request, AsyncSession, User], Awaitable[User]]

# Every write gate, with the call that runs its checks again by hand, passing the
# arguments FastAPI would inject. get_branch_id_override replays a route's gates
# before its read-only 409 (see :func:`_refuse_writes_to_a_read_only_branch`).
_WRITE_GATE_REPLAYS: dict[_WriteGate, _GateReplay] = {
    get_write_user: lambda request, _session, user: get_write_user(request, user),
    get_editor_user: get_editor_user,
    get_owner_user: get_owner_user,
    get_key_reachable_owner_user: get_key_reachable_owner_user,
    require_platform_admin: lambda request, _session, user: require_platform_admin(request, user),
    require_org_creator: lambda request, _session, user: require_org_creator(request, user),
    get_settings_admin_user: get_settings_admin_user,
    get_path_org_admin_user: get_path_org_admin_user,
    get_path_org_owner_user: get_path_org_owner_user,
}

# The route audits in tests/ classify every route by the gate it carries. Each
# audit used to spell its own literal set of gate functions, so adding a gate
# meant remembering every copy — and a forgotten copy does not fail, it silently
# reclassifies the new route as ungated. Spelled once, next to the gates
# themselves, so a new gate is added in the same edit that defines it. Read off
# the replay table's keys, so a gate cannot be a write gate without a replay.
WRITE_GATES = frozenset(_WRITE_GATE_REPLAYS)
# Gates that resolve the path's project as well as the caller's role:
# ``get_editor_user`` runs :func:`require_project_mutation_access` (an editing
# project role: org owner/admin or ``editor`` member), and the two owner gates
# demand org owner/admin of the bound organization AND project role ``owner``
# in the path's project, which only an owner/admin of that project's own
# organization holds; the slug is resolved inside the bound organization, so
# the path project's organization is always the request's. The platform and
# settings gates carry no project and are not in this set.
# :func:`require_project_membership` is deliberately NOT in this set: it is
# mounted on every route by the router and admits viewer members, so counting it
# would make every slug-scoped mutation pass the audit trivially.
PROJECT_SCOPED_GATES = frozenset({get_editor_user, get_owner_user, get_key_reachable_owner_user})

# A merged branch is the record of what landed on main and a closed one is
# shelved until someone reopens it, so neither takes plan writes. Main is stored
# with ``status="merged"`` too, which is why the check runs only after main has
# been split off (tripl-0zpq.145). Photo and Figma spec writes address a
# branch's event by its own id, with no ``?branch=``, so they never reach this;
# ``event_photo_service._get_plan_writable_event`` refuses them the same way.
_READ_ONLY_BRANCH_STATUSES = frozenset({BranchStatus.merged.value, BranchStatus.closed.value})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# The one write-gated route deliberately let through on a merged or closed
# branch. A search reindex derives its index from the plan without changing it,
# and the runbook rebuilds that "once per project AND per plan branch" after a
# migration, merged and closed branches included. It is not the only gated
# route that changes nothing: the AI describe-event and describe-event-type
# suggestions write nothing either, and they are refused on purpose, since a
# description suggested for a branch that cannot take the edit has nowhere to
# go. Named by handler because this module cannot import the routers that
# import it. test_branch_context_batch2 fails if the name stops matching.
_DERIVED_DATA_HANDLERS = frozenset({"tripl.api.v1.search.reindex_project_search"})
# Write-gated routes that change no plan row, so they take no plan lock
# (tripl-0zpq.288, .294): the AI describe suggestions. They are still refused
# on a merged or closed branch (see above), but holding the branch row across
# an LLM call would only make a merge wait for a request that writes nothing.
# Named by handler for the same reason as above; test_batch18_merge_races_pg
# fails if a name stops matching a route.
_LOCK_FREE_WRITE_HANDLERS = frozenset(
    {
        "tripl.api.v1.ai.describe_event",
        "tripl.api.v1.ai.describe_event_type",
    }
)


def _write_gates_in(dependant: Dependant) -> list[_WriteGate]:
    """Every write gate in ``dependant``'s tree, in the order FastAPI runs them.

    FastAPI solves a sub-dependency's own dependencies before the sub-dependency
    itself, and siblings in declaration order, so this walk does the same.
    """
    found: list[_WriteGate] = []
    for sub in dependant.dependencies:
        found.extend(gate for gate in _write_gates_in(sub) if gate not in found)
        call = sub.call
        if call is not None and call in _WRITE_GATE_REPLAYS and call not in found:
            found.append(call)
    return found


def _is_a_write(request: Request) -> bool:
    """Whether this request is on the mutation surface.

    Keyed on the matched route's write gate, not on the method alone.
    ``POST /ai/ask`` is a read that carries its question in a body, has no gate,
    and the command palette sends it with the active branch, which a diff-row
    link can set to a merged one. The write gates are already this module's
    definition of "mutation" (see :func:`require_project_mutation_access`),
    less :data:`_DERIVED_DATA_HANDLERS`.

    FastAPI resolves dependencies before it validates the endpoint's own path,
    query and body parameters, so the 409 wins over the route's own 404s and
    over the 422 for a body that parses but fails its schema. On a route that
    takes a body, one sent with a JSON Content-Type (``application/json`` or
    ``application/*+json``) that is not valid JSON at all is refused with 422
    before any dependency runs, authentication included. Under any other
    Content-Type, or none, FastAPI keeps the raw bytes and only validates them
    after the dependencies, so the 409 wins there too. The route's write gate
    answers ahead of the 409: see :func:`_refuse_writes_to_a_read_only_branch`.

    When the route cannot be seen (a call from outside the router), the method
    decides, so an unknown write is refused rather than let through.
    """
    if request.method in _SAFE_METHODS:
        return False
    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return True
    endpoint = getattr(route, "endpoint", None)
    handler = f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__qualname__', '')}"
    if handler in _DERIVED_DATA_HANDLERS:
        return False
    return bool(_write_gates_in(dependant))


def _writes_the_plan(request: Request) -> bool:
    """Whether this request must hold its branch's plan lock (tripl-0zpq.288).

    Every write, as :func:`_is_a_write` decides it, less the handlers in
    :data:`_LOCK_FREE_WRITE_HANDLERS`.
    """
    if not _is_a_write(request):
        return False
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    handler = f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__qualname__', '')}"
    return handler not in _LOCK_FREE_WRITE_HANDLERS


async def _refuse_writes_to_a_read_only_branch(
    request: Request, session: AsyncSession, user: User, plan_branch: PlanBranch
) -> None:
    """409 when a write targets a merged or closed branch.

    Before this, only revert and the transition route read ``status``. Every
    other write landed, so a merged branch kept drifting from the revision it
    merged. The docs said such writes are refused, which is why the UI offers no
    Edit on these branches (tripl-0zpq.145). 409 matches the revert refusal for
    the same state. For a write, :func:`get_branch_id_override` has re-read
    the row ``FOR SHARE`` before this runs, so a write that arrived during a
    merge has waited for it and sees ``merged`` here (tripl-0zpq.288). A close
    takes no row lock, so a write already past this check when a close commits
    still lands; a closed branch can be reopened, so nothing is lost.

    Authorization answers first. FastAPI runs a route's dependencies in the
    order its signature declares them, and most event write routes, two
    reconciliation routes and the AI describe suggestions declare ``?branch=``
    ahead of their write gate. There a viewer or a ``read`` key got this 409
    instead of the gate's 403: told to reopen a branch it has no right to
    reopen, about a write it could never make. So the route's own gates run
    here, in FastAPI's order, before the 409, and the caller gets exactly the
    403 the gate would have given. A gate that already ran passes again, and
    the editor gate reads the project role the membership gate stashed, so no
    query is repeated. That keeps the
    precedence out of each route's parameter order, including the next route's.
    """
    if plan_branch.status not in _READ_ONLY_BRANCH_STATUSES or not _is_a_write(request):
        return
    dependant = getattr(request.scope.get("route"), "dependant", None)
    if dependant is not None:
        for gate in _write_gates_in(dependant):
            await _WRITE_GATE_REPLAYS[gate](request, session, user)
    if plan_branch.status == BranchStatus.merged.value:
        detail = f"Branch '{plan_branch.name}' is merged, so its plan is read-only"
    else:
        detail = f"Branch '{plan_branch.name}' is closed; reopen it before editing its plan"
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _hold_main_for_a_plan_write(request: Request, session: AsyncSession) -> None:
    """Hold main's branch row for a write to main (tripl-0zpq.294).

    A merge takes main's row before it reads main for its conflict check, so a
    main edit arriving mid-merge waits and applies on top of the merged plan,
    and a merge arriving mid-edit waits and then sees the edit, as a conflict
    where it clashes with the branch. Without a ``slug`` there is no project,
    and so no main, to hold.
    """
    slug = request.path_params.get("slug")
    # ``locks_rows`` first: off PostgreSQL the lock is a no-op, so the id lookup
    # would only cost a query per plan write.
    if slug and _writes_the_plan(request) and locks_rows(session):
        # The lock is keyed by the project's id within the request's
        # organization (F20 PR3). An unknown slug has no main to hold; the
        # route's own lookup answers its 404.
        project_id = await session.scalar(select(Project.id).where(project_slug_clause(slug)))
        if project_id is not None:
            await hold_main_plan_for_write(session, project_id)


async def get_branch_id_override(
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    branch: Annotated[
        str | None,
        Query(description="Plan branch id (UUID) to read and write instead of the main branch."),
    ] = None,
) -> AsyncIterator[uuid.UUID | None]:
    """Resolve the editor's active branch from the ``?branch=`` query param.

    Yields ``None`` for main, whether no override is supplied or it names main
    by id (services then default to the project's main branch). Validates that
    the branch belongs to the project referenced by the path's ``slug`` so
    cross-project ids can't leak through, and refuses writes to a merged or
    closed branch with 409, once the route's write gate has admitted the caller.

    ``user`` is only needed to replay that gate. Every route carrying this
    dependency is already mounted behind :func:`get_current_user`, and FastAPI
    caches a dependency per request, so declaring it here re-runs nothing; it
    also puts the 401 ahead of the 409 by construction.

    Declared as a parameter rather than read off ``request.query_params`` so
    FastAPI propagates it into the OpenAPI schema of every route carrying
    :data:`BranchIdDep` — otherwise the one documented way to keep agent edits
    off the live plan is invisible to every generated client (tripl-l33u.7).
    Typed ``str`` and parsed here on purpose: FastAPI's own ``uuid.UUID``
    coercion answers a malformed value with 422, and the published contract for
    this parameter is 400.

    A generator rather than a plain ``async def`` because this is also the ONE
    place that binds the request's branch for ``audit_service.record`` — see
    :mod:`tripl.middleware.branch_context` — and the binding needs a teardown
    point. Every raise below, the replayed gate's 403 included, stays ahead of
    the first ``yield``, so the 400, 403, 404 and 409 contracts hold. A yield
    dependency's teardown runs in the same task, and so the same ``Context``, as
    its setup (fastapi/routing.py enters and exits ``request_stack`` inside one
    coroutine), which is what makes ``ContextVar.reset`` valid here; that has
    only been true since FastAPI 0.106, and the pin is 0.141.1.
    """
    if not branch:
        # Unbound rather than bound-to-None: a route with no ``?branch=`` is
        # main, which is what the contextvar's default already says.
        await _hold_main_for_a_plan_write(request, session)
        yield None
        return
    try:
        branch_id = uuid.UUID(branch)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid branch id",
        ) from exc
    slug = request.path_params.get("slug")
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Branch context requires a project slug in the path",
        )
    plan_branch = await session.scalar(
        select(PlanBranch)
        .join(Project, Project.id == PlanBranch.project_id)
        .where(PlanBranch.id == branch_id, project_slug_clause(slug))
    )
    if plan_branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Branch not found",
        )
    if plan_branch.kind == BranchKind.main.value:
        # ``GET /branches`` hands a caller main's own id (list_branches returns
        # the kind="main" row), and passing it back here is accepted. Main is
        # spelled as the absence of a branch everywhere else — audit_service,
        # the audit tab's chip, the CLI's "there is no literal for main" — so
        # binding it would spell it a second way and make two identical writes
        # to main render differently in the same compliance trail
        # (tripl-wkwv.6).
        #
        # The same holds for what this yields, so it yields ``None``, not the
        # id. event_type_service, meta_field_service and event_service decide
        # "main" by ``branch_id is None``, and their event-type, meta-field and
        # project cache busts key on it. field_service used to as well, for its
        # name-format delete guard and its cache busts; it now reads main off
        # the row instead (see ``field_service._on_main``), so it holds however
        # a caller spells main. With the id passed through, the write still
        # landed on main but skipped the checks and busts keyed on
        # ``branch_id is None``, and a field a scan names events by could be
        # deleted from the live plan (tripl-0zpq.121, tripl-0zpq.215).
        # Normalising here, in the one place that resolves ``?branch=``, makes
        # the request exactly what it is with no ``?branch=`` at all.
        await _hold_main_for_a_plan_write(request, session)
        yield None
        return
    if _writes_the_plan(request):
        # Re-read under FOR SHARE, in the session the route writes through, so
        # the lock lasts until the write commits: a merge of this branch that is
        # in flight is waited for, and its ``merged`` is what the refusal below
        # reads; a merge that starts later waits for this write before it
        # snapshots the branch (tripl-0zpq.288). ``_plan_branch_locks`` holds
        # the deadlock audit.
        locked = await hold_branch_for_plan_write(session, plan_branch.id)
        if locked is None:
            # Deleted while this request waited for the lock.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Branch not found",
            )
        plan_branch = locked
    await _refuse_writes_to_a_read_only_branch(request, session, user, plan_branch)
    # The name comes free — the query above already loads the whole row — and it
    # is what keeps an audit entry readable after the branch is deleted.
    with bound_branch(plan_branch.id, plan_branch.name):
        yield plan_branch.id


BranchIdDep = Annotated[uuid.UUID | None, Depends(get_branch_id_override)]
