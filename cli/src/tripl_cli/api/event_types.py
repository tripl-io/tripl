"""Event types, their fields, and the schema drifts detected against them.

Also the home of the two compound facts about drift that no single endpoint
answers: there is no project-level drift route, so "this project's drifts" is a
fan-out over event types under a budget, and "has anybody looked at this drift"
is a rule over ``status`` and ``snoozed_until`` rather than a field.

No projection for ``SchemaDriftResponse``. All fourteen fields are triage
material — ``resolved_by`` and ``resolution_note`` are how the 2026-07-30
field deletion was traced — so a drift is passed through verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import zip_longest
from typing import Any

from tripl_cli.api.request import ApiRequest
from tripl_cli.model import JsonDict, JsonList, page_items, parse_time, text_of, to_rfc3339

LIST = "/projects/{slug}/event-types"
DETAIL = "/projects/{slug}/event-types/{event_type_id}"
FIELDS = "/projects/{slug}/event-types/{event_type_id}/fields"
DRIFTS = "/projects/{slug}/event-types/{event_type_id}/drifts"
# NOT nested under {event_type_id}: the action route hangs off the collection,
# with the drift id alone. Guessing it from the list route above produces a 404.
DRIFT_ACTIONS_PATH = "/projects/{slug}/event-types/drifts/{drift_id}/actions"

ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("get", LIST),
    ("get", DETAIL),
    ("get", FIELDS),
    ("get", DRIFTS),
    ("post", DRIFT_ACTIONS_PATH),
)

# The API's own enum (SchemaDriftActionRequest.action), verbatim.
DRIFT_ACTIONS: tuple[str, ...] = ("accept", "snooze", "false_positive", "reopen")

# What `tripl drifts dismiss` may send, and which one you get is decided by the
# presence of `--snooze-until`. There is no `--action` flag.
DISMISS_ACTIONS: tuple[str, ...] = ("false_positive", "snooze")

# `tripl drifts reopen`, a verb of its own rather than a flag on `dismiss`:
# dismissing and reopening move a drift in opposite directions, and
# `dismiss --reopen` would name the wrong one.
REOPEN_ACTION = "reopen"

# Everything the CLI can put on the wire. `accept` is the one exclusion, and it
# is deliberate and permanent: on a `missing_field` drift it reaches
# `schema_drift_service._apply_acceptance_to_plan`, which DELETES the
# FieldDefinition — the exact 2026-07-30 damage doctor's
# `schema_field_deleted_by_accept` finding exists to report. The tool that
# reports that damage must not be the easiest way to cause it, so accepting
# stays in the tripl UI.
CLI_ALLOWED_DRIFT_ACTIONS: tuple[str, ...] = (*DISMISS_ACTIONS, REOPEN_ACTION)

# SchemaDriftStatus, verbatim from the OpenAPI document.
DRIFT_STATUSES: tuple[str, ...] = ("open", "accepted", "snoozed", "false_positive")


def list_event_types(slug: str, *, branch: str | None = None) -> ApiRequest:
    return ApiRequest("GET", LIST.format(slug=slug), params={"branch": branch})


def get_event_type(slug: str, event_type_id: str, *, branch: str | None = None) -> ApiRequest:
    return ApiRequest(
        "GET",
        DETAIL.format(slug=slug, event_type_id=event_type_id),
        params={"branch": branch},
    )


def get_fields(slug: str, event_type_id: str, *, branch: str | None = None) -> ApiRequest:
    return ApiRequest(
        "GET",
        FIELDS.format(slug=slug, event_type_id=event_type_id),
        params={"branch": branch},
    )


def list_drifts(slug: str, event_type_id: str) -> ApiRequest:
    """One event type's drifts. Answers ``{items, total}``, not a bare list."""
    return ApiRequest("GET", DRIFTS.format(slug=slug, event_type_id=event_type_id))


def apply_drift_action(
    slug: str,
    drift_id: str,
    *,
    action: str,
    note: str | None = None,
    snoozed_until: datetime | None = None,
) -> ApiRequest:
    """Triage one drift. Editor-gated; a ``tk_w_`` key backed by an editor.

    Unset members are omitted rather than sent as null, so the server's own
    defaults apply and the request document a ``--dry-run`` prints is the
    smallest true statement of what would be sent.

    ``note`` DOES NOT SURVIVE ``reopen``. ``schema_drift_service`` sets
    ``resolution_note = None`` for that action before it looks at the request's
    note at all — a reopened drift has no resolution any more, so it has no note
    either. Sending one is not an error and not a no-op either: the stored note
    is cleared regardless, and the one just sent is discarded. Hence no
    ``--note`` on ``tripl drifts reopen``; a flag whose value the server throws
    away is worse than an absent one.
    """
    body: JsonDict = {"action": action}
    if note is not None:
        body["note"] = note
    if snoozed_until is not None:
        body["snoozed_until"] = to_rfc3339(snoozed_until)
    return ApiRequest(
        "POST",
        DRIFT_ACTIONS_PATH.format(slug=slug, drift_id=drift_id),
        json_body=body,
    )


def drift_items(payload: Any) -> JsonList:
    """``SchemaDriftListResponse.items``. Anything else reads as empty.

    A caller must have established that the READ succeeded before calling this —
    a 404 body is not an empty list (TRAP 3), and this function cannot tell the
    difference. Kept as a name of its own because that warning is about drifts
    specifically; the unwrapping itself is ``model.page_items``, which every
    other ``{items, total}`` route in this package now reads through.
    """
    return page_items(payload)


def field_count(event_type: JsonDict) -> int:
    """How many field definitions this event type carries.

    Derived rather than served: ``EventTypeResponse`` embeds the whole
    ``field_definitions`` array, and both surfaces want the count instead —
    ``tripl plan types`` for its table, ``tripl-mcp``'s ``list_event_types`` to
    replace the array it strips. It lives here, beside ``scans.is_dispatchable``
    and for the same reason: a derived operational fact must not be computed
    twice. Both call sites read it now — the MCP's ``_event_type_summary`` spelled
    it inline until recently, which meant two places had to remember that
    ``field_definitions`` is nullable on the wire.
    """
    return len(event_type.get("field_definitions") or [])


def enum_options(field: JsonDict) -> tuple[str, ...]:
    """A field definition's allowed values, as text. Empty when it is not an enum.

    ``enum_options`` is typed ``array[Any] | null`` on the wire — the values are
    whatever the field's own type is — so a renderer that assumed strings would
    raise on a numeric enum.
    """
    options = field.get("enum_options")
    return tuple(str(option) for option in options) if isinstance(options, list) else ()


def is_untriaged(drift: JsonDict, now: datetime) -> bool:
    """Open, or snoozed past its snooze — both mean nobody has looked yet."""
    status = text_of(drift, "status")
    if status == "open":
        return True
    if status != "snoozed":
        return False
    until = parse_time(drift.get("snoozed_until"))
    return until is None or until <= now


def plan_drift_targets(
    event_types_by_slug: Mapping[str, Sequence[JsonDict]], *, budget: int
) -> tuple[tuple[tuple[str, str], ...], dict[str, int], dict[str, int]]:
    """Spend a drift-read budget across projects. Pure, over already-fetched rows.

    Returns ``(targets, examined_by_slug, totals_by_slug)`` where a target is
    ``(slug, event_type_id)``.

    The fan-out is the only unbounded thing a drift scan does — one request per
    event type across every selected project — so what the budget could not reach
    is REPORTED, never silently omitted.

    Spent ROUND-ROBIN across projects, not in project order. Spending it in order
    lets one large project consume the whole budget, and the project that then
    receives zero requests appears nowhere in the output — the summary reads
    "N of M event types examined" as though coverage were spread evenly. If the
    starved project is the one holding an accepted missing_field drift that
    deleted a FieldDefinition, the check written to surface exactly that reports
    nothing and says nothing about not having looked.

    The per-project counts are what let the caller record what each project
    actually got. A split budget lands unevenly by construction, and one
    instance-wide ratio names no project: "we did not look there" is only useful
    when it says where.
    """
    totals: dict[str, int] = {}
    per_project: list[list[tuple[str, str]]] = []
    for slug, rows in event_types_by_slug.items():
        totals[slug] = len(rows)
        candidates: list[tuple[str, str]] = []
        for event_type in rows:
            event_type_id = text_of(event_type, "id")
            if event_type_id:
                candidates.append((slug, event_type_id))
        per_project.append(candidates)
    targets: list[tuple[str, str]] = []
    for row in zip_longest(*per_project):
        for target in row:
            if target is not None and len(targets) < budget:
                targets.append(target)
    examined: dict[str, int] = dict.fromkeys(event_types_by_slug, 0)
    for slug, _ in targets:
        examined[slug] += 1
    return tuple(targets), examined, totals
