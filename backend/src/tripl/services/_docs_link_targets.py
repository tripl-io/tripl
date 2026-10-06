"""Resolving the docs catalog links beyond the plan (F24 part 2, GH #308).

``docs_links`` parses every ``[[kind:target]]`` and resolves event, event type
and field; this module resolves the rest, one query per kind:

* by NAME, with up to three relink suggestions when the name is gone:
  ``variable`` (on the main plan), ``metric``, ``branch``, ``scan`` (project)
  and ``data-source`` (the sources the project may use,
  ``data_source_scope.usable_by_project_clause``);
* by id: ``alert-rule`` (a rule of one of the project's destinations), ``user``
  (a member of the project's organization, rendered ``@Name``) and ``doc``.

A note link resolves only for a reader who may see the target
(``docs_access.visible_docs_clause``): anyone else gets ``unavailable`` with no
title, path, route or id, so the link reveals nothing but its own text. A
missing note answers exactly the same (``unavailable``, no reason), so a link
is never an oracle for whether a hidden note with some id exists.

:func:`normalize_doc_links` is the SAVE-time step that turns a hand-typed
``[[doc:path/to/note.md]]`` into the id form, when the path names a note the
author can read; otherwise the text is left alone and reads as a broken link.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Select, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.doc_file import DocFile
from tripl.models.metric_definition import MetricDefinition
from tripl.models.organization import OrganizationMember
from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.schemas.docs import DocLinkReason, DocLinkResolution
from tripl.services._docs_link_similar import NamePool, SuggestionBudget
from tripl.services.data_source_scope import usable_by_project_clause
from tripl.services.docs_access import visible_docs_clause
from tripl.services.docs_frontmatter import split_frontmatter
from tripl.services.docs_links import (
    MAX_POOL,
    LinkRef,
    canonical_id,
    extract_links,
    raw_link,
    status_of,
)
from tripl.services.docs_paths import DocPathError, DocScope, normalize_doc_path, path_key
from tripl.services.project_links import project_url

_SCOPE_LABEL: dict[str, str] = {"project": "Project notes", "organization": "Organization notes"}


@dataclass(frozen=True)
class _Found:
    """One entity a link may point at."""

    id: uuid.UUID
    label: str
    detail: str | None
    route: str | None


@dataclass(frozen=True)
class _NameKind:
    """How one name-stored kind is looked up: its rows, and where each one opens."""

    #: ``select(id, name, label, detail)`` over the rows the project may link to.
    rows: Callable[[Project, uuid.UUID], Select[*tuple[Any, ...]]]
    #: The page of one row, from its id (org-qualified project path or app path).
    route: Callable[[str, str, uuid.UUID], str]


def _variable_rows(project: Project, branch_id: uuid.UUID) -> Select[*tuple[Any, ...]]:
    return select(Variable.id, Variable.name, Variable.name, Variable.variable_type).where(
        Variable.project_id == project.id, Variable.branch_id == branch_id
    )


def _metric_rows(project: Project, _branch_id: uuid.UUID) -> Select[*tuple[Any, ...]]:
    return select(
        MetricDefinition.id,
        MetricDefinition.name,
        MetricDefinition.display_name,
        MetricDefinition.name,
    ).where(MetricDefinition.project_id == project.id)


def _branch_rows(project: Project, _branch_id: uuid.UUID) -> Select[*tuple[Any, ...]]:
    return select(PlanBranch.id, PlanBranch.name, PlanBranch.name, PlanBranch.kind).where(
        PlanBranch.project_id == project.id
    )


def _scan_rows(project: Project, _branch_id: uuid.UUID) -> Select[*tuple[Any, ...]]:
    return (
        select(ScanConfig.id, ScanConfig.name, ScanConfig.name, DataSource.name)
        .join(DataSource, DataSource.id == ScanConfig.data_source_id)
        .where(ScanConfig.project_id == project.id)
    )


def _data_source_rows(project: Project, _branch_id: uuid.UUID) -> Select[*tuple[Any, ...]]:
    return select(DataSource.id, DataSource.name, DataSource.name, DataSource.db_type).where(
        usable_by_project_clause(project.id, project.organization_id)
    )


NAME_KINDS: dict[str, _NameKind] = {
    "variable": _NameKind(
        _variable_rows, lambda org, slug, id_: project_url(org, slug, f"/variables/{id_}")
    ),
    "metric": _NameKind(
        _metric_rows, lambda org, slug, id_: project_url(org, slug, f"/monitoring/metric/{id_}")
    ),
    "branch": _NameKind(
        _branch_rows, lambda org, slug, id_: project_url(org, slug, f"/branches/{id_}")
    ),
    "scan": _NameKind(_scan_rows, lambda org, slug, id_: project_url(org, slug, f"/scans/{id_}")),
    # Data sources are organization settings, not a project page.
    "data_source": _NameKind(
        _data_source_rows, lambda _org, _slug, id_: f"/settings/data-sources/{id_}"
    ),
}
_NAME_COLUMN_INDEX = 1


def _resolution(
    ref: LinkRef,
    *,
    status: str,
    found: _Found | None = None,
    candidates: int = 0,
    reason: DocLinkReason | None = None,
    suggestions: Sequence[str] = (),
) -> DocLinkResolution:
    return DocLinkResolution.model_validate(
        {
            "kind": ref.kind,
            "target": ref.target,
            "qualifier": ref.qualifier,
            "raw": raw_link(ref.kind, ref.target, ref.qualifier),
            "status": status,
            "route_path": found.route if found else None,
            "entity_id": found.id if found else None,
            "candidates": candidates,
            "label": found.label if found else None,
            "detail": found.detail if found else None,
            "reason": reason,
            "suggestions": list(suggestions),
        }
    )


async def _resolve_names(
    session: AsyncSession,
    project: Project,
    kind: str,
    refs: Sequence[LinkRef],
    *,
    branch_id: uuid.UUID,
    org_slug: str,
    budget: SuggestionBudget,
) -> dict[LinkRef, DocLinkResolution]:
    spec = NAME_KINDS[kind]
    base = spec.rows(project, branch_id)
    name_column = base.selected_columns[_NAME_COLUMN_INDEX]
    rows = await session.execute(
        base.where(name_column.in_({ref.target for ref in refs})).order_by(
            name_column, base.selected_columns[0]
        )
    )
    by_name: dict[str, list[_Found]] = {}
    for entity_id, name, label, detail in rows:
        by_name.setdefault(name, []).append(
            _Found(
                id=entity_id,
                label=str(label or name),
                detail=str(detail) if detail else None,
                route=spec.route(org_slug, project.slug, entity_id),
            )
        )
    pool: NamePool | None = None
    out: dict[LinkRef, DocLinkResolution] = {}
    for ref in refs:
        matches = by_name.get(ref.target, [])
        status = status_of(len(matches))
        suggestions: list[str] = []
        if status == "broken" and budget.take():
            if pool is None:
                names = await session.scalars(
                    base.with_only_columns(name_column).distinct().limit(MAX_POOL)
                )
                pool = NamePool(str(name) for name in names.all())
            suggestions = pool.closest(ref.target)
        out[ref] = _resolution(
            ref,
            status=status,
            found=matches[0] if matches else None,
            candidates=len(matches),
            reason="not_found" if status == "broken" else None,
            suggestions=suggestions,
        )
    return out


def _ids(refs: Sequence[LinkRef]) -> set[uuid.UUID]:
    return {uuid.UUID(value) for ref in refs if (value := canonical_id(ref.target)) is not None}


async def _resolve_alert_rules(
    session: AsyncSession, project: Project, refs: Sequence[LinkRef], org_slug: str
) -> dict[LinkRef, DocLinkResolution]:
    found: dict[str, _Found] = {}
    ids = _ids(refs)
    if ids:
        rows = await session.execute(
            select(AlertRule.id, AlertRule.name, AlertDestination.name)
            .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
            .where(AlertDestination.project_id == project.id, AlertRule.id.in_(ids))
        )
        for rule_id, name, destination in rows:
            found[str(rule_id)] = _Found(
                id=rule_id,
                label=name,
                detail=destination,
                route=project_url(org_slug, project.slug, f"/monitors/{rule_id}"),
            )
    out: dict[LinkRef, DocLinkResolution] = {}
    for ref in refs:
        hit = found.get(ref.target)
        if hit is not None:
            out[ref] = _resolution(ref, status="resolved", found=hit, candidates=1)
            continue
        reason: DocLinkReason = "not_found" if canonical_id(ref.target) else "invalid_id"
        out[ref] = _resolution(ref, status="broken", reason=reason)
    return out


async def _resolve_users(
    session: AsyncSession, project: Project, refs: Sequence[LinkRef]
) -> dict[LinkRef, DocLinkResolution]:
    """A mention resolves to a member of the project's organization, and to nobody else.

    A user outside it (or no user at all) reads the same, so a note cannot be
    used to learn whether an id is somebody's.
    """
    found: dict[str, _Found] = {}
    ids = _ids(refs)
    if ids:
        rows = await session.execute(
            select(User.id, User.name, User.email)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .where(
                OrganizationMember.organization_id == project.organization_id,
                User.id.in_(ids),
            )
        )
        for user_id, name, email in rows:
            found[str(user_id)] = _Found(
                id=user_id, label=f"@{name or email}", detail=None, route=None
            )
    out: dict[LinkRef, DocLinkResolution] = {}
    for ref in refs:
        hit = found.get(ref.target)
        if hit is not None:
            out[ref] = _resolution(ref, status="resolved", found=hit, candidates=1)
        else:
            reason: DocLinkReason = "not_a_member" if canonical_id(ref.target) else "invalid_id"
            out[ref] = _resolution(ref, status="broken", reason=reason)
    return out


def _any_scope(project: Project) -> ColumnElement[bool]:
    return or_(DocFile.project_id == project.id, DocFile.organization_id == project.organization_id)


def _scope_of(doc: DocFile) -> DocScope:
    return "project" if doc.project_id is not None else "organization"


def _doc_route(org_slug: str, project: Project, doc: DocFile, anchor: str | None) -> str:
    fragment = f"#{anchor}" if anchor else ""
    return project_url(org_slug, project.slug, f"/docs/{_scope_of(doc)}/{doc.path}{fragment}")


def _typed_path_key(raw_path: str) -> str | None:
    """The ``path_key`` a hand-typed note path names (``.md`` added when missing), or None."""
    text = raw_path.strip().lstrip("/")
    if text and not text.lower().endswith(".md"):
        text = f"{text}.md"
    try:
        return path_key(normalize_doc_path(text))
    except DocPathError:
        return None


async def readable_docs_at_paths(
    session: AsyncSession,
    project: Project,
    raw_paths: Iterable[str],
    user_id: uuid.UUID | None,
) -> dict[str, DocFile]:
    """The note each of ``raw_paths`` names that the reader may see, in ONE query.

    The project's note wins over its organization's at the same path. A path
    that is not a note path, or names no note visible to ``user_id``, is absent
    from the result.
    """
    keys = {raw: key for raw in raw_paths if (key := _typed_path_key(raw)) is not None}
    if not keys:
        return {}
    rows = await session.scalars(
        select(DocFile).where(
            _any_scope(project),
            DocFile.path_key.in_(set(keys.values())),
            visible_docs_clause(user_id),
        )
    )
    by_key: dict[str, DocFile] = {}
    for doc in rows:
        held = by_key.get(doc.path_key)
        if held is None or (held.project_id is None and doc.project_id is not None):
            by_key[doc.path_key] = doc
    return {raw: by_key[key] for raw, key in keys.items() if key in by_key}


async def _resolve_docs(
    session: AsyncSession,
    project: Project,
    refs: Sequence[LinkRef],
    *,
    org_slug: str,
    user_id: uuid.UUID | None,
    budget: SuggestionBudget,
) -> dict[LinkRef, DocLinkResolution]:
    docs: dict[str, tuple[DocFile, bool]] = {}
    ids = _ids(refs)
    if ids:
        readable = case((visible_docs_clause(user_id), True), else_=False)
        rows = await session.execute(
            select(DocFile, readable).where(DocFile.id.in_(ids), _any_scope(project))
        )
        docs = {str(doc.id): (doc, bool(can_read)) for doc, can_read in rows}
    # A hand-typed path the save could not turn into an id: offer the note now
    # at that path, when this reader may see one. One query for every path, and
    # only while the suggestion budget lasts.
    typed = [ref.target for ref in refs if canonical_id(ref.target) is None]
    offered = [path for path in dict.fromkeys(typed) if budget.take()]
    at_paths = await readable_docs_at_paths(session, project, offered, user_id)
    out: dict[LinkRef, DocLinkResolution] = {}
    for ref in refs:
        if canonical_id(ref.target) is None:
            at_path = at_paths.get(ref.target)
            resolution = _resolution(ref, status="broken", reason="path_form")
            if at_path is not None:
                # A note this reader can read, so its title may label the
                # suggestion ("save to link 'Setup guide'").
                resolution = resolution.model_copy(
                    update={
                        "label": at_path.title or at_path.path,
                        "suggestions": [str(at_path.id)],
                    }
                )
            out[ref] = resolution
            continue
        entry = docs.get(ref.target)
        if entry is None or not entry[1]:
            # Missing and hidden read the same: nothing tells them apart.
            out[ref] = _resolution(ref, status="unavailable")
            continue
        doc, _can_read = entry
        found = _Found(
            id=doc.id,
            label=doc.title or doc.path,
            detail=f"{_SCOPE_LABEL[_scope_of(doc)]} · {doc.path}",
            route=_doc_route(org_slug, project, doc, ref.qualifier),
        )
        out[ref] = _resolution(ref, status="resolved", found=found, candidates=1)
    return out


async def resolve(
    session: AsyncSession,
    project: Project,
    refs: Sequence[LinkRef],
    *,
    branch_id: uuid.UUID,
    org_slug: str,
    user_id: uuid.UUID | None,
    budget: SuggestionBudget,
) -> dict[LinkRef, DocLinkResolution]:
    """Every ref of a kind this module owns, keyed by the ref.

    ``budget`` is shared with the plan kinds: it caps how many broken links of
    one response get relink suggestions.
    """
    by_kind: dict[str, list[LinkRef]] = {}
    for ref in refs:
        by_kind.setdefault(ref.kind, []).append(ref)
    out: dict[LinkRef, DocLinkResolution] = {}
    for kind, kind_refs in by_kind.items():
        if kind in NAME_KINDS:
            out.update(
                await _resolve_names(
                    session,
                    project,
                    kind,
                    kind_refs,
                    branch_id=branch_id,
                    org_slug=org_slug,
                    budget=budget,
                )
            )
        elif kind == "alert_rule":
            out.update(await _resolve_alert_rules(session, project, kind_refs, org_slug))
        elif kind == "user":
            out.update(await _resolve_users(session, project, kind_refs))
        elif kind == "doc":
            out.update(
                await _resolve_docs(
                    session,
                    project,
                    kind_refs,
                    org_slug=org_slug,
                    user_id=user_id,
                    budget=budget,
                )
            )
        else:  # pragma: no cover - every DocLinkKind is handled above or in docs_links
            raise ValueError(f"Unknown link kind {kind}")
    return out


async def normalize_doc_links(
    session: AsyncSession, project: Project, content: str, user_id: uuid.UUID
) -> str:
    """``content`` with every hand-typed ``[[doc:path]]`` rewritten to ``[[doc:<id>]]``.

    Only paths naming a note ``user_id`` may read are rewritten; the anchor and
    the label are kept. Anything else is left exactly as typed (it reads as a
    broken link). Links in the frontmatter or in code are never touched.
    """
    _yaml, body = split_frontmatter(content)
    if "[[doc:" not in body or not content.endswith(body):
        # No candidate, or a CRLF frontmatter whose body is not a suffix of the
        # raw content: nothing that can be rewritten in place.
        return content
    prefix = content[: len(content) - len(body)]
    typed = [
        link
        for link in extract_links(body)
        if link.kind == "doc" and canonical_id(link.target) is None
    ]
    at_paths = await readable_docs_at_paths(
        session, project, {link.target for link in typed}, user_id
    )
    replacements: list[tuple[int, int, str]] = []
    for link in typed:
        doc = at_paths.get(link.target)
        if doc is None:
            continue
        text = raw_link("doc", str(doc.id), link.qualifier)
        if link.label:
            text = f"{text[:-2]}|{link.label}]]"
        replacements.append((link.start, link.end, text))
    for start, end, text in reversed(replacements):
        body = body[:start] + text + body[end:]
    return prefix + body
