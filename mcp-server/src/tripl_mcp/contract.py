"""The REST endpoints each MCP tool wraps.

Single source of truth for the contract test: every (method, path) here must
exist in ``backend/openapi.json``. The path LITERALS live in ``tripl_cli.api``,
which is where the tool bodies build their requests from too — so this map is a
statement about which tool touches what, and cannot drift from what the tool
actually sends.

Kept separate from ``tripl_cli.diagnostics.endpoints`` on purpose: that one is
keyed by snapshot section, and this one's key set must equal the REGISTERED TOOL
NAMES, which ``test_every_registered_tool_has_a_contract_entry`` asserts. The
literal is shared; the declaration is not.
"""

from __future__ import annotations

from tripl_cli.api import branches, docs, event_types, events, monitoring, projects, scans, search
from tripl_cli.api import variables as variables_api

TOOL_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "search_plan": (("get", search.SEARCH),),
    "list_events": (("get", events.LIST),),
    "get_event": (("get", events.DETAIL),),
    "get_event_properties": (("get", events.PROPERTIES),),
    "create_event": (("post", events.LIST),),
    "update_event": (("patch", events.DETAIL),),
    "list_event_types": (("get", event_types.LIST),),
    "get_event_type_fields": (
        ("get", event_types.DETAIL),
        ("get", event_types.FIELDS),
    ),
    "list_variables": (("get", variables_api.LIST),),
    "get_variable_values": (
        ("get", variables_api.VALUES),
        ("get", variables_api.EVENT_OVERRIDES),
    ),
    "list_branches": (("get", branches.LIST),),
    "get_branch_diff": (("get", branches.DIFF),),
    "list_scans": (("get", scans.CONFIGS),),
    # list_scans became a trimmed projection, so the full
    # ScanConfigResponse needed a route of its own or the detail an agent used to
    # get from the listing would simply have been removed from the toolset.
    "get_scan": (("get", scans.CONFIG),),
    "trigger_scan": (("post", scans.RUN),),
    "get_scan_status": (
        ("get", scans.JOBS),
        ("get", scans.JOB),
    ),
    "monitors_summary": (
        ("get", monitoring.MONITORS_SUMMARY),
        ("get", monitoring.SIGNALS),
    ),
    "reconciliation_status": (
        ("get", monitoring.COVERAGE),
        ("get", monitoring.DEAD_EVENTS),
        ("get", monitoring.SHADOW_EVENTS),
    ),
    "list_projects": (("get", projects.LIST),),
    # The docs catalog (F22). Four tools over three routes: the tree, one file
    # (read and replace), and search. Move, delete and import are withheld.
    "list_docs": (("get", docs.TREE),),
    "read_doc": (("get", docs.FILE),),
    "search_docs": (("get", docs.SEARCH),),
    "write_doc": (("put", docs.FILE),),
}
