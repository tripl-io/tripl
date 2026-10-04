"""The event catalog.

No projection for ``EventListItemResponse``. Its ``field_values`` and
``meta_values`` are the whole content of an event, and a CLI writes to a pipe
rather than into a model's context window — so ``tripl events list --json``
carries a row verbatim, exactly as ``scans.list_jobs`` does. The MCP's
``EVENT_LIST_FIELDS`` is a statement about an AGENT's context budget and stays
where its one consumer is (tripl-i1dt revisits that).
"""

from __future__ import annotations

from typing import Any

from tripl_cli.api.request import ApiRequest

LIST = "/projects/{slug}/events"
DETAIL = "/projects/{slug}/events/{event_id}"
PROPERTIES = "/projects/{slug}/events/{event_id}/properties"

ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("get", LIST),
    ("get", DETAIL),
    ("post", LIST),
    ("patch", DETAIL),
    ("get", PROPERTIES),
)

# EventStatus, verbatim from the OpenAPI document. Spelled here rather than in
# the argparse `choices=` so the CLI's accepted values and the values the route
# accepts are one list, pinned against backend/openapi.json by
# cli/tests/test_contract.py rather than by whoever last read the enum.
STATUSES: tuple[str, ...] = (
    "draft",
    "in_review",
    "ready_for_dev",
    "implemented",
    "live",
    "deprecated",
    "archived",
)

# The route's `order_by` Literal, verbatim. "catalog" is the authored order the
# plan is read in; "volume" ranks busiest-first by each event's summed
# EventMetric count over the last 24h; "health" ranks least healthy first by
# the F15 health score (main plan only - a branch gets a 400). Spelled here
# rather than in the argparse `choices=` for the same reason STATUSES is, and
# pinned to backend/openapi.json by cli/tests/test_contract.py.
ORDER_BY: tuple[str, ...] = ("catalog", "volume", "health")

# What omitting `order_by` gets you. Held only so the help text can name it:
# unlike `limit`, this parameter is left OFF the wire when unasked-for, so a
# route that changed its mind about the default would be followed rather than
# overridden. Pinned to the document alongside the bounds below.
ORDER_BY_DEFAULT = "catalog"

# The route's own bounds: `limit` is Query(ge=1, le=10000) with a server default
# of 200, `silent_since_days` is Query(ge=0, le=3650). Reproduced here so a bad
# value costs no request, and pinned to the OpenAPI document by the contract
# test so the two can never state different ceilings (the 40-vs-200 trap).
LIMIT_DEFAULT = 200
LIMIT_MAX = 10_000
SILENT_SINCE_DAYS_MAX = 3650


def list_events(
    slug: str,
    *,
    search: str | None = None,
    status: list[str] | None = None,
    tag: str | None = None,
    # Substring and case-insensitive, over every field VALUE on the event
    # (`EventFieldValue.value ILIKE %...%`) - the same shape `meta_value` has,
    # on the other half of an event's content. It shipped with the route in
    # PR #78 and was never mirrored here, so no shell and no agent could ask
    # "which events carry this value".
    field_value: str | None = None,
    meta_value: str | None = None,
    event_type_id: str | None = None,
    silent_since_days: int | None = None,
    # Tri-state, exactly as the route declares it: True and False isolate the
    # two halves and OMITTING it means either. Not a spelling of `status` - an
    # event can be marked reviewed and still sit at status=in_review, which is
    # why the route grew an axis of its own.
    reviewed: bool | None = None,
    # Tri-state like `reviewed`, and for the same reason: True keeps only events
    # whose discussion still has an unanswered thread, False only those with
    # none, and OMITTING it means either. A snooze that has lapsed counts as
    # unanswered again — the server decides that when it reads, so there is
    # nothing for a caller to compute.
    has_open_questions: bool | None = None,
    # Events whose property list carries this property (F23): its id, or its
    # name so a link survives a branch switch. The server resolves either.
    property: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    # One of ORDER_BY, or None to take the route's own default (ORDER_BY_DEFAULT
    # says which). Not a bool and not free text: the route declares a Literal, so
    # any third value is a 422 the caller pays a request to learn.
    order_by: str | None = None,
    branch: str | None = None,
) -> ApiRequest:
    """Paged: answers ``{items, total}``."""
    return ApiRequest(
        "GET",
        LIST.format(slug=slug),
        params={
            "search": search,
            "status": status,
            "tag": tag,
            "field_value": field_value,
            "meta_value": meta_value,
            "event_type_id": event_type_id,
            "silent_since_days": silent_since_days,
            "reviewed": reviewed,
            "has_open_questions": has_open_questions,
            "property": property,
            "offset": offset,
            "limit": limit,
            "order_by": order_by,
            "branch": branch,
        },
    )


def get_event(slug: str, event_id: str, *, branch: str | None = None) -> ApiRequest:
    return ApiRequest("GET", DETAIL.format(slug=slug, event_id=event_id), params={"branch": branch})


def get_event_properties(slug: str, event_id: str, *, branch: str | None = None) -> ApiRequest:
    """The event's typed property list (F23): ``[{name, variable_type, required, …}]``."""
    return ApiRequest(
        "GET", PROPERTIES.format(slug=slug, event_id=event_id), params={"branch": branch}
    )


def create_event(slug: str, body: Any, *, branch: str | None = None) -> ApiRequest:
    return ApiRequest("POST", LIST.format(slug=slug), params={"branch": branch}, json_body=body)


def update_event(slug: str, event_id: str, patch: Any, *, branch: str | None = None) -> ApiRequest:
    return ApiRequest(
        "PATCH",
        DETAIL.format(slug=slug, event_id=event_id),
        params={"branch": branch},
        json_body=patch,
    )
