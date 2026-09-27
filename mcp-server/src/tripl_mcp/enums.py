"""The closed value sets the tool schemas carry, mirrored from the API.

A tool parameter annotated ``str`` generates a JSON schema with no ``enum``, so
an agent choosing ``order_by="newest"`` learns it was wrong from a 422 the route
answers: a wasted round trip, and an error it has to parse instead of a value
its own tool schema would never have let it emit. Annotated as a ``Literal`` the
constraint reaches the schema — FastMCP builds the schema with Pydantic, which
renders a ``Literal`` as ``{"type": "string", "enum": [...]}`` and a
``list[Literal]`` as an array whose ``items`` carry the same — so the client
rejects the bad value locally, before any request (tripl-i0vd).

These are the same enums ``tripl_cli.api`` already mirrors for the CLI's
argparse ``choices=``, and they are spelled a second time here of necessity
rather than by preference: a ``Literal``'s members must be static, so
``Literal[*events.STATUSES]`` is not a type and no amount of importing avoids
the transcription. What CAN be avoided is trusting it. Every set below is held
to the same source of truth the CLI's copy is held to —
``tests/test_contract.py`` reads each one out of ``backend/openapi.json`` and
also asserts it against the CLI's tuple, so the two surfaces cannot end up
mirroring one route with two vocabularies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

# ``EventStatus``, verbatim from the OpenAPI document: the lifecycle an event
# moves through. ``list_events`` filters on it and ``create_event`` sets it, and
# both mean the same seven states, so both read this one name.
EventStatus = Literal[
    "draft",
    "in_review",
    "ready_for_dev",
    "implemented",
    "live",
    "deprecated",
    "archived",
]

# ``GET /events?order_by``, which the route declares as a bare ``Literal``
# rather than as a named schema — so this is read off the parameter itself.
# "catalog" is the authored order, and what omitting the parameter gets;
# "volume" ranks busiest-first by ingested volume over the last 24h;
# "health" ranks least healthy first (main plan only; a branch gets a 400).
EventOrderBy = Literal["catalog", "volume", "health"]

# ``SearchResult.entity_type``, verbatim — the same list the search route
# validates its ``types`` filter against. Plan content (event, event_type,
# field, ...), project configuration (scan_config, alert_rule) and docs-catalog
# notes (doc) alike.
SearchEntityType = Literal[
    "event",
    "event_type",
    "field",
    "meta_field",
    "variable",
    "relation",
    "tag",
    "metric",
    "fact_table",
    "scan_config",
    "alert_rule",
    "doc",
]

# ``DocScope``, verbatim: the ``scope`` query parameter every docs route takes.
# A project's own notes, or the notes of its organization, which every project
# of that organization shows. Mirrors ``tripl_cli.api.docs.SCOPES``.
DocScope = Literal["project", "organization"]

# ``DocAudience``, verbatim: who a note is written for, from its frontmatter.
# Mirrors ``tripl_cli.api.docs.AUDIENCES``.
DocAudience = Literal["human", "agent", "both"]


def as_strings(values: Sequence[str] | None) -> list[str] | None:
    """Widen a list of ``Literal`` members to the ``list[str]`` the builder takes.

    ``tripl_cli.api`` spells its repeatable filters ``list[str] | None``, and
    ``list`` is invariant, so a ``list[EventStatus]`` is not assignable to one
    however obviously every member is a ``str``. The parameter is a ``Sequence``
    here precisely because that one IS covariant, which makes the widening a
    plain call rather than a cast, and puts the explanation in one place instead
    of at each tool that had to do it (tripl-i0vd).
    """
    return None if values is None else list(values)
