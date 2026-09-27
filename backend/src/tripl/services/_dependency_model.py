"""The value types of the dependency graph and the words it is described in.

Split out of ``dependency_service`` so the resolvers (``_dependency_edges``) and
the entry points share one definition of an edge, a link and a summary.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import cast, get_args

from tripl.schemas.dependency import (
    DependencyCertainty,
    DependencyEdgeKind,
    DependencyEntityKind,
)

__all__ = [
    "ENTITY_KINDS",
    "Dependencies",
    "Edge",
    "EntityRef",
    "counts_by_kind",
    "dedupe_edges",
    "impact_summary",
    "possible_counts_by_kind",
    "parse_entity_ref",
    "url_for",
]

ENTITY_KINDS: frozenset[str] = frozenset(get_args(DependencyEntityKind))

# The order a summary lists kinds in: what an analyst loses first.
_SUMMARY_ORDER: tuple[str, ...] = (
    "metric",
    "alert_rule",
    "event",
    "event_type",
    "field",
    "relation",
    "variable",
    "fact_table",
    "scan_config",
    "detection_override",
)

_LABELS: dict[str, tuple[str, str]] = {
    "event": ("event", "events"),
    "event_type": ("event type", "event types"),
    "field": ("field", "fields"),
    "variable": ("variable", "variables"),
    "metric": ("metric", "metrics"),
    "fact_table": ("fact table", "fact tables"),
    "alert_rule": ("alert rule", "alert rules"),
    "relation": ("relation", "relations"),
    "scan_config": ("scan", "scans"),
    "detection_override": ("detection override", "detection overrides"),
}


@dataclass(frozen=True, slots=True)
class EntityRef:
    kind: DependencyEntityKind
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Edge:
    kind: DependencyEdgeKind
    id: uuid.UUID
    name: str
    relation: str
    certainty: DependencyCertainty = "direct"
    url_hint: str | None = None
    depth: int = 1


@dataclass(slots=True)
class Dependencies:
    entity: EntityRef
    name: str | None = None
    exists: bool = True
    upstream: list[Edge] = field(default_factory=list)
    downstream: list[Edge] = field(default_factory=list)


def parse_entity_ref(raw: str) -> EntityRef:
    """``"event:<uuid>"`` → ``EntityRef``; ``ValueError`` on anything else."""
    kind, sep, raw_id = raw.partition(":")
    if not sep or kind not in ENTITY_KINDS:
        raise ValueError(f"entity must be '<kind>:<id>' with kind one of {sorted(ENTITY_KINDS)}")
    try:
        entity_id = uuid.UUID(raw_id.strip())
    except ValueError as exc:
        raise ValueError("entity id must be a UUID") from exc
    return EntityRef(kind=cast(DependencyEntityKind, kind), id=entity_id)


def url_for(
    slug: str | None,
    kind: str,
    entity_id: uuid.UUID,
    *,
    event_type_name: str | None = None,
    event_type_id: uuid.UUID | None = None,
) -> str | None:
    """The page an edge links to, mirroring ``_search_documents``' route paths."""
    if not slug:
        return None
    match kind:
        case "event":
            if event_type_name:
                return f"/p/{slug}/events/{event_type_name}/{entity_id}"
            return f"/p/{slug}/monitoring/event/{entity_id}"
        case "event_type":
            return f"/p/{slug}/event-types/{entity_id}"
        case "field":
            return f"/p/{slug}/event-types/{event_type_id}" if event_type_id else None
        case "variable":
            return f"/p/{slug}/variables/{entity_id}"
        case "metric":
            return f"/p/{slug}/monitoring/metric/{entity_id}"
        case "fact_table":
            return f"/p/{slug}/metrics/fact-tables/{entity_id}/edit"
        case "alert_rule":
            return f"/p/{slug}/monitors/{entity_id}"
        case "relation":
            return f"/p/{slug}/relations"
        case "scan_config":
            return f"/p/{slug}/scans/{entity_id}"
        case "detection_override":
            # Overrides are listed (and undone) in the project's Detection
            # settings; there is no per-override page.
            return f"/p/{slug}/settings/monitoring"
    return None


def dedupe_edges(edges: Iterable[Edge], *, exclude: EntityRef | None = None) -> list[Edge]:
    """One edge per (kind, id), the strongest reading kept, sorted for display.

    When two resolvers reach the same object, a ``direct`` edge beats a
    ``possible`` one and a nearer hop beats a farther one; the first relation
    seen for the winning reading is kept.
    """
    best: dict[tuple[str, uuid.UUID], Edge] = {}
    for edge in edges:
        if exclude is not None and edge.kind == exclude.kind and edge.id == exclude.id:
            continue
        key = (edge.kind, edge.id)
        current = best.get(key)
        if current is None or _rank(edge) < _rank(current):
            best[key] = edge
    return sorted(
        best.values(),
        key=lambda e: (e.depth, _kind_position(e.kind), e.certainty != "direct", e.name.lower()),
    )


def _rank(edge: Edge) -> tuple[int, int]:
    return (0 if edge.certainty == "direct" else 1, edge.depth)


def _kind_position(kind: str) -> int:
    try:
        return _SUMMARY_ORDER.index(kind)
    except ValueError:
        return len(_SUMMARY_ORDER)


def _count(edges: Iterable[Edge]) -> dict[str, int]:
    counted: Counter[str] = Counter(edge.kind for edge in edges)
    return {kind: counted[kind] for kind in _SUMMARY_ORDER if counted[kind]}


def counts_by_kind(edges: Sequence[Edge]) -> dict[str, int]:
    """First-hop ``direct`` edges per kind: the "Used by" header count.

    A name-only match or a neighbour's neighbour is not "used by" this entity,
    so neither inflates the headline; ``possible_counts_by_kind`` has the rest.
    """
    return _count(e for e in edges if e.certainty == "direct" and e.depth == 1)


def possible_counts_by_kind(edges: Sequence[Edge]) -> dict[str, int]:
    """First-hop ``possible`` (name-match) edges per kind, counted apart."""
    return _count(e for e in edges if e.certainty == "possible" and e.depth == 1)


def _phrase(edges: Sequence[Edge]) -> str:
    counted: Counter[str] = Counter(edge.kind for edge in edges)
    parts = []
    for kind in _SUMMARY_ORDER:
        count = counted.get(kind, 0)
        if not count:
            continue
        singular, plural = _LABELS[kind]
        parts.append(f"{count} {singular if count == 1 else plural}")
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def impact_summary(edges: Sequence[Edge]) -> str:
    """ "2 metrics and 1 alert rule", with name-only matches counted apart.

    ``possible`` edges are a name match, not a stored reference, so they are
    never folded into the headline count: "2 metrics, plus 1 fact table that
    may use it". An empty list reads "Nothing depends on it".
    """
    direct = [edge for edge in edges if edge.certainty == "direct"]
    possible = [edge for edge in edges if edge.certainty == "possible"]
    if not direct and not possible:
        return "Nothing depends on it"
    if not possible:
        return _phrase(direct)
    maybe = f"{_phrase(possible)} that may use it"
    if not direct:
        return maybe
    return f"{_phrase(direct)}, plus {maybe}"
