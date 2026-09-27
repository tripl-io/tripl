"""Dependency graph and impact analysis (GH #257, F04).

Read-only shapes: nothing here is persisted and nothing here refuses a change.
The one blocking check stays ``services.fact_table_dependents`` (409 on a
fact-table edit that would strand a metric); these answers only WARN.
"""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

# The entities a caller may ask about.
DependencyEntityKind = Literal[
    "event",
    "event_type",
    "field",
    "variable",
    "metric",
    "fact_table",
    "alert_rule",
    "relation",
]

# The entities an edge may point at: every askable kind plus a scan
# configuration, which holds scan-level metric breakdown columns and an event
# type binding but has no "Used by" page of its own, and a detection override
# (``AnomalyScopeOverride``, the false-positive ratchet's per-scope
# sensitivity), which is keyed by an event / event type / metric id and is
# listed in the project's Detection settings.
DependencyEdgeKind = Literal[
    "event",
    "event_type",
    "field",
    "variable",
    "metric",
    "fact_table",
    "alert_rule",
    "relation",
    "scan_config",
    "detection_override",
]

# ``direct``: a stored id or a column name read in the entity's own scope.
# ``possible``: a best-effort name match (an identifier inside free SQL, a fact
# table column or a variable binding that happens to carry the same name). A
# possible edge is a hint for a human; it never blocks anything.
DependencyCertainty = Literal["direct", "possible"]

# ``change`` is what the branch impact reports for an entity edited in place
# (a field's type, an event's filter columns); a caller planning a change
# normally sends one of the other three.
ImpactChangeKind = Literal["delete", "deprecate", "rename", "change"]


class DependencyEdge(BaseModel):
    kind: DependencyEdgeKind
    id: uuid.UUID
    name: str
    # A plain sentence fragment, e.g. "metric uses event in its composition".
    relation: str
    certainty: DependencyCertainty
    # A frontend path (``/p/{slug}/...``) without ``?branch=``; the caller adds
    # the branch it is looking at. Null when there is no page to link to.
    url_hint: str | None = None
    # 1 for a neighbour of the asked entity, 2 for a neighbour of a neighbour
    # (only present when ``depth=2`` was requested).
    depth: int = 1


class DependencyEntity(BaseModel):
    kind: DependencyEntityKind
    id: uuid.UUID
    # Null when the id resolves to nothing (deleted, or never existed).
    name: str | None = None
    exists: bool = True


class DependenciesResponse(BaseModel):
    entity: DependencyEntity
    # What the entity is built on (its event type, the events a metric reads…).
    upstream: list[DependencyEdge]
    # What would be affected by changing or removing it.
    downstream: list[DependencyEdge]
    # First-hop ``direct`` downstream edges per kind, for "Used by" headers.
    counts_by_kind: dict[str, int]
    # First-hop ``possible`` (name-match) downstream edges per kind, counted
    # apart so a hint never inflates the headline.
    possible_counts_by_kind: dict[str, int] = Field(default_factory=dict)


class ImpactChange(BaseModel):
    kind: DependencyEntityKind
    id: uuid.UUID
    change: ImpactChangeKind


class ImpactRequest(BaseModel):
    # 200 matches the frontend's ``IMPACT_MAX_CHANGES`` batch.
    changes: list[ImpactChange] = Field(min_length=1, max_length=200)
    # Accepted for compatibility and IGNORED: an impact answer is always one
    # hop (what a confirm dialog lists), whatever is sent. Two hops over a
    # 200-change batch was the expensive path; ``GET /dependencies?depth=2``
    # is the place to walk further.
    depth: Literal[1, 2] = 1


class ImpactItem(BaseModel):
    change: ImpactChange
    entity: DependencyEntity
    # ``entity.name`` repeated flat, for list rows.
    name: str | None = None
    affected: list[DependencyEdge]
    # "2 metrics and 1 alert rule", with any name-only matches counted apart.
    summary: str


class ImpactResponse(BaseModel):
    items: list[ImpactItem]
