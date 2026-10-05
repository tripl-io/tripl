from __future__ import annotations

import enum
import json
import uuid
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import ColumnElement, desc, func, null, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import extensions
from tripl.middleware.branch_context import current_branch
from tripl.models.audit_log import AuditLog
from tripl.models.project import Project
from tripl.models.user import User
from tripl.schemas.audit import (
    AuditEntryDetailResponse,
    AuditEntryResponse,
    AuditListResponse,
)
from tripl.services.project_lookup import owning_org_id, project_slug_clause

# Fields that must never make it into the audit payload — credentials, hashes,
# anything you would not want to read back from the audit UI in cleartext.
# Matching is exact-key (no substring); the alerting destination secrets below
# don't fit the generic password/token/secret names, so they're listed by name.
_REDACTED_KEYS = frozenset(
    {
        "password",
        "password_encrypted",
        "password_hash",
        "secret",
        "token",
        "credentials",
        # Alerting destination secrets (AlertDestinationCreate).
        "webhook_url",
        "bot_token",
        "target_url",
        "webhook_header_value",
        "jira_api_token",
        "linear_api_key",
    }
)

# ``audit_log.target_name`` is String(255) and no call site truncated. Every
# target that existed before events fits by construction — event_type, field,
# variable and meta_field names are String(100), plan_branch is String(255) —
# but ``Event.name`` is String(500), so a 300-character event name would make
# the audit INSERT fail with "value too long for character varying(255)" AFTER
# the event itself was already committed: a 500 response with the write applied
# and no audit row. The guard lives here, not at the six event call sites, so
# the next long-named target cannot reintroduce it. Widening the column was the
# alternative and was rejected: a migration whose downgrade has to truncate
# rows, for a display-only field.
_TARGET_NAME_MAX = 255


class _BoundOrg(enum.Enum):
    """Sentinel type of :data:`BOUND_ORG`."""

    token = 0


#: ``record(organization_id=...)``'s default: the project's organization, else
#: the bound one. Pass an id to file the row elsewhere (a new organization's
#: ``org.create``), or ``None`` for a platform-scope row (``org.delete_complete``).
BOUND_ORG: Final = _BoundOrg.token


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through JSON to coerce UUIDs, datetimes, enums to primitives."""
    return cast(dict[str, Any], json.loads(json.dumps(payload, default=str)))


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k in _REDACTED_KEYS else v) for k, v in payload.items()}


async def record(
    session: AsyncSession,
    *,
    user: User | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None,
    target_name: str = "",
    project: Project | None = None,
    project_slug: str | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
    organization_id: uuid.UUID | None | Literal[_BoundOrg.token] = BOUND_ORG,
) -> AuditLog:
    """Write one audit row.

    ``commit`` exists for the one caller that writes a BATCH of rows for a single
    user action: the inbox bulk route files one row per incident, and committing
    inside that loop made a 200-incident mute 200 separate transactions, any of
    which could fail after the earlier ones were already durable — leaving
    incidents silenced with no record of who silenced them. Passing
    ``commit=False`` and committing once after the loop makes the batch atomic.

    It defaults to True because every other caller writes exactly one row and
    relies on this function to land it; flipping that default would silently
    leave audit rows uncommitted across the whole API.

    ``target_name`` is truncated to the column width — see ``_TARGET_NAME_MAX``
    for why that is a guard and not a formality.

    The branch is NOT a parameter. It is read off the request-scoped contextvar
    that ``api.deps.get_branch_id_override`` — the one function that resolves
    ``?branch=`` — binds, so EVERY branch-scoped route handler records it
    without a single call-site edit and the next one cannot forget to.
    Deliberately not a census: this sentence used to say "all 21
    ... and the 22nd", which was already off by one when it was written and is
    off by six now that the six event routes record. Universal quantification is
    what the mechanism guarantees, and it cannot go stale.
    A NULL ``branch_id`` with an empty ``branch_name`` means "not written through
    a branch-scoped request": either main, or an action with no plan-branch
    dimension at all (alerting, scans, data sources, users, API keys). It does
    not assert "main", which is why the audit tab shows a chip only when
    ``branch_name`` is non-empty.
    """
    project_id: uuid.UUID | None
    slug = ""
    # The row belongs to the project's organization, else the bound one (the
    # default when none is bound), so the feed can be read per organization.
    owning_org = owning_org_id()
    if project is not None:
        project_id = project.id
        slug = project.slug
        owning_org = project.organization_id
    elif project_slug:
        row = (
            await session.execute(
                select(Project.id, Project.slug, Project.organization_id).where(
                    project_slug_clause(project_slug)
                )
            )
        ).one_or_none()
        if row is None:
            project_id = None
            slug = project_slug
        else:
            project_id, slug, owning_org = row
    else:
        project_id = None
    org_id: uuid.UUID | None = owning_org if organization_id is BOUND_ORG else organization_id

    # Read once per row so a ``commit=False`` batch (the inbox bulk route) is
    # consistent within itself; that route carries no branch, so it gets NULL.
    branch = current_branch()

    entry = AuditLog(
        user_id=user.id if user else None,
        user_email=user.email if user else "",
        project_id=project_id,
        project_slug=slug,
        organization_id=org_id,
        branch_id=branch[0] if branch else None,
        branch_name=branch[1] if branch else "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=(target_name or "")[:_TARGET_NAME_MAX],
        payload=_redact(_jsonable(payload or {})),
    )
    if org_id is None:
        # A platform-scope row. ``None`` alone would let the column's default
        # (the default organization) fill it in; ``NULL`` is written as such.
        entry.organization_id = null()
    session.add(entry)
    if org_id is not None:
        # Audit sinks (the organization's audit webhook), in this very
        # transaction: the row is delivered if and only if it commits.
        await extensions.on_audit_recorded(session, entry, org_id)
    if commit:
        await session.commit()
    return entry


async def _project_scope(session: AsyncSession, project_slug: str) -> ColumnElement[bool]:
    """The rows of the project ``project_slug`` names in the bound organization.

    Resolve the slug to a project and filter on the ID. ``project_slug`` on a
    row is DENORMALIZED — the slug the project answered to when the row was
    written — so matching the label means a rename splits a trail in two and a
    slug re-used by a later project makes it inherit its predecessor's history.

    The fallback is the other half of the same idea: when NOTHING live answers
    to this slug, the label is all there is and no live project can be confused
    by it. That is what keeps a deleted project's rows reachable — including its
    ``project.delete`` row, which is born with a NULL project id because it is
    written after its subject is gone. Callers fence the label fallback to the
    bound organization: a deleted project's slug names nothing outside it.
    """
    owner_id: uuid.UUID | None = await session.scalar(
        select(Project.id).where(project_slug_clause(project_slug))
    )
    return (
        AuditLog.project_id == owner_id
        if owner_id is not None
        else AuditLog.project_slug == project_slug
    )


async def list_entries(
    session: AsyncSession,
    *,
    project_slug: str | None = None,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    user_email: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AuditListResponse:
    # Only the bound organization's feed (F20 PR4): an organization admin must
    # not read another organization's payloads (warehouse details, scan SQL).
    in_org = AuditLog.organization_id == owning_org_id()
    base = select(AuditLog).where(in_org)
    count_base = select(func.count()).select_from(AuditLog).where(in_org)

    if project_slug:
        scope = await _project_scope(session, project_slug)
        base = base.where(scope)
        count_base = count_base.where(scope)
    if action:
        base = base.where(AuditLog.action == action)
        count_base = count_base.where(AuditLog.action == action)
    if user_id:
        base = base.where(AuditLog.user_id == user_id)
        count_base = count_base.where(AuditLog.user_id == user_id)
    if user_email:
        needle = f"%{user_email.lower()}%"
        base = base.where(func.lower(AuditLog.user_email).like(needle))
        count_base = count_base.where(func.lower(AuditLog.user_email).like(needle))
    if since:
        base = base.where(AuditLog.created_at >= since)
        count_base = count_base.where(AuditLog.created_at >= since)
    if until:
        base = base.where(AuditLog.created_at < until)
        count_base = count_base.where(AuditLog.created_at < until)

    # `created_at` alone is NOT a total order here. It is `server_default=now()`,
    # i.e. Postgres `transaction_timestamp()`, so every row a batch writes shares
    # one byte-identical value — the inbox bulk route (`record(..., commit=False)`
    # in a loop) files up to 200 of them per click. LIMIT/OFFSET runs each page as
    # its own top-N sort with a different bound, so ties are free to come out in a
    # different order per page: paging through a tie group repeated some rows and
    # made others unreachable. The primary key is unique, so appending it makes
    # the order total and every page a slice of one sequence.
    rows = (
        (
            await session.execute(
                base.order_by(desc(AuditLog.created_at), desc(AuditLog.id))
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = int(await session.scalar(count_base) or 0)
    return AuditListResponse(
        items=[AuditEntryResponse.model_validate(r) for r in rows],
        total=total,
    )


async def get_entry(
    session: AsyncSession, entry_id: uuid.UUID, *, project_slug: str | None = None
) -> AuditEntryDetailResponse | None:
    """One entry with the payload the list rows deliberately leave out.

    ``None`` for an id that is not in the log, so the router can answer 404
    rather than an empty body — and for another organization's entry, which
    does not exist for the caller. With ``project_slug``, also for an entry of
    another project or of none.
    """
    query = select(AuditLog).where(
        AuditLog.id == entry_id, AuditLog.organization_id == owning_org_id()
    )
    if project_slug:
        query = query.where(await _project_scope(session, project_slug))
    row = await session.scalar(query)
    return AuditEntryDetailResponse.model_validate(row) if row is not None else None
