"""Every path template the two surfaces can build, assembled by resource.

``cli/tests/test_contract.py`` checks each against ``backend/openapi.json`` and
then checks that no ``.py`` in ``tripl_cli`` or ``tripl_mcp`` spells a REST path
that is not in ``ALL_TEMPLATES``. The per-resource ``ENDPOINTS`` tuples are built
from the very constants the builders ``.format()``, so the declaration and the
request cannot drift.

This does NOT replace ``diagnostics/endpoints.py`` or ``tripl_mcp.contract``.
Those two keep their own shapes because their tests assert different things — the
MCP's keys must equal the registered tool names, the CLI's are snapshot sections.
What is shared is the literal, not the declaration.
"""

from __future__ import annotations

from tripl_cli.api import (
    auth,
    branches,
    chart_annotations,
    data_sources,
    event_types,
    events,
    monitoring,
    projects,
    scans,
    search,
    variables,
)

SHARED_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "auth": auth.ENDPOINTS,
    "branches": branches.ENDPOINTS,
    "chart_annotations": chart_annotations.ENDPOINTS,
    "data_sources": data_sources.ENDPOINTS,
    "event_types": event_types.ENDPOINTS,
    "events": events.ENDPOINTS,
    "monitoring": monitoring.ENDPOINTS,
    "projects": projects.ENDPOINTS,
    "scans": scans.ENDPOINTS,
    "search": search.ENDPOINTS,
    "variables": variables.ENDPOINTS,
}

ALL_TEMPLATES: frozenset[str] = frozenset(
    path for endpoints in SHARED_ENDPOINTS.values() for _, path in endpoints
)
