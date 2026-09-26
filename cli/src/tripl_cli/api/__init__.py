"""The one home for REST request construction (tripl-ey6j.5).

Shared by the ``tripl`` CLI commands and by ``tripl-mcp``'s tools. A path
template appears here and nowhere else; ``ApiRequest`` is constructed here and
nowhere else; ``cli/tests/test_contract.py`` and ``mcp-server/tests/test_contract.py``
enforce both, which is what turns "the two surfaces share their request layer"
from a claim into a fact.

Nothing consumer-specific belongs here. No ``mcp`` import — that would invert the
dependency (tripl-mcp depends on tripl, never the reverse) and drag the whole MCP
SDK into every ``uvx tripl`` install, which is the mistake ``errors.py``'s
docstring records being avoided once already. No argparse, no printing, no
rendering, no agent-facing prose.

What lives here is what the API itself IS: paths, parameters, bodies, envelope
shapes, and the compound reads no single endpoint answers (a project's drifts are
a budgeted fan-out over its event types). Response ASSEMBLY stays with the
consumer — the MCP's context-budget projections and its ``ToolAnnotations`` stay
in ``tripl_mcp.tools._common``, and the CLI's tables and JSON documents stay in
``tripl_cli.render`` and ``tripl_cli.report``.

A builder with no CLI caller is normal and expected: the rule is that EVERY path
lives here, so the MCP-only resources have builders too. A partial rule could not
be checked mechanically, and one that cannot be checked is one a contributor can
half-follow.

The one import out of this package is ``tripl_cli.model``, for its untyped-JSON
helpers and ``JOBS_WINDOW``. Those are facts about the wire rather than about
doctor's verdict machinery, which is why that module now sits at the package
root instead of under ``diagnostics`` (tripl-azhh) — this import used to read as
the request layer depending on the doctor.

``page_items``/``page_total`` are RE-EXPORTED from there, not redefined: the
``{items, total}`` envelope is an envelope shape, so it is part of what this
package is about, and ``tripl_cli.api`` is the whole import surface ``tripl_mcp``
has. Before tripl-i1dt that unwrapping was written out by hand at four call
sites — three tool bodies plus the CLI's own watch loop — and they had already
diverged from the definition: the shared reader drops a non-dict row where the
tools passed it through, reading the same routes. ``cli/tests/test_contract.py``
now pins both the identity of the re-export and the rule that nothing outside
this package reads those keys itself.
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
from tripl_cli.api.endpoints import ALL_TEMPLATES, SHARED_ENDPOINTS
from tripl_cli.api.request import ApiRequest, send
from tripl_cli.model import page_items, page_total

__all__ = [
    "ALL_TEMPLATES",
    "SHARED_ENDPOINTS",
    "ApiRequest",
    "auth",
    "branches",
    "chart_annotations",
    "data_sources",
    "event_types",
    "events",
    "monitoring",
    "page_items",
    "page_total",
    "projects",
    "scans",
    "search",
    "send",
    "variables",
]
