"""Plan diff, snapshot and housekeeping fixes from the 2026-09-09 sweep (tripl-0zpq).

* tripl-0zpq.148 / .141 — a meta field's ``allow_multiple`` is diffed, so the
  approval hash, the merge and the revert no longer act on a change the diff
  never showed.
* tripl-0zpq.140 — a multi-value meta field's values serialize in one order
  whatever order the rows arrive in, and a stored base written in row order
  reads equal to a fresh snapshot of the same content.
* Namesakes (tripl-0zpq.149, cut back) — rows sharing a natural key are still
  matched one per key, as main always matched them; an entry whose key more
  than one event or relation holds says what that means for the diff, the
  merge and a revert. Pairing them properly needs an origin id per branch copy.
* tripl-0zpq.138 — the "unused scan variable retired" rule matches the shape a
  scan actually writes, and not a tombstone, a person's rename or half of one,
  nor a variable an event on the branch or on main still names.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import select

from tripl.core.analyzers._event_generator_variables import SCAN_PROVENANCE_DESCRIPTION
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.schemas.plan_revision import PlanDiffEntry
from tripl.services._plan_diff_housekeeping import RETIRED_SCAN_VARIABLE, mark_housekeeping
from tripl.services.plan_revision_service import (
    PLAN_SNAPSHOT_VERSION,
    build_plan_snapshot,
    compute_plan_diff_entries,
    plan_snapshot_hash,
    with_snapshot_defaults,
)
from tripl.tests.conftest import TestSessionLocal, engine
from tripl.tests.test_plan_branches import _create_branch, _seed_plan, _transition

# --- payload builders --------------------------------------------------------


def _payload(**collections: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "snapshot_version": PLAN_SNAPSHOT_VERSION,
        "event_types": [],
        "events": [],
        "variables": [],
        "meta_fields": [],
        "relations": [],
        **collections,
    }


def _event_row(
    name: str = "purchase",
    *,
    description: str = "",
    meta_values: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"event-{uuid.uuid4()}",
        "event_type_id": "type-track",
        "event_type_name": "track",
        "name": name,
        "title": "",
        "source_name": None,
        "description": description,
        "order": 0,
        "status": "draft",
        "sunset_at": None,
        "superseded_by": None,
        "owner_id": None,
        "reviewed": False,
        "metric_breakdown_columns": [],
        "field_values": [],
        "meta_values": list(meta_values or []),
        "tags": [],
        "photos": [],
    }


def _meta_field_row(name: str = "jira") -> dict[str, Any]:
    return {
        "id": f"meta-field-{uuid.uuid4()}",
        "name": name,
        "display_name": name.title(),
        "field_type": "string",
        "is_required": False,
        "allow_multiple": False,
        "enum_options": None,
        "default_value": "",
        "link_template": None,
        "order": 0,
        "sensitivity": None,
    }


def _relation_row(*, description: str = "") -> dict[str, Any]:
    return {
        "id": f"relation-{uuid.uuid4()}",
        "source_event_type_id": "type-track",
        "source_event_type_name": "track",
        "target_event_type_id": "type-screen",
        "target_event_type_name": "screen",
        "source_field_name": "screen_id",
        "target_field_name": "id",
        "relation_type": "belongs_to",
        "description": description,
    }


def _meta(name: str, value: str) -> dict[str, Any]:
    return {"meta_field_name": name, "value": value}


def _copy(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The same row as another side holds it: its own id, any edits applied."""
    return {**row, "id": f"copy-{uuid.uuid4()}", **changes}


# --- tripl-0zpq.148 / .141: allow_multiple is a diffed key --------------------


@pytest.mark.parametrize(
    ("allow_multiple", "expected"),
    [
        pytest.param(False, [], id="unchanged-against-a-base-predating-the-key"),
        pytest.param(True, [("allow_multiple", False, True)], id="flipped-on"),
    ],
)
def test_allow_multiple_is_diffed_and_a_base_predating_it_reads_single_valued(
    allow_multiple: bool, expected: list[tuple[str, Any, Any]]
) -> None:
    """The flag is a change key, and ``_V2_META_FIELD_DEFAULTS`` keeps an older
    base (no key at all) from reading as a change when nothing moved."""
    base_field = {k: v for k, v in _meta_field_row().items() if k != "allow_multiple"}
    entries = compute_plan_diff_entries(
        _payload(meta_fields=[base_field]),
        _payload(meta_fields=[_copy(base_field, allow_multiple=allow_multiple)]),
    )
    assert [(fc.field, fc.before, fc.after) for e in entries for fc in e.field_changes] == expected


async def _meta_field_on_branch(client: AsyncClient, slug: str, branch_id: str, name: str) -> dict:
    listed = await client.get(f"/api/v1/projects/{slug}/meta-fields?branch={branch_id}")
    assert listed.status_code == 200, listed.text
    return next(mf for mf in listed.json() if mf["name"] == name)


async def _branch_with_multi_valued_jira(client: AsyncClient, slug: str) -> str:
    """A branch on which the single-valued ``jira`` meta field was made multi-valued."""
    await _seed_plan(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "jira", "display_name": "Jira", "field_type": "string"},
    )
    assert created.status_code == 201, created.text
    branch_id = await _create_branch(client, slug)
    branch_field = await _meta_field_on_branch(client, slug, branch_id, "jira")
    flipped = await client.patch(
        f"/api/v1/projects/{slug}/meta-fields/{branch_field['id']}?branch={branch_id}",
        json={"allow_multiple": True},
    )
    assert flipped.status_code == 200, flipped.text
    return branch_id


@pytest.mark.asyncio
async def test_making_a_meta_field_multi_valued_on_a_branch_is_in_the_diff(
    client: AsyncClient,
) -> None:
    """Before the fix the diff was empty and ``ahead`` was 0 while the approval
    hash had moved and the merge would write the flag onto main."""
    slug = "diff-allow-multiple"
    branch_id = await _branch_with_multi_valued_jira(client, slug)

    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert [(e["entity_type"], e["kind"], e["name"]) for e in diff["entries"]] == [
        ("meta_field", "changed", "jira")
    ]
    assert diff["entries"][0]["field_changes"] == [
        {"field": "allow_multiple", "before": False, "after": True, "items": []}
    ]
    assert diff["summary"] == {"added": 0, "removed": 0, "changed": 1, "housekeeping": 0}

    listed = (await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")).json()
    assert next(b for b in listed["items"] if b["id"] == branch_id)["ahead"] == 1


@pytest.mark.asyncio
async def test_making_a_meta_field_multi_valued_can_be_reverted_from_the_diff(
    client: AsyncClient,
) -> None:
    """The revert looks the change up in the diff; it answered 404 "not in this
    branch's diff" for a flag the merge would nevertheless have applied."""
    slug = "revert-allow-multiple"
    branch_id = await _branch_with_multi_valued_jira(client, slug)

    reverted = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "meta_field", "name": "jira", "field": "allow_multiple"},
    )
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["entries"] == []
    assert (await _meta_field_on_branch(client, slug, branch_id, "jira"))["allow_multiple"] is False


# --- tripl-0zpq.140: one order for a multi-value field's values ---------------


def test_a_stored_base_in_row_order_diffs_equal_to_a_fresh_snapshot() -> None:
    """A revision written before the fix holds a field's values in the order the
    database returned them; compared with a fresh snapshot of the same content
    that order alone read as a meta_values change."""
    stored = _event_row(
        meta_values=[
            _meta("area", "checkout"),
            _meta("jira_keys", "PROJ-2"),
            _meta("jira_keys", "PROJ-1"),
        ]
    )
    fresh = _copy(
        stored,
        meta_values=[
            _meta("area", "checkout"),
            _meta("jira_keys", "PROJ-1"),
            _meta("jira_keys", "PROJ-2"),
        ],
    )
    assert compute_plan_diff_entries(_payload(events=[stored]), _payload(events=[fresh])) == []

    # A value that really changed still does.
    edited = _copy(
        stored,
        meta_values=[
            _meta("area", "checkout"),
            _meta("jira_keys", "PROJ-1"),
            _meta("jira_keys", "PROJ-3"),
        ],
    )
    (entry,) = compute_plan_diff_entries(_payload(events=[stored]), _payload(events=[edited]))
    assert [fc.field for fc in entry.field_changes] == ["meta_values"]


def test_with_snapshot_defaults_orders_meta_values_without_touching_its_input() -> None:
    stored = _payload(
        events=[
            _event_row(meta_values=[_meta("jira_keys", "PROJ-2"), _meta("jira_keys", "PROJ-1")])
        ]
    )
    upgraded = with_snapshot_defaults(stored)

    assert [m["value"] for m in upgraded["events"][0]["meta_values"]] == ["PROJ-1", "PROJ-2"]
    # The stored payload is left exactly as it was ...
    assert [m["value"] for m in stored["events"][0]["meta_values"]] == ["PROJ-2", "PROJ-1"]
    # ... and a payload already in order comes back as the very same object.
    assert with_snapshot_defaults(upgraded) is upgraded


def _reverse_meta_value_rows(rewritten: list[str]) -> Callable[..., tuple[str, Any]]:
    """A ``before_cursor_execute`` hook that serves meta values in DESCENDING value order.

    SQLite answers the selectin load through the unique index on (event_id,
    meta_field_definition_id, value), so a field's values already arrive in value
    order here and the bug cannot show. Postgres promises no order: a heap scan
    returns rows as they sit, and ``update_event`` re-inserts them in payload
    order. This stands in for that.
    """

    def hook(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> tuple[str, Any]:
        flat = " ".join(statement.split())
        if (
            flat.startswith("SELECT ")
            and "FROM event_meta_values WHERE event_meta_values.event_id IN (" in flat
            and "ORDER BY" not in flat
            and "LIMIT" not in flat
        ):
            rewritten.append(flat)
            return f"{statement} ORDER BY event_meta_values.value DESC", parameters
        return statement, parameters

    return hook


@contextmanager
def _meta_value_rows_reversed() -> Iterator[list[str]]:
    rewritten: list[str] = []
    hook = _reverse_meta_value_rows(rewritten)
    sa_event.listen(engine.sync_engine, "before_cursor_execute", hook, retval=True)
    try:
        yield rewritten
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", hook)


async def _multi_meta_field(client: AsyncClient, slug: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={
            "name": "jira_keys",
            "display_name": "Jira keys",
            "field_type": "string",
            "allow_multiple": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _event_id(client: AsyncClient, slug: str, name: str, branch_id: str | None = None) -> str:
    query = f"?branch={branch_id}" if branch_id else ""
    listed = await client.get(f"/api/v1/projects/{slug}/events{query}")
    assert listed.status_code == 200, listed.text
    return next(e["id"] for e in listed.json()["items"] if e["name"] == name)


def _meta_values_of(payload: dict[str, Any], event_name: str) -> list[tuple[str, str]]:
    event = next(e for e in payload["events"] if e["name"] == event_name)
    return [(m["meta_field_name"], m["value"]) for m in event["meta_values"]]


@pytest.mark.asyncio
async def test_snapshot_orders_a_fields_values_whatever_order_the_rows_arrive_in(
    client: AsyncClient,
) -> None:
    slug = "snapshot-meta-order"
    await _seed_plan(client, slug)
    multi_id = await _multi_meta_field(client, slug)
    event_id = await _event_id(client, slug, "purchase:success")
    saved = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={
            "meta_values": [
                {"meta_field_definition_id": multi_id, "value": value}
                for value in ("PROJ-2", "PROJ-1", "PROJ-3")
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    async with TestSessionLocal() as session:
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == slug))
        ).scalar_one()

    async with TestSessionLocal() as session:
        as_indexed = await build_plan_snapshot(session, project_id)
    async with TestSessionLocal() as session:
        with _meta_value_rows_reversed() as rewritten:
            as_heap_ordered = await build_plan_snapshot(session, project_id)
    assert rewritten, "the hook must actually have reordered the load"

    in_value_order = [("jira_keys", "PROJ-1"), ("jira_keys", "PROJ-2"), ("jira_keys", "PROJ-3")]
    assert _meta_values_of(as_heap_ordered, "purchase:success") == in_value_order
    assert _meta_values_of(as_indexed, "purchase:success") == in_value_order
    assert plan_snapshot_hash(as_heap_ordered) == plan_snapshot_hash(as_indexed)


@pytest.mark.asyncio
async def test_an_approval_stays_fresh_when_the_rows_come_back_in_another_order(
    client: AsyncClient,
) -> None:
    """The approval hash pins the snapshot, so a row order no one chose made a
    reviewed branch unmergeable (``stale``) with nothing edited."""
    slug = "approval-meta-order"
    await _seed_plan(client, slug)
    await _multi_meta_field(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_field = await _meta_field_on_branch(client, slug, branch_id, "jira_keys")
    event_id = await _event_id(client, slug, "purchase:success", branch_id)
    saved = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}",
        json={
            "meta_values": [
                {"meta_field_definition_id": branch_field["id"], "value": value}
                for value in ("PROJ-1", "PROJ-2")
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    assert "_status" not in await _transition(client, slug, branch_id, "submit")
    assert "_status" not in await _transition(client, slug, branch_id, "approve")

    with _meta_value_rows_reversed() as rewritten:
        detail = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}")
    assert rewritten, "the hook must actually have reordered the load"
    assert detail.status_code == 200, detail.text
    assert [a["stale"] for a in detail.json()["approvals"]] == [False]


# --- Namesakes (tripl-0zpq.149, cut back): said on the row, not solved --------

_EVENT_WARNING = (
    "More than one event is named 'purchase' in 'track'. This diff, the merge and a "
    "revert all match rows by name, so a change to one of them can show on, or land on, "
    "the other. Rename one of them before changing either."
)
_RELATION_WARNING = (
    "More than one relation links the same two fields (track.screen_id → screen.id). "
    "This diff and a revert match relations by those fields, so a change to one of them "
    "can show on, or land on, the other. Remove one of them before changing either."
)

_SHARED_KEY_KINDS = [
    pytest.param("events", lambda tag: _event_row(description=tag), _EVENT_WARNING, id="event"),
    pytest.param(
        "relations", lambda tag: _relation_row(description=tag), _RELATION_WARNING, id="relation"
    ),
]


@pytest.mark.parametrize(("collection", "row", "warning"), _SHARED_KEY_KINDS)
@pytest.mark.parametrize("held_twice_on", ["base", "branch"])
def test_an_entry_whose_key_more_than_one_row_holds_says_so(
    collection: str,
    row: Callable[[str], dict[str, Any]],
    warning: str,
    held_twice_on: str,
) -> None:
    """Rows are matched one per key, as main always matched them, so with two
    rows under a key the entry can show one row's change as the other's: here a
    deleted namesake reads as an edit to the survivor, and an added one as an
    edit to the original. The entry says that, and how to get out of it.

    It claims NOTHING about the merge refusing. A refusal was written in this
    batch and then removed (``plan_branch_merge_service._reject_ambiguous_keys``
    is gone): a branch is how an analyst CLEANS UP a pair of namesakes — delete
    both copies, author one row in their place — and refusing that merge takes
    away the only door out of the state this very sentence complains about
    (``test_event_comment_merge_batch2`` holds that workflow). So the merge is
    named here only as one more reader that matches by name, and the sentence
    ends on the repair the operator can perform. The wording and the claim move
    together, which is why the whole sentence is pinned above rather than a
    substring of it."""
    original = row("original")
    if held_twice_on == "base":
        base, branch = [original, row("deleted")], [_copy(original)]
    else:
        base, branch = [original], [_copy(original), row("added")]

    entries = compute_plan_diff_entries(
        _payload(**{collection: base}), _payload(**{collection: branch})
    )

    assert [(e.kind, e.warnings) for e in entries] == [("changed", [warning])]
    # No gate is promised: the merge matches rows by name like the diff and the
    # revert do, it does not refuse while the pair exists.
    assert "refuse" not in warning
    # Still prose a reviewer can act on, not a status code read aloud.
    assert "409" not in warning
    assert warning.rstrip().endswith("before changing either.")


@pytest.mark.parametrize(("collection", "row", "warning"), _SHARED_KEY_KINDS)
def test_a_key_only_main_holds_twice_no_longer_warns(
    collection: str, row: Callable[[str], dict[str, Any]], warning: str
) -> None:
    """Copilot on PR #164: the duplicate is on MAIN, where a branch diff never
    looks — the base holds one row and so does the branch. The merge used to
    keep one main row per key and write the branch's change onto whichever it
    kept, so this diff had to warn.

    It no longer has to: the merge pairs main's rows with the base by their own
    ids, which the base recorded, so the row the branch changed lands on the
    main row it was cut from however many namesakes main has grown since
    (tripl-0zpq.292). A warning here would send the analyst to rename a row for
    nothing."""
    original = row("original")
    base, branch = [original], [_copy(original, description="edited")]
    main = [original, row("added on main after the cut")]

    (entry,) = compute_plan_diff_entries(
        _payload(**{collection: base}),
        _payload(**{collection: branch}),
        key_collisions_from=_payload(**{collection: main}),
    )
    assert (entry.kind, entry.warnings) == ("changed", [])


@pytest.mark.parametrize(("collection", "row", "warning"), _SHARED_KEY_KINDS)
def test_an_entry_whose_key_one_row_holds_carries_no_such_warning(
    collection: str, row: Callable[[str], dict[str, Any]], warning: str
) -> None:
    original = row("original")
    (entry,) = compute_plan_diff_entries(
        _payload(**{collection: [original]}),
        _payload(**{collection: [_copy(original, description="edited")]}),
    )
    assert (entry.kind, entry.warnings) == ("changed", [])


# --- tripl-0zpq.138: the scan's own shape is housekeeping, a person's mark is not


_DELETE = object()


def _scan_variable(**changes: Any) -> dict[str, Any]:
    """A removed variable's ``before``, as a scan-minted row serializes.

    ``ensure_variable`` writes ``source_name=<key>``, ``bindings=[<key>]``, the
    provenance description and a display name from ``display_name_candidates``.
    ``_DELETE`` drops a key, for payloads serialised before it existed.
    """
    row: dict[str, Any] = {
        "name": "adana",
        "source_name": "property.adana",
        "variable_type": "string",
        "description": SCAN_PROVENANCE_DESCRIPTION,
        "allowed_values": [],
        "bindings": ["property.adana"],
        "excluded_from_scans": False,
        "event_value_overrides": [],
    }
    row.update(changes)
    return {k: v for k, v in row.items() if v is not _DELETE}


def _removed(before: dict[str, Any]) -> PlanDiffEntry:
    return PlanDiffEntry(entity_type="variable", kind="removed", name=before["name"], before=before)


def _added(after: dict[str, Any]) -> PlanDiffEntry:
    return PlanDiffEntry(entity_type="variable", kind="added", name=after["name"], after=after)


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        pytest.param(_scan_variable(), RETIRED_SCAN_VARIABLE, id="as-the-scan-wrote-it"),
        pytest.param(
            _scan_variable(name="property_adana"), RETIRED_SCAN_VARIABLE, id="longer-scan-name"
        ),
        pytest.param(
            _scan_variable(bindings=_DELETE), RETIRED_SCAN_VARIABLE, id="payload-predating-bindings"
        ),
        pytest.param(
            _scan_variable(name="property_adana", source_name=None, bindings=[]),
            RETIRED_SCAN_VARIABLE,
            id="made-through-the-api",
        ),
        pytest.param(
            _scan_variable(source_name=None, bindings=[], excluded_from_scans=True),
            None,
            id="a-tombstone",
        ),
        pytest.param(
            _scan_variable(excluded_from_scans=True), None, id="a-tombstone-of-a-scan-row"
        ),
        pytest.param(
            _scan_variable(bindings=["property.adana", "page.city"]),
            None,
            id="hand-written-binding",
        ),
        pytest.param(_scan_variable(bindings=[]), None, id="binding-cleared-by-a-person"),
        pytest.param(
            _scan_variable(bindings=_DELETE, name="city_of_adana"), None, id="renamed-by-a-person"
        ),
        pytest.param(_scan_variable(allowed_values=["adana"]), None, id="documented"),
        pytest.param(
            _scan_variable(
                event_value_overrides=[
                    {"event_type_name": "track", "event_name": "purchase", "values": ["adana"]}
                ]
            ),
            None,
            id="overridden",
        ),
        pytest.param(_scan_variable(description="Cities we ship to"), None, id="described"),
    ],
)
def test_retired_scan_variable_is_the_shape_the_scan_writes_and_nothing_a_person_touched(
    before: dict[str, Any], expected: str | None
) -> None:
    """The same reading ``core.variable_retirement._human_claim`` gives a row:
    anything other than what ``ensure_variable`` wrote is a person's mark."""
    entry = _removed(before)
    mark_housekeeping([entry])
    assert entry.housekeeping == expected


@pytest.mark.parametrize(
    "before",
    [
        pytest.param(_scan_variable(), id="as-the-scan-wrote-it"),
        pytest.param(_scan_variable(bindings=_DELETE), id="payload-predating-bindings"),
    ],
)
def test_a_removal_whose_scan_identity_an_addition_carries_is_the_authors(
    before: dict[str, Any],
) -> None:
    """Half of a rename (or a delete-and-recreate) is the author's doing,
    however untouched the old row looked."""
    removed = _removed(before)
    renamed = _added({**before, "name": "city_adana", "bindings": ["property.adana"]})
    mark_housekeeping([removed, renamed])
    assert removed.housekeeping is None

    # An addition carrying some OTHER identity says nothing about this removal.
    removed = _removed(before)
    unrelated = _added(_scan_variable(name="ankara", source_name="property.ankara"))
    mark_housekeeping([removed, unrelated])
    assert removed.housekeeping == RETIRED_SCAN_VARIABLE


def _naming_event(
    *, field_value: str | None = None, meta_value: str | None = None
) -> dict[str, Any]:
    row = _event_row()
    if field_value is not None:
        row["field_values"] = [{"field_name": "screen", "value": field_value, "is_authored": True}]
    if meta_value is not None:
        row["meta_values"] = [_meta("notes", meta_value)]
    return row


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(_naming_event(), RETIRED_SCAN_VARIABLE, id="named-by-nothing"),
        pytest.param(_naming_event(field_value="${adana}"), None, id="field-value-by-name"),
        pytest.param(
            _naming_event(field_value="to ${property.adana}"), None, id="field-value-by-source"
        ),
        pytest.param(_naming_event(meta_value="${adana}"), None, id="meta-value"),
        pytest.param(
            _naming_event(field_value="${ankara}"), RETIRED_SCAN_VARIABLE, id="another-token"
        ),
        pytest.param(
            _naming_event(field_value="adana"), RETIRED_SCAN_VARIABLE, id="the-word-not-a-token"
        ),
    ],
)
def test_a_scan_variable_an_event_still_names_is_not_housekeeping(
    event: dict[str, Any], expected: str | None
) -> None:
    """The scan writes ``${<display name>}`` into the values it saw, so a
    scan-shaped variable is normally still in use; the row alone cannot say.
    The same tokens ``plan_retirement`` keeps a variable by: its name, its
    ``source_name`` and its bindings, in a field or a meta value."""
    variable = {"id": f"variable-{uuid.uuid4()}", **_scan_variable()}
    entries = compute_plan_diff_entries(
        _payload(events=[event], variables=[variable]), _payload(events=[_copy(event)])
    )
    mark_housekeeping(entries)
    assert [(e.kind, e.name, e.housekeeping) for e in entries] == [("removed", "adana", expected)]


@pytest.mark.parametrize(
    ("on_main", "expected"),
    [
        pytest.param(_naming_event(), RETIRED_SCAN_VARIABLE, id="main-names-nothing"),
        pytest.param(_naming_event(field_value="${adana}"), None, id="main-field-value"),
        pytest.param(_naming_event(meta_value="${property.adana}"), None, id="main-meta-value"),
    ],
)
def test_a_scan_variable_main_named_after_the_cut_is_not_housekeeping(
    on_main: dict[str, Any], expected: str | None
) -> None:
    """Read the way ``diff_branch`` reads a branch: its entries base to branch,
    what main did base to main. Nothing named ``adana`` at the cut and nothing
    on the branch does, but main does now: the merge deletes the variable
    there, with its observed values, and leaves main's token unresolved. The
    branch side alone called that "unused scan variable retired"."""
    variable = {"id": f"variable-{uuid.uuid4()}", **_scan_variable()}
    at_the_cut = _naming_event()
    base = _payload(events=[at_the_cut], variables=[variable])
    main = _payload(events=[_copy(on_main)], variables=[_copy(variable)])
    branch = _payload(events=[_copy(at_the_cut)])

    entries = compute_plan_diff_entries(base, branch)
    behind = compute_plan_diff_entries(base, main)
    mark_housekeeping(entries, behind_entries=behind, main_payload=main)

    assert [(e.kind, e.name, e.housekeeping) for e in entries] == [("removed", "adana", expected)]


async def _seed_scan_variable(slug: str) -> None:
    """Mint ``adana`` on main exactly as ``ensure_variable`` does — the API cannot
    set ``source_name``, which is why the older test never saw this shape."""
    async with TestSessionLocal() as session:
        seeded = (
            await session.execute(select(Event).where(Event.name == "purchase:success"))
        ).scalar_one()
        session.add(
            Variable(
                id=uuid.uuid4(),
                project_id=seeded.project_id,
                branch_id=seeded.branch_id,
                name="adana",
                source_name="property.adana",
                variable_type="string",
                description=SCAN_PROVENANCE_DESCRIPTION,
                bindings=["property.adana"],
            )
        )
        await session.commit()


async def _branch_variable_id(client: AsyncClient, slug: str, branch_id: str, name: str) -> str:
    listed = (await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")).json()
    rows = listed["items"] if isinstance(listed, dict) else listed
    return next(row["id"] for row in rows if row["name"] == name)


@pytest.mark.asyncio
async def test_a_retired_scan_minted_variable_is_housekeeping_in_the_diff(
    client: AsyncClient,
) -> None:
    slug = "housekeeping-scan-shape"
    await _seed_plan(client, slug)
    await _seed_scan_variable(slug)
    branch_id = await _create_branch(client, slug)
    variable_id = await _branch_variable_id(client, slug, branch_id, "adana")
    gone = await client.delete(
        f"/api/v1/projects/{slug}/variables/{variable_id}?branch={branch_id}"
    )
    assert gone.status_code == 204, gone.text

    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert [(e["kind"], e["name"], e["housekeeping"]) for e in diff["entries"]] == [
        ("removed", "adana", RETIRED_SCAN_VARIABLE)
    ]
    assert diff["summary"] == {"added": 0, "removed": 0, "changed": 0, "housekeeping": 1}

    listed = (await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")).json()
    assert next(b for b in listed["items"] if b["id"] == branch_id)["ahead"] == 0


@pytest.mark.asyncio
async def test_deleting_a_scan_variable_an_event_still_names_is_counted_in_the_diff(
    client: AsyncClient,
) -> None:
    """An event on main carries ``${adana}``, as the scan writes it. Deleting the
    variable on the branch read as "unused scan variable retired": out of the
    counts and past the merge's "deletes variables from main" warning, while
    the merge deleted the live variable on main."""
    slug = "housekeeping-scan-referenced"
    await _seed_plan(client, slug)
    await _seed_scan_variable(slug)
    async with TestSessionLocal() as session:
        seeded = (
            await session.execute(select(Event).where(Event.name == "purchase:success"))
        ).scalar_one()
        field_id = (
            await session.execute(
                select(FieldDefinition.id).where(
                    FieldDefinition.event_type_id == seeded.event_type_id
                )
            )
        ).scalar_one()
        session.add(
            EventFieldValue(
                id=uuid.uuid4(), event_id=seeded.id, field_definition_id=field_id, value="${adana}"
            )
        )
        await session.commit()
    branch_id = await _create_branch(client, slug)
    variable_id = await _branch_variable_id(client, slug, branch_id, "adana")
    gone = await client.delete(
        f"/api/v1/projects/{slug}/variables/{variable_id}?branch={branch_id}"
    )
    assert gone.status_code == 204, gone.text

    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert [(e["kind"], e["name"], e["housekeeping"]) for e in diff["entries"]] == [
        ("removed", "adana", None)
    ]
    assert diff["summary"] == {"added": 0, "removed": 1, "changed": 0, "housekeeping": 0}

    listed = (await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")).json()
    assert next(b for b in listed["items"] if b["id"] == branch_id)["ahead"] == 1


@pytest.mark.asyncio
async def test_deleting_a_scan_variable_main_started_naming_after_the_cut_is_counted(
    client: AsyncClient,
) -> None:
    """Nothing names ``adana`` at the cut; afterwards an event on MAIN gets
    ``${adana}`` (an editor there, another branch's merge, or the scan, which
    runs on main). The branch deletes the variable. Its own events never named
    it, so read from the branch alone the removal was housekeeping: out of
    ``ahead`` and past the merge's "deletes variables from main" warning, while
    the merge deleted a variable main uses."""
    slug = "housekeeping-scan-named-on-main"
    await _seed_plan(client, slug)
    await _seed_scan_variable(slug)
    async with TestSessionLocal() as session:
        on_main = (
            await session.execute(select(Event).where(Event.name == "purchase:success"))
        ).scalar_one()
        main_event_id, event_type_id = on_main.id, on_main.event_type_id
        field_id = (
            await session.execute(
                select(FieldDefinition.id).where(FieldDefinition.event_type_id == event_type_id)
            )
        ).scalar_one()
    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=main_event_id,
                field_definition_id=field_id,
                value="${adana}",
            )
        )
        await session.commit()
    variable_id = await _branch_variable_id(client, slug, branch_id, "adana")
    gone = await client.delete(
        f"/api/v1/projects/{slug}/variables/{variable_id}?branch={branch_id}"
    )
    assert gone.status_code == 204, gone.text

    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert [(e["kind"], e["name"], e["housekeeping"]) for e in diff["entries"]] == [
        ("removed", "adana", None)
    ]
    assert diff["summary"] == {"added": 0, "removed": 1, "changed": 0, "housekeeping": 0}
    assert diff["behind_base"] is True

    listed = (await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")).json()
    assert next(b for b in listed["items"] if b["id"] == branch_id)["ahead"] == 1


@pytest.mark.asyncio
async def test_renaming_a_scan_minted_variable_on_a_branch_is_not_housekeeping(
    client: AsyncClient,
) -> None:
    """The diff splits a rename into a removal and an addition; the removal half
    of a scan variable's rename must stay the author's change."""
    slug = "housekeeping-scan-rename"
    await _seed_plan(client, slug)
    await _seed_scan_variable(slug)
    branch_id = await _create_branch(client, slug)
    variable_id = await _branch_variable_id(client, slug, branch_id, "adana")
    renamed = await client.patch(
        f"/api/v1/projects/{slug}/variables/{variable_id}?branch={branch_id}",
        json={"name": "city_adana"},
    )
    assert renamed.status_code == 200, renamed.text

    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    by_name = {e["name"]: e for e in diff["entries"]}
    assert (by_name["adana"]["kind"], by_name["adana"]["housekeeping"]) == ("removed", None)
    assert by_name["city_adana"]["kind"] == "added"
    assert [(r["removed_name"], r["added_name"]) for r in diff["renames"]] == [
        ("adana", "city_adana")
    ]
    assert diff["summary"] == {"added": 1, "removed": 1, "changed": 0, "housekeeping": 0}

    # The list's badge pairs the rename as the diff view does: ONE change, not
    # the raw "added + removed" two (tripl-amnn, branch-list counts).
    listed = (await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")).json()
    assert next(b for b in listed["items"] if b["id"] == branch_id)["ahead"] == 1
