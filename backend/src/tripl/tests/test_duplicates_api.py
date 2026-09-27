"""Duplicate detection routes and the scan dry-run name warnings (GH #265, F12).

Drives ``POST /projects/{slug}/events/duplicate-check``,
``GET /projects/{slug}/duplicates`` and ``POST /projects/{slug}/duplicates/dismiss``:
the issue's "Done when" on the demo project (a near-duplicate warns with a
working link to the existing event), a lint suggestion, clusters, dismissal,
the membership 404, and a viewer who may check but not dismiss. The last test
exercises the dry run's combinatorial-explosion and duplicate warnings.

Every name here is synthetic.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.models.event import Event
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.schemas.duplicates import DuplicateCandidateIn
from tripl.schemas.scan_config import ScanDryRunNameWarning
from tripl.services import duplicate_service
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.utils import name_warnings
from tripl.worker.utils.name_warnings import dry_run_name_warnings

PASSWORD = "Password123!"


async def _post(client: AsyncClient, url: str, body: dict[str, object]) -> dict[str, object]:
    resp = await client.post(url, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


async def _seed(
    client: AsyncClient, slug: str, names: list[tuple[str, str]]
) -> tuple[str, dict[str, uuid.UUID]]:
    """A project with one event type ``page`` and ``names`` as (name, status) events."""
    await _post(client, "/api/v1/projects", {"name": slug, "slug": slug, "description": ""})
    page = await _post(
        client,
        f"/api/v1/projects/{slug}/event-types",
        {"name": "page", "display_name": "Page"},
    )
    type_id = uuid.UUID(str(page["id"]))
    ids: dict[str, uuid.UUID] = {}
    async with TestSessionLocal() as session, session.begin():
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == slug))
        ).scalar_one()
        main_id = (
            await session.execute(
                select(PlanBranch.id).where(
                    PlanBranch.project_id == project_id,
                    PlanBranch.kind == BranchKind.main.value,
                )
            )
        ).scalar_one()
        for order, (name, status) in enumerate(names):
            event = Event(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_id,
                event_type_id=type_id,
                name=name,
                status=status,
                order=order,
            )
            session.add(event)
            ids[name] = event.id
    return str(type_id), ids


def _check_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/events/duplicate-check"


# ---------------------------------------------------------------------------
# Done when: the demo near-duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_near_duplicate_warns_with_a_working_link(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 201, resp.text
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == slug))
        ).scalar_one()
        existing = (
            await session.execute(
                select(Event)
                .join(PlanBranch, PlanBranch.id == Event.branch_id)
                .where(
                    Event.project_id == project_id,
                    Event.name == "Paywall View",
                    PlanBranch.kind == BranchKind.main.value,
                )
            )
        ).scalar_one()
        existing_id, type_id = existing.id, existing.event_type_id

    checked = await client.post(
        _check_url(slug),
        json={"candidates": [{"name": "Paywall Screen View", "event_type_id": str(type_id)}]},
    )
    assert checked.status_code == 200, checked.text
    [item] = checked.json()["items"]
    assert item["name"] == "Paywall Screen View"
    top = item["duplicates"][0]
    assert top["event_id"] == str(existing_id)
    assert top["name"] == "Paywall View"
    assert top["score"] >= checked.json()["threshold"] == 0.88
    assert "same event type" in top["reasons"]

    # The warning's "Open" link resolves.
    opened = await client.get(f"/api/v1/projects/{slug}/events/{top['event_id']}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["name"] == "Paywall View"

    # A snake_case name in the demo's Title Case catalog gets a conforming suggestion.
    linted = await client.post(
        _check_url(slug),
        json={"candidates": [{"name": "wishlist_screen_view", "event_type_id": str(type_id)}]},
    )
    assert linted.status_code == 200, linted.text
    [lint_item] = linted.json()["items"]
    assert lint_item["duplicates"] == []
    assert "case" in [issue["code"] for issue in lint_item["lint"]]
    assert lint_item["suggestion"] == "Wishlist Screen View"


# ---------------------------------------------------------------------------
# Lint on a controlled catalog
# ---------------------------------------------------------------------------

TITLE_CATALOG = [
    ("Home Screen View", "live"),
    ("Cart Screen View", "live"),
    ("Profile Screen View", "live"),
    ("Order History View", "live"),
    ("Settings Screen View", "live"),
    ("Search Results View", "live"),
]


@pytest.mark.asyncio
async def test_lint_suggests_a_conforming_name(client: AsyncClient) -> None:
    type_id, _ids = await _seed(client, "dup-lint", TITLE_CATALOG)
    resp = await client.post(
        _check_url("dup-lint"),
        json={
            "candidates": [
                {"name": "checkout_screen_view", "event_type_id": type_id},
                {"name": "View Wishlist", "event_type_id": type_id},
                {"name": "Checkout Screen View", "event_type_id": type_id},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    snake, verb_first, conforming = resp.json()["items"]

    assert [issue["code"] for issue in snake["lint"]] == ["case"]
    assert snake["suggestion"] == "Checkout Screen View"
    assert snake["convention"]["case"] == "space"
    assert snake["lint_applicable"] is True

    assert [issue["code"] for issue in verb_first["lint"]] == ["verb_order"]
    assert verb_first["suggestion"] == "Wishlist View"

    assert conforming["lint"] == []
    assert conforming["suggestion"] is None
    assert conforming["duplicates"] == []


@pytest.mark.asyncio
async def test_candidate_excludes_itself_and_reports_exact_names(client: AsyncClient) -> None:
    type_id, ids = await _seed(client, "dup-self", TITLE_CATALOG)
    resp = await client.post(
        _check_url("dup-self"),
        json={
            "candidates": [
                {
                    "name": "Home Screen View",
                    "event_type_id": type_id,
                    "event_id": str(ids["Home Screen View"]),
                },
                {"name": "home_screen_view", "event_type_id": type_id},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    itself, renamed = resp.json()["items"]
    assert itself["duplicates"] == []
    assert renamed["duplicates"][0]["event_id"] == str(ids["Home Screen View"])
    assert renamed["duplicates"][0]["score"] == 1.0
    assert "same name" in renamed["duplicates"][0]["reasons"]
    # An exact duplicate is not linted: the fix is to open the existing event.
    assert renamed["lint"] == []


@pytest.mark.asyncio
async def test_candidate_batch_is_capped(client: AsyncClient) -> None:
    type_id, _ids = await _seed(client, "dup-cap", TITLE_CATALOG)
    candidate = {"name": "Cart View", "event_type_id": type_id}
    over = await client.post(_check_url("dup-cap"), json={"candidates": [candidate] * 501})
    assert over.status_code == 422
    empty = await client.post(_check_url("dup-cap"), json={"candidates": []})
    assert empty.status_code == 422


# ---------------------------------------------------------------------------
# Clusters and dismissal
# ---------------------------------------------------------------------------

CLUSTER_CATALOG = [
    ("Paywall View", "live"),
    ("Paywall Screen View", "implemented"),
    ("Home View", "live"),
    ("Onboarding Step 1 View", "live"),
    ("Onboarding Step 2 View", "live"),
    # Drafts are not clustered.
    ("paywall_view", "draft"),
]


@pytest.mark.asyncio
async def test_clusters_list_and_dismiss(client: AsyncClient) -> None:
    _type_id, ids = await _seed(client, "dup-clusters", CLUSTER_CATALOG)
    url = "/api/v1/projects/dup-clusters/duplicates"

    listed = await client.get(url)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["total"] == 1
    assert page["next_cursor"] is None
    [cluster] = page["items"]
    assert {event["name"] for event in cluster["events"]} == {"Paywall View", "Paywall Screen View"}
    assert cluster["score"] >= 0.88
    assert all(event["volume_7d"] == 0 for event in cluster["events"])

    dismissed = await client.post(
        f"{url}/dismiss",
        json={
            "event_a_id": str(ids["Paywall Screen View"]),
            "event_b_id": str(ids["Paywall View"]),
        },
    )
    assert dismissed.status_code == 200, dismissed.text
    body = dismissed.json()
    assert body["created"] is True
    # Stored ordered, whatever order the caller sent.
    assert str(body["event_a_id"]) < str(body["event_b_id"])

    again = await client.post(
        f"{url}/dismiss",
        json={
            "event_a_id": str(ids["Paywall View"]),
            "event_b_id": str(ids["Paywall Screen View"]),
        },
    )
    assert again.status_code == 200
    assert again.json()["created"] is False

    after = await client.get(url)
    assert after.json()["total"] == 0
    assert after.json()["items"] == []


@pytest.mark.asyncio
async def test_dismiss_validates_its_pair(client: AsyncClient) -> None:
    _type_id, ids = await _seed(client, "dup-bad", CLUSTER_CATALOG)
    url = "/api/v1/projects/dup-bad/duplicates/dismiss"
    same = await client.post(
        url,
        json={"event_a_id": str(ids["Home View"]), "event_b_id": str(ids["Home View"])},
    )
    assert same.status_code == 422
    unknown = await client.post(
        url,
        json={"event_a_id": str(ids["Home View"]), "event_b_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 404
    bad_cursor = await client.get("/api/v1/projects/dup-bad/duplicates?cursor=abc")
    assert bad_cursor.status_code == 422


# ---------------------------------------------------------------------------
# Membership and roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_member_gets_404_and_viewer_can_check_but_not_dismiss(
    client: AsyncClient,
) -> None:
    type_id, ids = await _seed(client, "dup-members", CLUSTER_CATALOG)
    base = "/api/v1/projects/dup-members"
    check_body = {"candidates": [{"name": "Paywall Screen View", "event_type_id": type_id}]}
    dismiss_body = {
        "event_a_id": str(ids["Paywall View"]),
        "event_b_id": str(ids["Paywall Screen View"]),
    }

    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "dup-outsider@example.com", "password": PASSWORD, "name": "Outsider"},
    )
    assert registered.status_code == 201, registered.text

    for resp in (
        await client.post(_check_url("dup-members"), json=check_body),
        await client.get(f"{base}/duplicates"),
        await client.post(f"{base}/duplicates/dismiss", json=dismiss_body),
    ):
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Project not found"

    await add_member_by_slug("dup-members", "dup-outsider@example.com", "viewer")
    checked = await client.post(_check_url("dup-members"), json=check_body)
    assert checked.status_code == 200, checked.text
    found = {hit["event_id"] for hit in checked.json()["items"][0]["duplicates"]}
    assert str(ids["Paywall View"]) in found
    clusters = await client.get(f"{base}/duplicates")
    assert clusters.status_code == 200, clusters.text
    denied = await client.post(f"{base}/duplicates/dismiss", json=dismiss_body)
    assert denied.status_code == 403, denied.text


# ---------------------------------------------------------------------------
# Scan dry run: combinatorial explosion and duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_dry_run_name_warnings(client: AsyncClient) -> None:
    type_id, ids = await _seed(client, "dup-scan", [("shop:tap:hat", "live")])
    async with TestSessionLocal() as session:
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == "dup-scan"))
        ).scalar_one()
        new_names = [f"shop:tap:item_{k}" for k in range(60)] + ["Shop:Tap:Hat"]

        def run(sync_session: Session) -> list[dict[str, object]]:
            return dry_run_name_warnings(
                sync_session,
                project_id,
                new_names_by_type={"page": new_names, "brand_new": ["x:y:z"]},
                event_type_ids={"page": uuid.UUID(type_id), "brand_new": None},
                name_format="{category}:{action}:{label}",
                cardinality_threshold=100,
            )

        warnings = await session.run_sync(run)

    parsed = [ScanDryRunNameWarning.model_validate(w) for w in warnings]
    explosions = [w for w in parsed if w.code == "combinatorial_explosion"]
    duplicates = [w for w in parsed if w.code == "duplicate"]

    [explosion] = explosions
    assert explosion.event_type == "page"
    assert explosion.count == 60
    assert explosion.slot == 2
    assert explosion.slot_label == "{label}"
    assert explosion.pattern == "shop:tap:*"

    # ``shop:tap:item_N`` differs from ``shop:tap:hat`` in the label slot, so
    # under the rule it is a different event; only the case variant matches.
    [duplicate] = duplicates
    assert duplicate.name == "Shop:Tap:Hat"
    assert duplicate.duplicate_of is not None
    assert duplicate.duplicate_of.event_id == ids["shop:tap:hat"]
    assert duplicate.duplicate_of.score == 1.0


@pytest.mark.asyncio
async def test_scan_dry_run_name_warnings_survive_a_failing_catalog_read(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    type_id, _ids = await _seed(client, "dup-scan-fail", [("shop:tap:hat", "live")])

    def broken_catalog(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("catalog read failed")

    monkeypatch.setattr(name_warnings, "_catalog", broken_catalog)
    async with TestSessionLocal() as session:
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == "dup-scan-fail"))
        ).scalar_one()
        new_names = [f"shop:tap:item_{k}" for k in range(60)] + ["Shop:Tap:Hat"]

        def run(sync_session: Session) -> list[dict[str, object]]:
            return dry_run_name_warnings(
                sync_session,
                project_id,
                new_names_by_type={"page": new_names},
                event_type_ids={"page": uuid.UUID(type_id)},
                name_format="{category}:{action}:{label}",
                cardinality_threshold=100,
            )

        warnings = await session.run_sync(run)
        # The session is still usable: the failure stayed inside the savepoint.
        assert (
            await session.execute(select(Project.id).where(Project.id == project_id))
        ).scalar_one() == project_id

    codes = [w["code"] for w in warnings]
    # The explosion was computed before the read failed; no duplicate, no raise.
    assert codes == ["combinatorial_explosion"]


@pytest.mark.asyncio
async def test_naming_rule_keeps_names_one_value_apart(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    type_id, ids = await _seed(client, "dup-ruled", [("shop:tap:filters", "live")])
    event_type_id = uuid.UUID(type_id)
    async with TestSessionLocal() as session:
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == "dup-ruled"))
        ).scalar_one()
        branch_id = (
            await session.execute(
                select(PlanBranch.id).where(
                    PlanBranch.project_id == project_id,
                    PlanBranch.kind == BranchKind.main.value,
                )
            )
        ).scalar_one()

        async def check(name: str) -> list[uuid.UUID]:
            response = await duplicate_service.check_candidates(
                session,
                project_id,
                branch_id,
                [DuplicateCandidateIn(name=name, event_type_id=event_type_id)],
            )
            return [hit.event_id for hit in response.items[0].duplicates]

        # No rule: a plural is a near-duplicate.
        assert await check("shop:tap:filter") == [ids["shop:tap:filters"]]

        async def ruled_formats(
            _session: object, _project_id: uuid.UUID, _type_ids: object
        ) -> dict[uuid.UUID, str]:
            return {event_type_id: "{category}:{action}:{label}"}

        monkeypatch.setattr(duplicate_service, "_name_formats", ruled_formats)
        # Under the rule the label is a different warehouse value: another event.
        assert await check("shop:tap:filter") == []
        # A spelling variant of the same values is still the same event.
        assert await check("Shop:Tap:Filters") == [ids["shop:tap:filters"]]
