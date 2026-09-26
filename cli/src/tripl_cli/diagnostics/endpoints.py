"""Every REST endpoint the commands read, grouped by the section it fills.

Single source of truth for the contract test: every (method, path) here must
exist in ``backend/openapi.json``. The path LITERALS live in ``tripl_cli.api``
now, which is also where ``tripl_mcp.contract`` takes them from — so a rename
breaks both packages loudly instead of one loudly and the other silently
(tripl-ey6j.5). What survives here is the per-section GROUPING, because a
contract failure should name the check that would have gone blind, not just a
URL, and the MCP's map is keyed by tool name instead.

``HEALTH_PATH`` is NOT here: it lives outside ``/api/v1`` and is declared
``include_in_schema=False``, so it is absent from the OpenAPI document by design
and cannot be checked against it. ``tests/test_doctor.py`` pins its location and
its unauthenticated-ness directly instead.
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

_SELECTION: tuple[tuple[str, str], ...] = (
    ("get", projects.LIST),
    ("get", projects.DETAIL),
)

# What each collector reads, keyed by the snapshot section it fills - so a
# contract failure names the check that would have gone blind, not just a URL.
DOCTOR_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "auth": (("get", auth.ME),),
    "selection": _SELECTION,
    "data_sources": (("get", data_sources.LIST),),
    "scans": (("get", scans.CONFIGS),),
    "jobs": (("get", scans.JOBS),),
    "event_types": (("get", event_types.LIST),),
    "drifts": (("get", event_types.DRIFTS),),
}

STATUS_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "selection": _SELECTION,
    "coverage": (("get", monitoring.COVERAGE),),
}

# `tripl watch` polls these on a loop. Note what is NOT here: the Server-Sent
# Events stream at /projects/{slug}/events/stream. It is declared
# `include_in_schema=False` and is therefore absent from the OpenAPI document, so
# adding it would fail the contract test rather than protect it - and watch does
# not open it anyway, because the replay chunk progress that is this command's
# headline is written to ScanJob.result_summary with no publish_project_event
# call and is invisible on that bus (tripl-ey6j.4).
#
# /auth/me is deliberately absent too: raise_selection_failure already turns a
# listing 403 into the "name the project with --project <slug>" advice, so
# probing the key's reach would buy nothing status does not already get free.
WATCH_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "selection": _SELECTION,
    "scans": (("get", scans.CONFIGS),),
    "jobs": (("get", scans.JOBS),),
    "signals": (("get", monitoring.SIGNALS),),
    "deliveries": (("get", monitoring.ALERT_DELIVERIES),),
}

# `tripl scans <verb>`. The two mutations are the CLI's first writes; both are
# editor-gated server-side, and neither is pre-authorised locally (the tk_r_ /
# tk_w_ prefix is derived from the scope's first letter in the backend and says
# nothing about the backing user's role - the server is the authority).
#
# METRICS_REPLAY is absent on purpose: the route exists, but it is guarded by
# ``deps.get_owner_user``, which rejects EVERY request carrying an API key scope.
# No Bearer-token client can reach it, so shipping `tripl scans replay` would
# ship a command that always 403s (tripl-ey6j.5).
SCANS_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "selection": _SELECTION,
    "scans": (("get", scans.CONFIGS),),
    "jobs": (("get", scans.JOBS),),
    "run": (("post", scans.RUN),),
    "cancel": (("post", scans.JOB_CANCEL),),
}

# `tripl drifts <verb>`. The listing is a fan-out over event types because there
# is NO project-level drift route - probing one returns 404, which a naive
# harness reads as "no drifts" (TRAP 3). The action route is not nested under
# {event_type_id}; it hangs off the collection with the drift id alone.
DRIFTS_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "selection": _SELECTION,
    "event_types": (("get", event_types.LIST),),
    "drifts": (("get", event_types.DRIFTS),),
    # Both writes post to the SAME route with a different `action`, so this pair
    # is one path spelled twice on purpose: the key names the verb whose contract
    # check would go blind, and a `reopen` with no entry of its own would be
    # protected only by the coincidence that dismiss shares its route (tripl-k8j9).
    "dismiss": (("post", event_types.DRIFT_ACTIONS_PATH),),
    "reopen": (("post", event_types.DRIFT_ACTIONS_PATH),),
}

# `tripl events <verb>`. Both verbs are READS: the POST and PATCH on
# events.LIST/DETAIL exist in the shared layer for tripl-mcp and are absent here
# because no CLI verb reaches them (see commands/events.py for why).
#
# `?branch=` is not in either map, and cannot be: these are (method, path) pairs
# and it is a query parameter. It is watched all the same -
# deps.get_branch_id_override declares it, so it is in backend/openapi.json, and
# for GET /events test_the_events_list_builder_takes_every_filter_the_route_declares
# covers it alongside every other filter the route offers (tripl-l33u.7).
EVENTS_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "events": (("get", events.LIST),),
    "event": (("get", events.DETAIL),),
    # `events show` resolves field_definition_id -> name from this.
    "fields": (("get", event_types.FIELDS),),
    # Every verb taking --branch reads the branch listing to resolve it.
    "branches": (("get", branches.LIST),),
}

# `tripl plan <verb>`. branches.DIFF is deliberately absent: no verb reads it
# (commands/plan.py records why), and declaring an endpoint nothing reads would
# make this map a wish list rather than a description.
PLAN_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "types": (("get", event_types.LIST),),
    "fields": (("get", event_types.FIELDS),),
    "variables": (("get", variables.LIST),),
    "branches": (("get", branches.LIST),),
    "search": (("get", search.SEARCH),),
}

# `tripl annotate`: one POST and nothing else. The slug is taken on trust rather
# than resolved first - the route's own 404 names a wrong one, and a CI step
# posting a deploy marker should cost exactly one request.
ANNOTATE_ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "annotate": (("post", chart_annotations.CREATE),),
}
