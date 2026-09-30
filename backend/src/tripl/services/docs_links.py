"""Links inside docs catalog notes (F22, GH #299; F24 part 2, GH #308).

A note links with ``[[kind:target]]`` plus an optional label
(``[[event:signup|the signup event]]``). Links inside fenced or inline code are
text, not links. The kinds:

* plan entities, by NAME: ``event``, ``event-type``, ``field`` (``NAME`` or
  ``EVENT_TYPE/NAME``), ``variable``, ``metric``, ``branch``, ``scan`` and
  ``data-source``;
* by id, so they survive renames and moves: ``doc`` (another note, with an
  optional ``#heading`` anchor), ``alert-rule`` (rule names are not unique) and
  ``user`` (an @mention, rendered ``@Name``).

A link is stored as written (``DocLink``) and resolved on every read, against
the project's MAIN branch for branched plan entities: a renamed event shows up
as a broken link with up to three relink suggestions instead of silently
pointing at a stale id. Routes match the search index's builders
(``services/_search_documents.py``), so a link opens the same page a search hit
does. A link to a note the reader may not see resolves as ``unavailable`` and
names nothing (``services/_docs_link_targets.py``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile, DocLink
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.schemas.docs import (
    DocAudience,
    DocBacklinkItem,
    DocLinkKind,
    DocLinkResolution,
    DocLinkStatus,
)
from tripl.services._docs_link_similar import NamePool, SuggestionBudget
from tripl.services.docs_access import visible_docs_clause
from tripl.services.docs_paths import MAX_LINKS_PER_FILE, DocScope
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_links import project_org_slugs, project_url

_KIND_BY_SYNTAX: dict[str, DocLinkKind] = {
    "event": "event",
    "event-type": "event_type",
    "field": "field",
    "doc": "doc",
    "variable": "variable",
    "metric": "metric",
    "alert-rule": "alert_rule",
    "branch": "branch",
    "scan": "scan",
    "data-source": "data_source",
    "user": "user",
}
SYNTAX_BY_KIND: dict[str, str] = {kind: syntax for syntax, kind in _KIND_BY_SYNTAX.items()}
#: Kinds stored by id; every other kind is stored by name.
ID_KINDS: frozenset[str] = frozenset({"doc", "alert_rule", "user"})
#: Kinds resolved here; the others in ``_docs_link_targets``.
PLAN_KINDS: frozenset[str] = frozenset({"event", "event_type", "field"})

LINK_PATTERN = re.compile(
    r"\[\[("
    + "|".join(re.escape(syntax) for syntax in sorted(_KIND_BY_SYNTAX, key=len, reverse=True))
    + r"):([^\]|\n]{1,500})(?:\|([^\]\n]{1,200}))?\]\]"
)
_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.DOTALL)


@dataclass(frozen=True)
class ParsedLink:
    kind: DocLinkKind
    target: str
    qualifier: str | None
    label: str | None
    start: int
    end: int
    raw: str


def _blank(text: str) -> str:
    """The same text with every character that is not a newline turned into a space.

    Blanking rather than deleting keeps offsets stable, so a link's ``start`` and
    ``end`` still index the original body.
    """
    return re.sub(r"[^\n]", " ", text)


def _strip_code(body: str) -> str:
    lines = body.split("\n")
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        match = _FENCE.match(line)
        if fence is None and match:
            fence = match.group("fence")[0] * len(match.group("fence"))
            out.append(_blank(line))
            continue
        if fence is not None:
            closing = _FENCE.match(line)
            if (
                closing
                and closing.group("fence")[0] == fence[0]
                and len(closing.group("fence")) >= len(fence)
                and not line.strip()[len(closing.group("fence")) :].strip()
            ):
                fence = None
            out.append(_blank(line))
            continue
        out.append(line)
    joined = "\n".join(out)
    return _INLINE_CODE.sub(lambda match: _blank(match.group(0)), joined)


def canonical_id(text: str) -> str | None:
    """The canonical text of a UUID (lower case, hyphenated), or None."""
    try:
        return str(uuid.UUID(text.strip()))
    except ValueError:
        return None


def split_target(kind: DocLinkKind, value: str) -> tuple[str, str | None]:
    """``(target, qualifier)``.

    A field splits ``TYPE/NAME`` (the qualifier is the event type); a note link
    splits ``TARGET#anchor`` (the qualifier is the heading anchor). A target
    stored by id is written in its canonical form, so two spellings of one
    UUID are one link.
    """
    text = value.strip()
    if kind == "field" and "/" in text:
        qualifier, _, name = text.partition("/")
        if qualifier.strip() and name.strip():
            return name.strip(), qualifier.strip()
    if kind == "doc":
        target, _, anchor = text.partition("#")
        target = target.strip()
        return canonical_id(target) or target, anchor.strip() or None
    if kind in ID_KINDS:
        return canonical_id(text) or text, None
    return text, None


def raw_link(kind: str, target: str, qualifier: str | None) -> str:
    """The canonical reference text, without a label."""
    syntax = SYNTAX_BY_KIND.get(kind, kind)
    if kind == "doc":
        return f"[[doc:{target}#{qualifier}]]" if qualifier else f"[[doc:{target}]]"
    name = f"{qualifier}/{target}" if qualifier else target
    return f"[[{syntax}:{name}]]"


def extract_links(body: str) -> list[ParsedLink]:
    """Every plan link in ``body`` outside code, in order, at most ``MAX_LINKS_PER_FILE``."""
    searchable = _strip_code(body)
    links: list[ParsedLink] = []
    for match in LINK_PATTERN.finditer(searchable):
        kind = _KIND_BY_SYNTAX[match.group(1)]
        target, qualifier = split_target(kind, match.group(2))
        if not target:
            continue
        label = match.group(3).strip() if match.group(3) else None
        links.append(
            ParsedLink(
                kind=kind,
                target=target,
                qualifier=qualifier,
                label=label or None,
                start=match.start(),
                end=match.end(),
                raw=body[match.start() : match.end()],
            )
        )
        if len(links) >= MAX_LINKS_PER_FILE:
            break
    return links


def rewrite_links_as_text(body: str) -> str:
    """Links as plain words, for the search index body.

    ``[[kind:name|label]]`` becomes ``name label``. A link stored by id becomes
    its label alone: a UUID is noise to the index, and a note's title must not
    reach another note's index entry (the reader of one may not see the other).
    """

    def _plain(match: re.Match[str]) -> str:
        kind = _KIND_BY_SYNTAX[match.group(1)]
        target, qualifier = split_target(kind, match.group(2))
        label = (match.group(3) or "").strip()
        if kind in ID_KINDS:
            return label
        parts = [qualifier, target, label]
        return " ".join(part for part in parts if part)

    return LINK_PATTERN.sub(_plain, body)


def parse_ref(ref: str) -> tuple[DocLinkKind, str, str | None]:
    """``kind:target`` as used by ``GET /docs/links?ref=``; ValueError when malformed."""
    syntax, sep, value = ref.partition(":")
    kind = _KIND_BY_SYNTAX.get(syntax.strip())
    if not sep or kind is None:
        raise ValueError(
            f"'{ref}' is not kind:target with kind one of {', '.join(_KIND_BY_SYNTAX)}"
        )
    target, qualifier = split_target(kind, value)
    if not target or len(target) > 500:
        raise ValueError(f"'{ref}' has no target")
    return kind, target, qualifier


def kind_of_syntax(syntax: str) -> DocLinkKind | None:
    """``event-type`` -> ``event_type``; None for an unknown prefix."""
    return _KIND_BY_SYNTAX.get(syntax)


@dataclass(frozen=True)
class LinkRef:
    kind: DocLinkKind
    target: str
    qualifier: str | None = None


async def resolve_links(
    session: AsyncSession,
    project: Project,
    refs: Sequence[LinkRef],
    *,
    user_id: uuid.UUID | None = None,
) -> list[DocLinkResolution]:
    """Resolve each ref for the reader ``user_id``, in input order.

    Event, event type and field against the project's main plan (here); every
    other kind in ``_docs_link_targets``. A note link resolves only for a
    reader who may see the note; ``None`` (no reader) sees ``level`` notes
    only. One query per kind, whatever the number of refs. Relink suggestions
    go to the first ``MAX_SUGGESTED_REFS`` broken links only (in input order),
    so a note full of broken links costs a bounded amount per read.
    """
    if not refs:
        return []
    # Imported here: that module builds on this one's parsing helpers.
    from tripl.services import _docs_link_targets

    branch_id = await resolve_branch_id(session, project.id, None)
    # Built per read, never stored: the organization goes straight into the link.
    org_slug = (await project_org_slugs(session, [project.id])).get(project.id, "")
    unique = list(dict.fromkeys(refs))
    budget = SuggestionBudget()
    resolved: dict[LinkRef, DocLinkResolution] = {}
    plan_refs = [ref for ref in unique if ref.kind in PLAN_KINDS]
    if plan_refs:
        resolved.update(
            await _resolve_plan(session, project, plan_refs, branch_id, org_slug, budget)
        )
    other_refs = [ref for ref in unique if ref.kind not in PLAN_KINDS]
    if other_refs:
        resolved.update(
            await _docs_link_targets.resolve(
                session,
                project,
                other_refs,
                branch_id=branch_id,
                org_slug=org_slug,
                user_id=user_id,
                budget=budget,
            )
        )
    return [resolved[ref] for ref in refs]


def status_of(candidates: int) -> DocLinkStatus:
    return "broken" if candidates == 0 else "resolved" if candidates == 1 else "ambiguous"


async def _resolve_plan(
    session: AsyncSession,
    project: Project,
    refs: Sequence[LinkRef],
    branch_id: uuid.UUID,
    org_slug: str,
    budget: SuggestionBudget,
) -> dict[LinkRef, DocLinkResolution]:
    slug = project.slug
    event_names = {ref.target for ref in refs if ref.kind == "event"}
    events: dict[str, list[uuid.UUID]] = {}
    if event_names:
        rows = await session.execute(
            select(Event.id, Event.name)
            .where(
                Event.project_id == project.id,
                Event.branch_id == branch_id,
                Event.name.in_(event_names),
            )
            .order_by(Event.name, Event.id)
        )
        for event_id, name in rows:
            events.setdefault(name, []).append(event_id)

    type_names = {ref.target for ref in refs if ref.kind == "event_type"} | {
        ref.qualifier for ref in refs if ref.kind == "field" and ref.qualifier
    }
    types: dict[str, tuple[uuid.UUID, str]] = {}
    if type_names:
        type_rows = await session.execute(
            select(EventType.id, EventType.name, EventType.display_name).where(
                EventType.project_id == project.id,
                EventType.branch_id == branch_id,
                EventType.name.in_(type_names),
            )
        )
        types = {name: (type_id, display) for type_id, name, display in type_rows}

    field_names = {ref.target for ref in refs if ref.kind == "field"}
    fields: dict[str, list[tuple[uuid.UUID, uuid.UUID, str]]] = {}
    if field_names:
        field_rows = await session.execute(
            select(
                FieldDefinition.id,
                FieldDefinition.event_type_id,
                FieldDefinition.name,
                EventType.name,
            )
            .join(EventType, EventType.id == FieldDefinition.event_type_id)
            .where(
                EventType.project_id == project.id,
                EventType.branch_id == branch_id,
                FieldDefinition.name.in_(field_names),
            )
            .order_by(EventType.name, FieldDefinition.name)
        )
        for field_id, type_id, field_name, type_name in field_rows:
            fields.setdefault(field_name, []).append((field_id, type_id, type_name))

    pools = _PlanNamePools(session, project.id, branch_id)
    out: dict[LinkRef, DocLinkResolution] = {}
    for ref in refs:
        entity_id: uuid.UUID | None = None
        route: str | None = None
        label: str | None = None
        detail: str | None = None
        if ref.kind == "event":
            matches = events.get(ref.target, [])
            candidates = len(matches)
            if matches:
                entity_id = matches[0]
                route = project_url(org_slug, slug, f"/monitoring/event/{entity_id}")
                label = ref.target
        elif ref.kind == "event_type":
            found = types.get(ref.target)
            candidates = 1 if found else 0
            if found:
                entity_id, label = found[0], found[1] or ref.target
                route = project_url(org_slug, slug, f"/events/{ref.target}")
        else:
            field_matches = [
                match
                for match in fields.get(ref.target, [])
                if ref.qualifier is None or match[2] == ref.qualifier
            ]
            candidates = len(field_matches)
            if field_matches:
                entity_id = field_matches[0][0]
                route = project_url(org_slug, slug, f"/event-types/{field_matches[0][1]}")
                label, detail = ref.target, field_matches[0][2]
        status = status_of(candidates)
        suggestions: list[str] = []
        if status == "broken" and budget.take():
            suggestions = (await pools.names(ref, types)).closest(ref.target)
        out[ref] = DocLinkResolution(
            kind=ref.kind,
            target=ref.target,
            qualifier=ref.qualifier,
            raw=raw_link(ref.kind, ref.target, ref.qualifier),
            status=status,
            route_path=route,
            entity_id=entity_id,
            candidates=candidates,
            label=label,
            detail=detail,
            reason="not_found" if status == "broken" else None,
            suggestions=suggestions,
        )
    return out


class _PlanNamePools:
    """The current names a broken plan link may be relinked to, loaded and indexed once per kind."""

    def __init__(self, session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id
        self._branch_id = branch_id
        self._cache: dict[tuple[str, uuid.UUID | None], NamePool] = {}

    async def names(self, ref: LinkRef, types: dict[str, tuple[uuid.UUID, str]]) -> NamePool:
        type_id = (
            types[ref.qualifier][0] if ref.kind == "field" and ref.qualifier in types else None
        )
        key = (ref.kind, type_id)
        if key not in self._cache:
            self._cache[key] = NamePool(await self._load(ref.kind, type_id))
        return self._cache[key]

    async def _load(self, kind: str, type_id: uuid.UUID | None) -> list[str]:
        on_main = (EventType.project_id == self._project_id, EventType.branch_id == self._branch_id)
        if kind == "event":
            stmt = select(Event.name).where(
                Event.project_id == self._project_id, Event.branch_id == self._branch_id
            )
        elif kind == "event_type":
            stmt = select(EventType.name).where(*on_main)
        elif type_id is not None:
            stmt = select(FieldDefinition.name).where(FieldDefinition.event_type_id == type_id)
        else:
            stmt = (
                select(FieldDefinition.name)
                .join(EventType, EventType.id == FieldDefinition.event_type_id)
                .where(*on_main)
            )
        return list((await self._session.scalars(stmt.distinct().limit(MAX_POOL))).all())


#: At most this many current names are compared against a broken link.
MAX_POOL = 20_000

_NOUNS: dict[str, str] = {
    "event": "event",
    "event_type": "event type",
    "field": "field",
    "doc": "note",
    "variable": "variable",
    "metric": "metric",
    "alert_rule": "alert rule",
    "branch": "branch",
    "scan": "scan",
    "data_source": "data source",
    "user": "person",
}
# Plan entities that live on a branch: a name is looked up on the main plan.
_ON_MAIN = frozenset({"event", "event_type", "field", "variable"})


def link_warning(resolution: DocLinkResolution) -> str | None:
    """The human sentence for a link that does not resolve; None when it does."""
    kind = resolution.kind
    noun = _NOUNS[kind]
    raw = resolution.raw
    if resolution.status == "unavailable":
        # One sentence whether the note is gone or hidden from this reader, so
        # the answer never confirms that a hidden note exists.
        return f"Link {raw} points at a note that is unavailable (deleted, or not visible to you)"
    if resolution.status == "ambiguous":
        where = "on the main plan" if kind in _ON_MAIN else "in this project"
        return (
            f"Ambiguous link {raw}: {resolution.candidates} {noun}s are named "
            f"'{resolution.target}' {where}; it opens the first"
        )
    if resolution.status != "broken":
        return None
    hint = f"; did you mean {', '.join(repr(name) for name in resolution.suggestions)}?"
    hint = hint if resolution.suggestions else ""
    if kind == "user":
        return f"Broken mention {raw}: not a member of this organization"
    if kind == "doc":
        if resolution.reason == "path_form" and resolution.suggestions:
            title = f" ('{resolution.label}')" if resolution.label else ""
            return (
                f"Link {raw} names a path, but notes are linked by id: save the note to "
                f"link the note at this path{title} by its id"
            )
        if resolution.reason == "path_form":
            return (
                f"Broken link {raw}: notes are linked by id, "
                "and no note you can read is at this path"
            )
        return f"Broken link {raw}"
    if resolution.reason == "invalid_id":
        return f"Broken link {raw}: an {noun} is linked by its id"
    if kind in ID_KINDS:
        return f"Broken link {raw}: no {noun} with this id in this project"
    where = f" on event type '{resolution.qualifier}'" if resolution.qualifier else ""
    scope = "on the main plan" if kind in _ON_MAIN else "in this project"
    return f"Broken link {raw}: no {noun} named '{resolution.target}'{where} {scope}{hint}"


def backlink_target(kind: DocLinkKind, name: str) -> str:
    """How a backlinks query names its target: a canonical id for id kinds."""
    text = name.strip()
    if kind in ID_KINDS:
        return canonical_id(text) or text
    return text


async def backlinks(
    session: AsyncSession,
    project: Project,
    kind: DocLinkKind,
    name: str,
    qualifier: str | None = None,
    *,
    user_id: uuid.UUID | None,
) -> list[DocBacklinkItem]:
    """Notes in this project or its organization that link to ``kind:name``.

    Only the notes ``user_id`` can see (F24, ``docs_access.visible_docs_clause``);
    ``None`` sees ``level`` notes only. ``name`` is a note's, an alert rule's or a
    user's id for the kinds stored by id.

    A qualified field (``checkout/amount``) also matches unqualified links to the
    same field name, since those may mean it; an unqualified query matches every
    link to the name. A note link matches whatever heading it anchors to, and a
    note never lists itself.
    """
    target = backlink_target(kind, name)
    conditions = [DocLink.kind == kind, DocLink.target == target]
    if kind == "field" and qualifier:
        conditions.append(or_(DocLink.qualifier.is_(None), DocLink.qualifier == qualifier))
    if kind == "doc":
        target_id = canonical_id(target)
        if target_id is not None:
            conditions.append(DocFile.id != uuid.UUID(target_id))
    rows = await session.execute(
        select(DocFile, DocLink.target, DocLink.qualifier)
        .join(DocLink, DocLink.doc_file_id == DocFile.id)
        .where(
            *conditions,
            or_(
                DocFile.project_id == project.id,
                DocFile.organization_id == project.organization_id,
            ),
            visible_docs_clause(user_id),
        )
        .order_by(DocFile.organization_id.is_not(None), func.lower(DocFile.path))
    )
    items: list[DocBacklinkItem] = []
    seen: set[uuid.UUID] = set()
    for doc, link_target, link_qualifier in rows:
        if doc.id in seen:
            continue
        seen.add(doc.id)
        scope: DocScope = "project" if doc.project_id is not None else "organization"
        items.append(
            DocBacklinkItem(
                scope=scope,
                path=doc.path,
                title=doc.title,
                description=doc.description,
                audience=cast(DocAudience, doc.audience),
                link_raw=raw_link(kind, link_target, link_qualifier),
            )
        )
    return items
