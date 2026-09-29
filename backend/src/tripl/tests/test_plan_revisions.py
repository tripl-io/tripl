from typing import Any

import pytest
from httpx import AsyncClient

from tripl.services.plan_revision_service import (
    _public_snapshot_payload,
    compute_plan_diff_entries,
)


async def _setup_project(client: AsyncClient, slug: str = "rev-proj"):
    await client.post("/api/v1/projects", json={"name": "R", "slug": slug})
    et_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    et_id = et_resp.json()["id"]
    f_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={
            "name": "screen",
            "display_name": "Screen",
            "field_type": "string",
            "is_required": True,
        },
    )
    field_id = f_resp.json()["id"]
    ev_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et_id,
            "name": "Home View",
            "field_values": [{"field_definition_id": field_id, "value": "home"}],
        },
    )
    return et_id, field_id, ev_resp.json()["id"]


def test_public_snapshot_redacts_internal_merge_fingerprints() -> None:
    payload = {
        "events": [
            {
                "photos": [
                    {
                        "storage_key_fingerprint": "secret-hash",
                        "comments": [
                            {
                                "user_fingerprint": "user-hash",
                                "body_fingerprint": "body-hash",
                            }
                        ],
                    }
                ]
            }
        ]
    }

    public = _public_snapshot_payload(payload)

    photo = public["events"][0]["photos"][0]
    assert photo["storage_key_fingerprint"] == "<redacted>"
    assert photo["comments"][0] == {
        "user_fingerprint": "<redacted>",
        "body_fingerprint": "<redacted>",
    }
    assert payload["events"][0]["photos"][0]["storage_key_fingerprint"] == "secret-hash"


# --- snapshot version skew (tripl-avrs) -------------------------------------
# Branches frozen before the PLAN_SNAPSHOT_VERSION 2 bump hold v1 base payloads
# whose events lack source_name / owner_id / reviewed / metric_breakdown_columns /
# field_values / meta_values / tags / photos and whose variables lack
# excluded_from_scans and store event_value_overrides as a dict instead of a
# list. The diff must not report those missing keys as changes.


def _v1_event(name: str, *, description: str | None = None) -> dict[str, Any]:
    return {
        "id": f"v1-{name}",
        "event_type_id": "et-1",
        "event_type_name": "pv",
        "name": name,
        "description": description,
        "order": 0,
        "status": "draft",
        "sunset_at": None,
    }


def _v2_event(name: str, *, description: str | None = None) -> dict[str, Any]:
    return {
        "id": f"v2-{name}",
        "event_type_id": "et-1",
        "event_type_name": "pv",
        "name": name,
        "source_name": None,
        "description": description,
        "order": 0,
        "status": "draft",
        "sunset_at": None,
        "owner_id": None,
        "reviewed": False,
        "metric_breakdown_columns": [],
        "field_values": [],
        "meta_values": [],
        "tags": [],
        "photos": [],
    }


def _v1_variable(name: str) -> dict[str, Any]:
    return {
        "id": f"v1-var-{name}",
        "name": name,
        "source_name": None,
        "variable_type": "string",
        "description": None,
        "allowed_values": [],
        "bindings": [],
        "event_value_overrides": {},
    }


def _v2_variable(name: str) -> dict[str, Any]:
    return {
        "id": f"v2-var-{name}",
        "name": name,
        "source_name": None,
        "variable_type": "string",
        "description": None,
        "allowed_values": [],
        "bindings": [],
        "excluded_from_scans": False,
        "event_value_overrides": [],
    }


def _payload(
    version: int, events: list[dict[str, Any]], variables: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "snapshot_version": version,
        "event_types": [],
        "events": events,
        "variables": variables,
        "meta_fields": [],
        "relations": [],
    }


def test_diff_tolerates_v1_base_payload_missing_v2_keys() -> None:
    """A v1 base compared against a v2 snapshot must not flood "changed"."""
    v1_base = _payload(
        1,
        events=[_v1_event("Home View"), _v1_event("Checkout", description="pay")],
        variables=[_v1_variable("user_id")],
    )
    v2_snapshot = _payload(
        2,
        events=[
            _v2_event("Home View"),
            _v2_event("Checkout", description="pay"),
            _v2_event("Brand New"),
        ],
        variables=[_v2_variable("user_id")],
    )

    entries = compute_plan_diff_entries(v1_base, v2_snapshot)

    assert [(e.kind, e.entity_type, e.name) for e in entries] == [("added", "event", "Brand New")]


def test_diff_from_v1_base_still_reports_genuine_changes() -> None:
    v1_base = _payload(1, events=[_v1_event("Home View", description="old")], variables=[])
    v2_snapshot = _payload(2, events=[_v2_event("Home View", description="new")], variables=[])

    entries = compute_plan_diff_entries(v1_base, v2_snapshot)

    assert len(entries) == 1
    entry = entries[0]
    assert (entry.kind, entry.entity_type, entry.name) == ("changed", "event", "Home View")
    assert [fc.field for fc in entry.field_changes] == ["description"]
    assert any("description" in change for change in entry.changes)


# --- version-anchored skip-absent-key tolerance (tripl-2d3d) ----------------
# The skip-absent-key tolerance must be anchored to the OLD payload's
# snapshot_version. A current-version base is expected to carry every change
# key, so a missing key is a genuine divergence and must surface; only a
# pre-bump (v1) base may legitimately omit v2 keys and stay tolerated.


def test_current_version_base_missing_change_key_surfaces_diff() -> None:
    """A current-version base missing a change key is a real diff, not skew.

    Simulates a future conditionally-omitting serializer path: the v2 base omits
    the ``tags`` change key while the new snapshot carries a real value. Because
    the base IS the current version, the missing key must NOT be silently
    dropped — the diff has to surface (tripl-2d3d).
    """
    old_event = _v2_event("Home View")
    del old_event["tags"]  # current-version snapshot missing a change key
    new_event = _v2_event("Home View")
    new_event["tags"] = ["urgent"]

    v2_base = _payload(2, events=[old_event], variables=[])
    v2_snapshot = _payload(2, events=[new_event], variables=[])

    entries = compute_plan_diff_entries(v2_base, v2_snapshot)

    assert len(entries) == 1
    entry = entries[0]
    assert (entry.kind, entry.entity_type, entry.name) == ("changed", "event", "Home View")
    assert "tags" in [fc.field for fc in entry.field_changes]


def test_old_version_base_missing_change_key_still_tolerated() -> None:
    """Backward tolerance preserved: the SAME structural omission under a v1
    base is still skipped, so no phantom "changed" entry appears (tripl-2d3d).

    Only ``snapshot_version`` differs from the test above — proving the outcome
    is governed by the OLD payload's version, not by the missing key alone.
    """
    old_event = _v2_event("Home View")
    del old_event["tags"]
    new_event = _v2_event("Home View")
    new_event["tags"] = ["urgent"]

    v1_base = _payload(1, events=[old_event], variables=[])
    v2_snapshot = _payload(2, events=[new_event], variables=[])

    entries = compute_plan_diff_entries(v1_base, v2_snapshot)

    assert entries == []


# --- per-item diff of collection-valued fields (tripl-mzsb.1) ----------------
# An event's field values, its tags, a variable's documented values and its
# per-event overrides are collections. Comparing them as opaque blobs makes the
# reviewer eyeball two JSON dumps to find the one item that moved, so the diff
# breaks them down by the item's natural key.


def _field_value(name: str, value: str, *, is_authored: bool = True) -> dict[str, Any]:
    return {"field_name": name, "value": value, "is_authored": is_authored}


def test_field_value_change_breaks_down_per_field() -> None:
    old = _v2_event("Checkout")
    old["field_values"] = [_field_value("currency", "USD"), _field_value("coupon", "SUMMER")]
    new = _v2_event("Checkout")
    new["field_values"] = [_field_value("currency", "EUR"), _field_value("method", "card")]

    entries = compute_plan_diff_entries(_payload(2, [old], []), _payload(2, [new], []))

    change = next(fc for fc in entries[0].field_changes if fc.field == "field_values")
    assert [(item.key, item.kind) for item in change.items] == [
        ("coupon", "removed"),
        ("currency", "changed"),
        ("method", "added"),
    ]
    currency = next(item for item in change.items if item.key == "currency")
    assert currency.before == {"value": "USD", "is_authored": True}
    assert currency.after == {"value": "EUR", "is_authored": True}
    # The summary string stays field-prefixed (the collapsed row renders it) but
    # counts the items instead of dumping both lists.
    assert entries[0].changes == ["field_values: 1 added, 1 changed, 1 removed"]


def test_scalar_list_change_keys_items_by_value() -> None:
    old = _v2_event("Checkout")
    old["tags"] = ["urgent"]
    new = _v2_event("Checkout")
    new["tags"] = ["urgent", "revenue"]

    entries = compute_plan_diff_entries(_payload(2, [old], []), _payload(2, [new], []))

    change = next(fc for fc in entries[0].field_changes if fc.field == "tags")
    assert [(item.key, item.kind, item.before, item.after) for item in change.items] == [
        ("revenue", "added", None, "revenue")
    ]


def test_variable_event_overrides_key_on_the_event_they_target() -> None:
    old = _v2_variable("currency")
    old["event_value_overrides"] = [
        {"event_type_name": "track", "event_name": "purchase", "values": ["USD"]}
    ]
    new = _v2_variable("currency")
    new["event_value_overrides"] = [
        {"event_type_name": "track", "event_name": "purchase", "values": ["USD", "EUR"]}
    ]

    entries = compute_plan_diff_entries(_payload(2, [], [old]), _payload(2, [], [new]))

    change = next(fc for fc in entries[0].field_changes if fc.field == "event_value_overrides")
    assert [(item.key, item.kind) for item in change.items] == [("track.purchase", "changed")]
    # Two attributes since F23 (the values and ``required``), so the member is
    # shown whole rather than unwrapped; a pre-F23 payload is read as optional.
    assert change.items[0].before == {"values": ["USD"], "required": False}
    assert change.items[0].after == {"values": ["USD", "EUR"], "required": False}


def test_scalar_field_change_carries_no_items() -> None:
    entries = compute_plan_diff_entries(
        _payload(2, [_v2_event("Home View", description="old")], []),
        _payload(2, [_v2_event("Home View", description="new")], []),
    )

    assert entries[0].field_changes[0].items == []


def test_duplicate_member_keys_fall_back_to_whole_value_diff() -> None:
    """Two photos can share a filename — nothing in the schema stops them.

    Keying members by a value that isn't unique would let one member mask
    another's removal, so a collision drops the per-member breakdown and leaves
    the raw before/after in place: the reviewer sees the whole collection rather
    than a confident lie about part of it.
    """
    old = _v2_event("Checkout")
    old["photos"] = [
        {"original_filename": "shot.png", "size_bytes": 10},
        {"original_filename": "shot.png", "size_bytes": 20},
    ]
    new = _v2_event("Checkout")
    new["photos"] = [{"original_filename": "shot.png", "size_bytes": 20}]

    entries = compute_plan_diff_entries(_payload(2, [old], []), _payload(2, [new], []))

    change = next(fc for fc in entries[0].field_changes if fc.field == "photos")
    assert change.items == []
    assert len(change.before) == 2
    assert len(change.after) == 1


def test_v1_dict_shaped_overrides_fall_back_to_whole_value_diff() -> None:
    """A v1 base stored event_value_overrides as a dict, not a list.

    Cross-shape item keying is meaningless, so the entry keeps the raw
    before/after and simply reports no per-item breakdown.
    """
    old = _v1_variable("currency")
    old["event_value_overrides"] = {"purchase": ["USD"]}
    new = _v2_variable("currency")
    new["event_value_overrides"] = [
        {"event_type_name": "track", "event_name": "purchase", "values": ["USD"]}
    ]

    entries = compute_plan_diff_entries(_payload(1, [], [old]), _payload(2, [], [new]))

    change = next(fc for fc in entries[0].field_changes if fc.field == "event_value_overrides")
    assert change.items == []
    assert change.before == {"purchase": ["USD"]}


# --- entity_id: the diff row has to link to the entity it describes ----------


def test_entries_carry_the_entity_id_they_describe() -> None:
    old_event = _v2_event("Home View", description="old")
    new_event = _v2_event("Home View", description="new")
    removed = _v2_event("Gone")
    added = _v2_event("Brand New")

    entries = compute_plan_diff_entries(
        _payload(2, [old_event, removed], []),
        _payload(2, [new_event, added], []),
    )
    by_name = {entry.name: entry for entry in entries}

    # Changed and added entities are linkable on the branch side …
    assert by_name["Home View"].entity_id == new_event["id"]
    assert by_name["Brand New"].entity_id == added["id"]
    # … a removed one only exists on the base side, so link there.
    assert by_name["Gone"].entity_id == removed["id"]


@pytest.mark.asyncio
async def test_create_revision_captures_full_plan_snapshot(client: AsyncClient) -> None:
    et_id, _field_id, _event_id = await _setup_project(client, "rev-snap")
    resp = await client.post(
        "/api/v1/projects/rev-snap/revisions",
        json={"summary": "initial baseline"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["summary"] == "initial baseline"
    assert body["entity_counts"]["event_types"] == 1
    assert body["entity_counts"]["fields"] == 1
    assert body["entity_counts"]["events"] == 1

    payload = body["payload"]
    assert payload["event_types"][0]["id"] == et_id
    assert payload["event_types"][0]["field_definitions"][0]["name"] == "screen"
    assert payload["events"][0]["name"] == "Home View"


@pytest.mark.asyncio
async def test_list_revisions_orders_by_created_at_desc(client: AsyncClient) -> None:
    await _setup_project(client, "rev-list")
    for summary in ("first", "second", "third"):
        r = await client.post("/api/v1/projects/rev-list/revisions", json={"summary": summary})
        assert r.status_code == 201

    resp = await client.get("/api/v1/projects/rev-list/revisions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    summaries = [item["summary"] for item in body["items"]]
    assert summaries == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_diff_reports_added_removed_and_changed_entities(
    client: AsyncClient,
) -> None:
    et_id, field_id, event_id = await _setup_project(client, "rev-diff")

    baseline = await client.post(
        "/api/v1/projects/rev-diff/revisions", json={"summary": "baseline"}
    )
    baseline_id = baseline.json()["id"]

    # Mutate the plan: add a new field, mark the event implemented,
    # delete the original field by removing it from the field_values payload
    # (the field itself stays; the diff is across declared schema).
    new_field = await client.post(
        f"/api/v1/projects/rev-diff/event-types/{et_id}/fields",
        json={
            "name": "platform",
            "display_name": "Platform",
            "field_type": "enum",
            "is_required": False,
            "enum_options": ["web", "ios", "android"],
        },
    )
    assert new_field.status_code == 201
    # Flip required → False AND tag the field as PII to drive a "changed"
    # entry covering both kinds of field-level edits.
    patch = await client.patch(
        f"/api/v1/projects/rev-diff/event-types/{et_id}/fields/{field_id}",
        json={"is_required": False, "sensitivity": "pii"},
    )
    assert patch.status_code == 200
    # Move the event through the lifecycle to drive an event-level change.
    ev_patch = await client.patch(
        f"/api/v1/projects/rev-diff/events/{event_id}",
        json={"status": "implemented"},
    )
    assert ev_patch.status_code == 200

    after = await client.post("/api/v1/projects/rev-diff/revisions", json={"summary": "after"})
    after_id = after.json()["id"]

    diff_resp = await client.get(
        f"/api/v1/projects/rev-diff/revisions/{after_id}/diff?compare_to={baseline_id}"
    )
    assert diff_resp.status_code == 200
    diff = diff_resp.json()

    kinds_by_name = {(entry["entity_type"], entry["name"]): entry for entry in diff["entries"]}
    assert kinds_by_name[("field_definition", "platform")]["kind"] == "added"
    assert kinds_by_name[("field_definition", "screen")]["kind"] == "changed"
    assert any(
        "is_required" in change
        for change in kinds_by_name[("field_definition", "screen")]["changes"]
    )
    assert any(
        "sensitivity" in change
        for change in kinds_by_name[("field_definition", "screen")]["changes"]
    )
    assert kinds_by_name[("event", "Home View")]["kind"] == "changed"
    assert any("status" in change for change in kinds_by_name[("event", "Home View")]["changes"])
    assert diff["summary"]["added"] >= 1
    assert diff["summary"]["changed"] >= 2
