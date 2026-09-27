"""Plan links inside docs catalog notes (F22, GH #299).

A note names plan entities with ``[[event:NAME]]``, ``[[event-type:NAME]]``,
``[[field:NAME]]`` or ``[[field:EVENT_TYPE/NAME]]``, each with an optional
label (``[[event:signup|the signup event]]``). Links inside fenced or inline
code are text, not links.

A link is stored by NAME (``DocLink``), never by id, so it survives branches and
re-imports. Whether it resolves is computed on every read against the project's
MAIN branch: an event that is renamed shows up as a broken-link warning instead
of silently pointing at a stale id. Routes match the search index's builders
(``services/_search_documents.py``), so a link opens the same page a search hit
does.
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
from tripl.schemas.docs import DocAudience, DocBacklinkItem, DocLinkKind, DocLinkResolution
from tripl.services.docs_paths import MAX_LINKS_PER_FILE, DocScope
from tripl.services.plan_branch_service import resolve_branch_id

LINK_PATTERN = re.compile(
    r"\[\[(event|event-type|field):([^\]|\n]{1,500})(?:\|([^\]\n]{1,200}))?\]\]"
)
_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.DOTALL)

_KIND_BY_SYNTAX: dict[str, DocLinkKind] = {
    "event": "event",
    "event-type": "event_type",
    "field": "field",
}
SYNTAX_BY_KIND: dict[str, str] = {kind: syntax for syntax, kind in _KIND_BY_SYNTAX.items()}


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


def split_target(kind: DocLinkKind, value: str) -> tuple[str, str | None]:
    """``(target, qualifier)`` — only a field splits ``TYPE/NAME``."""
    text = value.strip()
    if kind == "field" and "/" in text:
        qualifier, _, name = text.partition("/")
        if qualifier.strip() and name.strip():
            return name.strip(), qualifier.strip()
    return text, None


def raw_link(kind: str, target: str, qualifier: str | None) -> str:
    syntax = SYNTAX_BY_KIND.get(kind, kind)
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
    """``[[kind:name|label]]`` as ``name label``, for the search index body."""

    def _plain(match: re.Match[str]) -> str:
        kind = _KIND_BY_SYNTAX[match.group(1)]
        target, qualifier = split_target(kind, match.group(2))
        parts = [qualifier, target, (match.group(3) or "").strip()]
        return " ".join(part for part in parts if part)

    return LINK_PATTERN.sub(_plain, body)


def parse_ref(ref: str) -> tuple[DocLinkKind, str, str | None]:
    """``kind:target`` as used by ``GET /docs/links?ref=``; ValueError when malformed."""
    syntax, sep, value = ref.partition(":")
    kind = _KIND_BY_SYNTAX.get(syntax.strip())
    if not sep or kind is None:
        raise ValueError(f"'{ref}' is not kind:name with kind event, event-type or field")
    target, qualifier = split_target(kind, value)
    if not target or len(target) > 500:
        raise ValueError(f"'{ref}' has no name")
    return kind, target, qualifier


@dataclass(frozen=True)
class LinkRef:
    kind: DocLinkKind
    target: str
    qualifier: str | None = None


async def resolve_links(
    session: AsyncSession, project: Project, refs: Sequence[LinkRef]
) -> list[DocLinkResolution]:
    """Resolve each ref against the project's main plan, in input order.

    One query per kind, whatever the number of refs.
    """
    if not refs:
        return []
    branch_id = await resolve_branch_id(session, project.id, None)
    slug = project.slug

    event_names = {ref.target for ref in refs if ref.kind == "event"}
    events: dict[str, list[tuple[uuid.UUID, str]]] = {}
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
            events.setdefault(name, []).append((event_id, name))

    type_names = {ref.target for ref in refs if ref.kind == "event_type"} | {
        ref.qualifier for ref in refs if ref.kind == "field" and ref.qualifier
    }
    types: dict[str, uuid.UUID] = {}
    if type_names:
        type_rows = await session.execute(
            select(EventType.id, EventType.name).where(
                EventType.project_id == project.id,
                EventType.branch_id == branch_id,
                EventType.name.in_(type_names),
            )
        )
        types = {name: type_id for type_id, name in type_rows}

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

    out: list[DocLinkResolution] = []
    for ref in refs:
        raw = raw_link(ref.kind, ref.target, ref.qualifier)
        entity_id: uuid.UUID | None = None
        route: str | None = None
        candidates = 0
        if ref.kind == "event":
            matches = events.get(ref.target, [])
            candidates = len(matches)
            if matches:
                entity_id = matches[0][0]
                route = f"/p/{slug}/monitoring/event/{entity_id}"
        elif ref.kind == "event_type":
            type_id = types.get(ref.target)
            candidates = 1 if type_id else 0
            if type_id:
                entity_id = type_id
                route = f"/p/{slug}/events/{ref.target}"
        else:
            field_matches = [
                match
                for match in fields.get(ref.target, [])
                if ref.qualifier is None or match[2] == ref.qualifier
            ]
            candidates = len(field_matches)
            if field_matches:
                entity_id = field_matches[0][0]
                route = f"/p/{slug}/event-types/{field_matches[0][1]}"
        status = "broken" if candidates == 0 else "resolved" if candidates == 1 else "ambiguous"
        out.append(
            DocLinkResolution(
                kind=ref.kind,
                target=ref.target,
                qualifier=ref.qualifier,
                raw=raw,
                status=status,
                route_path=route,
                entity_id=entity_id,
                candidates=candidates,
            )
        )
    return out


def link_warning(resolution: DocLinkResolution) -> str | None:
    """The human sentence for a broken or ambiguous link; None when it resolves."""
    noun = {"event": "event", "event_type": "event type", "field": "field"}[resolution.kind]
    name = resolution.target
    if resolution.status == "broken":
        where = f" on event type '{resolution.qualifier}'" if resolution.qualifier else ""
        return f"Broken link {resolution.raw}: no {noun} named '{name}'{where} on the main plan"
    if resolution.status == "ambiguous":
        return (
            f"Ambiguous link {resolution.raw}: {resolution.candidates} {noun}s are named "
            f"'{name}' on the main plan; it opens the first"
        )
    return None


async def backlinks(
    session: AsyncSession,
    project: Project,
    kind: DocLinkKind,
    name: str,
    qualifier: str | None = None,
) -> list[DocBacklinkItem]:
    """Notes in this project or its organization that link to ``kind:name``.

    A qualified field (``checkout/amount``) also matches unqualified links to the
    same field name, since those may mean it; an unqualified query matches every
    link to the name.
    """
    conditions = [DocLink.kind == kind, DocLink.target == name.strip()]
    if kind == "field" and qualifier:
        conditions.append(or_(DocLink.qualifier.is_(None), DocLink.qualifier == qualifier))
    rows = await session.execute(
        select(DocFile, DocLink.target, DocLink.qualifier)
        .join(DocLink, DocLink.doc_file_id == DocFile.id)
        .where(
            *conditions,
            or_(
                DocFile.project_id == project.id,
                DocFile.organization_id == project.organization_id,
            ),
        )
        .order_by(DocFile.organization_id.is_not(None), func.lower(DocFile.path))
    )
    items: list[DocBacklinkItem] = []
    seen: set[uuid.UUID] = set()
    for doc, target, link_qualifier in rows:
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
                link_raw=raw_link(kind, target, link_qualifier),
            )
        )
    return items
