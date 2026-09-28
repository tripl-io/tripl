"""The note editor's link picker (F24 part 2, GH #308).

``GET /projects/{slug}/docs/link-suggestions?q=&kind=&limit=`` answers what the
editor offers after ``[[`` (every kind), ``[[metric:`` (one kind) or ``@``
(people). Each suggestion carries ``insert``, the canonical reference text, so
the editor never builds a reference itself.

Where the candidates come from:

* plan entities (event, event type, field, variable, metric): the project's
  hybrid search on the main plan, filtered by search type, lexical leg only (a
  keystroke must not wait on an embedding round trip). The hits' ids are then
  read back for their current NAMES, which is what a reference stores. With an
  empty ``q`` they are listed by name instead;
* notes: the ones the caller may see (``docs_access.visible_docs_clause``),
  by title or path;
* people: members of the project's organization, by name or email prefix;
* alert rules, branches, scans and data sources: the project's own (and the
  sources it may use), by name.

The route is behind the project membership gate, so every answer is about a
project the caller is a member of.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from itertools import zip_longest
from typing import Any

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.doc_file import DocFile
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_definition import MetricDefinition
from tripl.models.organization import OrganizationMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.schemas.docs import DocLinkKind, DocLinkSuggestion
from tripl.schemas.search import SearchEntityType
from tripl.services import search_service
from tripl.services._docs_link_targets import NAME_KINDS
from tripl.services._search_query import sanitize_query
from tripl.services.docs_access import visible_docs_clause
from tripl.services.docs_links import raw_link
from tripl.services.plan_branch_service import resolve_branch_id

MAX_SUGGESTIONS = 50
MAX_QUERY_CHARS = 200

#: Plan kinds served by the search index, with their search type.
_SEARCHED: dict[str, SearchEntityType] = {
    "event": "event",
    "event_type": "event_type",
    "field": "field",
    "variable": "variable",
    "metric": "metric",
}
#: The order streams are interleaved in when no kind is asked for.
_ALL_KINDS: tuple[DocLinkKind, ...] = (
    "doc",
    "event",
    "event_type",
    "field",
    "metric",
    "variable",
    "alert_rule",
    "branch",
    "scan",
    "data_source",
    "user",
)
_SCOPE_LABEL = {"project": "Project notes", "organization": "Organization notes"}


def _insert(kind: str, target: str, qualifier: str | None = None) -> str:
    return raw_link(kind, target, qualifier)


def _matches(column: Any, q: str) -> ColumnElement[bool]:
    return func.lower(column).contains(q.lower(), autoescape=True)


def _prefix_first(column: Any, q: str) -> Any:
    """Sort key: names starting with ``q`` before names merely containing it."""
    if not q:
        return func.lower(column)
    return case((func.lower(column).startswith(q.lower(), autoescape=True), 0), else_=1)


class _Context:
    def __init__(
        self, session: AsyncSession, project: Project, user: User, q: str, limit: int
    ) -> None:
        self.session = session
        self.project = project
        self.user = user
        self.q = q
        self.limit = limit
        self._branch_id: uuid.UUID | None = None

    async def branch_id(self) -> uuid.UUID:
        if self._branch_id is None:
            self._branch_id = await resolve_branch_id(self.session, self.project.id, None)
        return self._branch_id


# ── Plan entities: the search index, then their current names ───────────────


async def _plan_rows(
    ctx: _Context, kind: str, ids: Sequence[uuid.UUID] | None
) -> list[DocLinkSuggestion]:
    """Suggestions of one plan kind: for ``ids`` in that order, or by name when None."""
    branch_id = await ctx.branch_id()
    project_id = ctx.project.id
    on_main = (EventType.project_id == project_id, EventType.branch_id == branch_id)
    stmt: Any
    if kind == "event":
        stmt = (
            select(Event.id, Event.name, EventType.name, Event.name)
            .join(EventType, EventType.id == Event.event_type_id)
            .where(Event.project_id == project_id, Event.branch_id == branch_id)
        )
        name_column: Any = Event.name
    elif kind == "event_type":
        stmt = select(EventType.id, EventType.name, EventType.display_name, EventType.name).where(
            *on_main
        )
        name_column = EventType.name
    elif kind == "field":
        stmt = (
            select(FieldDefinition.id, FieldDefinition.name, EventType.name, FieldDefinition.name)
            .join(EventType, EventType.id == FieldDefinition.event_type_id)
            .where(*on_main)
        )
        name_column = FieldDefinition.name
    elif kind == "variable":
        stmt = select(Variable.id, Variable.name, Variable.variable_type, Variable.name).where(
            Variable.project_id == project_id, Variable.branch_id == branch_id
        )
        name_column = Variable.name
    else:
        stmt = select(
            MetricDefinition.id,
            MetricDefinition.display_name,
            MetricDefinition.name,
            MetricDefinition.name,
        ).where(MetricDefinition.project_id == project_id)
        name_column = MetricDefinition.name
    id_column = stmt.selected_columns[0]
    if ids is not None:
        if not ids:
            return []
        stmt = stmt.where(id_column.in_(ids))
    else:
        if ctx.q:
            stmt = stmt.where(_matches(name_column, ctx.q))
        stmt = stmt.order_by(_prefix_first(name_column, ctx.q), name_column).limit(ctx.limit)
    rows = list((await ctx.session.execute(stmt)).all())
    order = {entity_id: index for index, entity_id in enumerate(ids or [])}
    if ids is not None:
        rows.sort(key=lambda row: order.get(row[0], len(order)))
    out: list[DocLinkSuggestion] = []
    for entity_id, label, detail, name in rows:
        insert = _insert("field", name, str(detail)) if kind == "field" else _insert(kind, name)
        out.append(
            DocLinkSuggestion(
                kind=kind,
                id=entity_id,
                label=str(label or name),
                detail=str(detail or ""),
                insert=insert,
            )
        )
    return out


async def _searched(ctx: _Context, kinds: Sequence[str]) -> list[DocLinkSuggestion]:
    """Plan suggestions of ``kinds`` in the search index's rank order."""
    if not ctx.q:
        streams = [await _plan_rows(ctx, kind, None) for kind in kinds]
        return _interleave(streams, ctx.limit)
    response = await search_service.search_project(
        ctx.session,
        ctx.project.slug,
        ctx.q,
        entity_types=[_SEARCHED[kind] for kind in kinds],
        limit=ctx.limit,
        semantic=False,
        project_id=ctx.project.id,
        viewer=ctx.user,
    )
    ids_by_kind: dict[str, list[uuid.UUID]] = {}
    rank: dict[uuid.UUID, int] = {}
    for index, hit in enumerate(response.items):
        if hit.entity_id not in rank:
            rank[hit.entity_id] = index
            ids_by_kind.setdefault(hit.entity_type, []).append(hit.entity_id)
    found: list[DocLinkSuggestion] = []
    for kind, ids in ids_by_kind.items():
        found.extend(await _plan_rows(ctx, kind, ids))
    found.sort(key=lambda item: rank.get(item.id, len(rank)))
    return found[: ctx.limit]


# ── Everything else: direct queries ──────────────────────────────────────────


async def _docs(ctx: _Context) -> list[DocLinkSuggestion]:
    project = ctx.project
    stmt = select(DocFile).where(
        or_(DocFile.project_id == project.id, DocFile.organization_id == project.organization_id),
        visible_docs_clause(ctx.user.id),
    )
    if ctx.q:
        stmt = stmt.where(or_(_matches(DocFile.title, ctx.q), _matches(DocFile.path, ctx.q)))
    stmt = stmt.order_by(
        _prefix_first(DocFile.title, ctx.q), DocFile.organization_id.is_not(None), DocFile.path_key
    ).limit(ctx.limit)
    out: list[DocLinkSuggestion] = []
    for doc in (await ctx.session.scalars(stmt)).all():
        scope = "project" if doc.project_id is not None else "organization"
        out.append(
            DocLinkSuggestion(
                kind="doc",
                id=doc.id,
                label=doc.title or doc.path,
                detail=f"{_SCOPE_LABEL[scope]} · {doc.path}",
                insert=_insert("doc", str(doc.id)),
            )
        )
    return out


async def _users(ctx: _Context) -> list[DocLinkSuggestion]:
    stmt = (
        select(User.id, User.name, User.email)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == ctx.project.organization_id)
    )
    if ctx.q:
        prefix = ctx.q.lower()
        stmt = stmt.where(
            or_(
                func.lower(func.coalesce(User.name, "")).startswith(prefix, autoescape=True),
                func.lower(User.email).startswith(prefix, autoescape=True),
            )
        )
    stmt = stmt.order_by(func.lower(func.coalesce(User.name, User.email)), User.id).limit(ctx.limit)
    return [
        DocLinkSuggestion(
            kind="user",
            id=user_id,
            label=name or email,
            detail=email,
            insert=_insert("user", str(user_id)),
        )
        for user_id, name, email in (await ctx.session.execute(stmt)).all()
    ]


async def _alert_rules(ctx: _Context) -> list[DocLinkSuggestion]:
    stmt = (
        select(AlertRule.id, AlertRule.name, AlertDestination.name)
        .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
        .where(AlertDestination.project_id == ctx.project.id)
    )
    if ctx.q:
        stmt = stmt.where(_matches(AlertRule.name, ctx.q))
    stmt = stmt.order_by(_prefix_first(AlertRule.name, ctx.q), AlertRule.name, AlertRule.id).limit(
        ctx.limit
    )
    return [
        DocLinkSuggestion(
            kind="alert_rule",
            id=rule_id,
            label=name,
            detail=destination,
            # By id: two destinations may each have a rule of this name.
            insert=_insert("alert_rule", str(rule_id)),
        )
        for rule_id, name, destination in (await ctx.session.execute(stmt)).all()
    ]


async def _named(ctx: _Context, kind: str) -> list[DocLinkSuggestion]:
    """Branches, scans and data sources: the rows a ``[[kind:NAME]]`` link resolves against."""
    base = NAME_KINDS[kind].rows(ctx.project, await ctx.branch_id())
    name_column = base.selected_columns[1]
    stmt = base
    if ctx.q:
        stmt = stmt.where(_matches(name_column, ctx.q))
    stmt = stmt.order_by(_prefix_first(name_column, ctx.q), name_column).limit(ctx.limit)
    return [
        DocLinkSuggestion(
            kind=kind,
            id=entity_id,
            label=str(label or name),
            detail=str(detail or ""),
            insert=_insert(kind, name),
        )
        for entity_id, name, label, detail in (await ctx.session.execute(stmt)).all()
    ]


def _interleave(
    streams: Sequence[Sequence[DocLinkSuggestion]], limit: int
) -> list[DocLinkSuggestion]:
    """Round-robin over the streams, each in its own order, so no kind starves the rest."""
    out: list[DocLinkSuggestion] = []
    for row in zip_longest(*streams):
        out.extend(item for item in row if item is not None)
    return out[:limit]


async def suggest(
    session: AsyncSession,
    project: Project,
    user: User,
    *,
    q: str,
    kind: DocLinkKind | None,
    limit: int,
) -> list[DocLinkSuggestion]:
    """Up to ``limit`` candidates for ``q``, of ``kind`` or of every kind."""
    text = sanitize_query(q)[:MAX_QUERY_CHARS]
    ctx = _Context(session, project, user, text, max(1, min(limit, MAX_SUGGESTIONS)))
    kinds = [kind] if kind is not None else list(_ALL_KINDS)
    plan_kinds = [item for item in kinds if item in _SEARCHED]
    streams: list[list[DocLinkSuggestion]] = []
    for item in kinds:
        if item == "doc":
            streams.append(await _docs(ctx))
        elif item == "user":
            streams.append(await _users(ctx))
        elif item == "alert_rule":
            streams.append(await _alert_rules(ctx))
        elif item in NAME_KINDS and item not in _SEARCHED:
            streams.append(await _named(ctx, item))
        elif item == plan_kinds[0]:
            # All plan kinds share one search, ranked together.
            streams.append(await _searched(ctx, plan_kinds))
    return _interleave(streams, ctx.limit)
