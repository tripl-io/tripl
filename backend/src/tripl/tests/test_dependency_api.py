"""Dependency and impact routes (GH #257, F04).

``GET /dependencies``, ``POST /impact`` and ``GET /branches/{id}/impact``: the
response shapes, the "2 metrics and 1 alert rule" wording, branch impact read
from a real branch diff, the membership 404, and a viewer member reading them.
Deleting a used event stays allowed: dependents warn, they never block.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_dependency_service import seed_dependency_plan

PASSWORD = "Password123!"


def _item_for(body: dict[str, object], kind: str, entity_id: uuid.UUID) -> dict[str, object]:
    items = body["items"]
    assert isinstance(items, list)
    for item in items:
        if item["entity"]["kind"] == kind and item["entity"]["id"] == str(entity_id):
            assert isinstance(item, dict)
            return item
    raise AssertionError(f"no impact item for {kind}:{entity_id} in {items}")


@pytest.mark.asyncio
async def test_get_dependencies_shape_and_counts(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-get")
    resp = await client.get(
        f"/api/v1/projects/{s.slug}/dependencies", params={"entity": f"event:{s.purchase}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity"] == {
        "kind": "event",
        "id": str(s.purchase),
        "name": "purchase",
        "exists": True,
    }
    assert body["counts_by_kind"] == {"metric": 2, "alert_rule": 1}
    assert body["possible_counts_by_kind"] == {}
    edge = next(e for e in body["downstream"] if e["id"] == str(s.composition_metric))
    assert edge == {
        "kind": "metric",
        "id": str(s.composition_metric),
        "name": "Purchases",
        "relation": "metric uses event in its composition",
        "certainty": "direct",
        "url_hint": f"/o/default/p/{s.slug}/monitoring/metric/{s.composition_metric}",
        "depth": 1,
    }
    assert any(e["kind"] == "event_type" for e in body["upstream"])


@pytest.mark.asyncio
async def test_get_dependencies_depth_two_and_possible_edges(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-depth")
    deep = await client.get(
        f"/api/v1/projects/{s.slug}/dependencies",
        params={"entity": f"event_type:{s.track}", "depth": 2},
    )
    assert deep.status_code == 200, deep.text
    depths = {e["id"]: e["depth"] for e in deep.json()["downstream"]}
    assert depths[str(s.rule_event)] == 2

    field = await client.get(
        f"/api/v1/projects/{s.slug}/dependencies", params={"entity": f"field:{s.amount}"}
    )
    certainty = {e["id"]: e["certainty"] for e in field.json()["downstream"]}
    assert certainty[str(s.sql_metric)] == "possible"
    assert certainty[str(s.composition_metric)] == "direct"
    # The headline counts only first-hop direct edges; name matches apart.
    assert field.json()["counts_by_kind"] == {"metric": 1, "event": 1}
    assert field.json()["possible_counts_by_kind"]["metric"] == 2
    assert field.json()["possible_counts_by_kind"]["fact_table"] == 1
    # Second-hop edges never reach the headline counts.
    deep_counts = deep.json()["counts_by_kind"]
    assert "alert_rule" in deep_counts and deep_counts["alert_rule"] == 1


@pytest.mark.asyncio
async def test_get_dependencies_rejects_bad_entity(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-bad")
    for raw in ("event", "widget:" + str(uuid.uuid4()), "event:not-a-uuid"):
        resp = await client.get(f"/api/v1/projects/{s.slug}/dependencies", params={"entity": raw})
        assert resp.status_code == 422, raw
    too_deep = await client.get(
        f"/api/v1/projects/{s.slug}/dependencies",
        params={"entity": f"event:{s.purchase}", "depth": 3},
    )
    assert too_deep.status_code == 422


@pytest.mark.asyncio
async def test_unknown_entity_is_not_a_404(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-unknown")
    resp = await client.get(
        f"/api/v1/projects/{s.slug}/dependencies", params={"entity": f"event:{uuid.uuid4()}"}
    )
    assert resp.status_code == 200
    assert resp.json()["entity"]["exists"] is False
    assert resp.json()["downstream"] == []


@pytest.mark.asyncio
async def test_post_impact_summary_wording_and_delete_still_allowed(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-impact")
    resp = await client.post(
        f"/api/v1/projects/{s.slug}/impact",
        json={
            "changes": [
                {"kind": "event", "id": str(s.purchase), "change": "delete"},
                {"kind": "event", "id": str(s.refund), "change": "deprecate"},
                {"kind": "fact_table", "id": str(s.fact_quoted), "change": "delete"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    purchase = _item_for(body, "event", s.purchase)
    assert purchase["summary"] == "2 metrics and 1 alert rule"
    assert purchase["change"] == {"kind": "event", "id": str(s.purchase), "change": "delete"}
    assert purchase["name"] == "purchase"
    assert {a["id"] for a in purchase["affected"]} == {
        str(s.composition_metric),
        str(s.ratio_metric),
        str(s.rule_event),
    }
    assert _item_for(body, "event", s.refund)["summary"] == "1 metric"
    assert _item_for(body, "fact_table", s.fact_quoted)["summary"] == "Nothing depends on it"

    # Warn only: the delete itself is not refused by its dependents.
    deleted = await client.delete(f"/api/v1/projects/{s.slug}/events/{s.purchase}")
    assert deleted.status_code == 204, deleted.text


@pytest.mark.asyncio
async def test_post_impact_validates_the_change_set(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-impact-bad")
    empty = await client.post(f"/api/v1/projects/{s.slug}/impact", json={"changes": []})
    assert empty.status_code == 422
    bad_change = await client.post(
        f"/api/v1/projects/{s.slug}/impact",
        json={"changes": [{"kind": "event", "id": str(s.purchase), "change": "explode"}]},
    )
    assert bad_change.status_code == 422


@pytest.mark.asyncio
async def test_branch_impact_from_real_branch_diff(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-branch")
    created = await client.post(f"/api/v1/projects/{s.slug}/branches", json={"name": "cleanup"})
    assert created.status_code == 201, created.text
    branch_id = created.json()["id"]

    async with TestSessionLocal() as session:
        copies = dict(
            (
                await session.execute(
                    select(Event.origin_id, Event.id).where(
                        Event.branch_id == uuid.UUID(branch_id),
                        Event.origin_id.in_([s.purchase, s.refund]),
                    )
                )
            ).all()
        )
    purchase_copy, refund_copy = copies[s.purchase], copies[s.refund]

    deleted = await client.delete(
        f"/api/v1/projects/{s.slug}/events/{purchase_copy}", params={"branch": branch_id}
    )
    assert deleted.status_code == 204, deleted.text
    deprecated = await client.patch(
        f"/api/v1/projects/{s.slug}/events/{refund_copy}",
        params={"branch": branch_id},
        json={"status": "deprecated"},
    )
    assert deprecated.status_code == 200, deprecated.text

    resp = await client.get(f"/api/v1/projects/{s.slug}/branches/{branch_id}/impact")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The deleted copy is read on main, where the references point.
    purchase = _item_for(body, "event", s.purchase)
    assert purchase["change"]["change"] == "delete"
    assert purchase["summary"] == "2 metrics and 1 alert rule"

    # The deprecated copy is read on the branch, and still finds main's metric.
    refund = _item_for(body, "event", refund_copy)
    assert refund["change"]["change"] == "deprecate"
    assert {a["id"] for a in refund["affected"]} == {str(s.ratio_metric)}

    # A branch that is not this project's answers the branch service's 404.
    unknown = await client.get(f"/api/v1/projects/{s.slug}/branches/{uuid.uuid4()}/impact")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_non_member_gets_404_and_viewer_can_read(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-members")
    created = await client.post(f"/api/v1/projects/{s.slug}/branches", json={"name": "b"})
    branch_id = created.json()["id"]

    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "outsider@example.com", "password": PASSWORD, "name": "Outsider"},
    )
    assert registered.status_code == 201, registered.text

    urls = [
        ("GET", f"/api/v1/projects/{s.slug}/dependencies?entity=event:{s.purchase}", None),
        (
            "POST",
            f"/api/v1/projects/{s.slug}/impact",
            {"changes": [{"kind": "event", "id": str(s.purchase), "change": "delete"}]},
        ),
        ("GET", f"/api/v1/projects/{s.slug}/branches/{branch_id}/impact", None),
    ]
    for method, url, body in urls:
        resp = await client.request(method, url, json=body)
        assert resp.status_code == 404, (url, resp.text)
        assert resp.json()["detail"] == "Project not found"

    # A viewer member reads all three; the POST is a read carrying a body.
    await add_member_by_slug(s.slug, "outsider@example.com", "viewer")
    for method, url, body in urls:
        resp = await client.request(method, url, json=body)
        assert resp.status_code == 200, (url, resp.text)


@pytest.mark.asyncio
async def test_post_impact_is_always_one_hop(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-impact-depth")
    resp = await client.post(
        f"/api/v1/projects/{s.slug}/impact",
        json={
            "changes": [{"kind": "event_type", "id": str(s.track), "change": "delete"}],
            "depth": 2,
        },
    )
    assert resp.status_code == 200, resp.text
    item = _item_for(resp.json(), "event_type", s.track)
    affected = item["affected"]
    assert isinstance(affected, list)
    assert all(a["depth"] == 1 for a in affected)
    ids = {a["id"] for a in affected}
    # The events of the type are one hop; what reads those events is two.
    assert {str(s.purchase), str(s.refund)} <= ids
    assert str(s.rule_event) not in ids
    assert str(s.composition_metric) not in ids


@pytest.mark.asyncio
async def test_foreign_project_ids_are_not_resolved_over_the_api(client: AsyncClient) -> None:
    mine = await seed_dependency_plan(client, "dep-api-iso-a")
    theirs = await seed_dependency_plan(client, "dep-api-iso-b")
    resp = await client.get(
        f"/api/v1/projects/{mine.slug}/dependencies",
        params={"entity": f"event:{theirs.purchase}", "depth": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity"]["exists"] is False
    assert body["upstream"] == [] and body["downstream"] == []
    assert body["counts_by_kind"] == {}

    impact = await client.post(
        f"/api/v1/projects/{mine.slug}/impact",
        json={"changes": [{"kind": "event_type", "id": str(theirs.track), "change": "delete"}]},
    )
    assert impact.status_code == 200, impact.text
    item = _item_for(impact.json(), "event_type", theirs.track)
    assert item["entity"]["exists"] is False
    assert item["affected"] == []


@pytest.mark.asyncio
async def test_branch_impact_pairs_a_renamed_copy_by_origin(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-branch-rename")
    created = await client.post(f"/api/v1/projects/{s.slug}/branches", json={"name": "rename"})
    assert created.status_code == 201, created.text
    branch_id = created.json()["id"]

    async with TestSessionLocal() as session:
        purchase_copy = await session.scalar(
            select(Event.id).where(
                Event.branch_id == uuid.UUID(branch_id), Event.origin_id == s.purchase
            )
        )
    assert purchase_copy is not None

    renamed = await client.patch(
        f"/api/v1/projects/{s.slug}/events/{purchase_copy}",
        params={"branch": branch_id},
        json={"name": "purchase_completed"},
    )
    assert renamed.status_code == 200, renamed.text

    resp = await client.get(f"/api/v1/projects/{s.slug}/branches/{branch_id}/impact")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    purchase = _item_for(body, "event", s.purchase)
    assert purchase["change"]["change"] == "rename"
    assert purchase["summary"] == "2 metrics and 1 alert rule"
    # One item for the row, not a delete plus a rename.
    assert sum(1 for i in body["items"] if i["entity"]["id"] == str(s.purchase)) == 1


@pytest.mark.asyncio
async def test_branch_impact_does_not_count_removed_children_twice(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-api-branch-type")
    created = await client.post(f"/api/v1/projects/{s.slug}/branches", json={"name": "drop"})
    assert created.status_code == 201, created.text
    branch_id = created.json()["id"]

    async with TestSessionLocal() as session:
        track_copy = await session.scalar(
            select(EventType.id).where(
                EventType.branch_id == uuid.UUID(branch_id), EventType.name == "track"
            )
        )
    assert track_copy is not None
    deleted = await client.delete(
        f"/api/v1/projects/{s.slug}/event-types/{track_copy}", params={"branch": branch_id}
    )
    assert deleted.status_code == 204, deleted.text

    resp = await client.get(f"/api/v1/projects/{s.slug}/branches/{branch_id}/impact")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    track = _item_for(body, "event_type", s.track)
    assert track["change"]["change"] == "delete"
    affected = {a["id"] for a in track["affected"]}
    # The type's events are removed too and listed as their own items.
    purchase = _item_for(body, "event", s.purchase)
    assert purchase["change"]["change"] == "delete"
    assert str(s.purchase) not in affected and str(s.refund) not in affected
    assert str(s.type_metric) in affected
