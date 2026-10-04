"""``?branch=`` context: main by id is main, and a merged or closed branch is read-only.

``GET /branches`` hands out main's own id and
``?branch=<main id>`` is accepted, but every plan service decided "main" by
``branch_id is None``. The dependency passed the id through, so the write landed
on main while skipping the name-format delete guard (the name-format outage) and
the event-type, meta-field and project cache busts.

nothing refused a plan write to a merged or closed branch,
although the docs say its writes are refused. The refusal must not outrank
authorization: a caller the route's write gate turns away gets that gate's 403,
whichever order the route declares ``?branch=`` and the gate in.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.api.deps import ORG_ADMIN_REQUIRED, WRITE_GATES, get_branch_id_override
from tripl.main import app
from tripl.models.data_source import DataSource
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.schemas.field_definition import (
    FieldDefinitionBulkCreate,
    FieldDefinitionCreate,
    FieldDefinitionUpdate,
    FieldReorder,
)
from tripl.services import field_service
from tripl.tests._members import add_member_by_slug
from tripl.tests._project_ids import project_id_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch, _transition
from tripl.tests.test_rbac import _register, _set_role, iter_api_routes

# --- helpers ------------------------------------------------------------------


async def _project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/projects", json={"name": slug, "slug": slug, "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _event_type(client: AsyncClient, slug: str, name: str = "track") -> uuid.UUID:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _field(client: AsyncClient, slug: str, event_type_id: uuid.UUID, name: str) -> uuid.UUID:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": name, "display_name": name, "field_type": "string"},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _main_branch(client: AsyncClient, slug: str) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/projects/{slug}/branches")
    assert resp.status_code == 200, resp.text
    return next(b for b in resp.json()["items"] if b["kind"] == "main")


async def _scan_names_events_by(
    *, project_id: uuid.UUID, event_type_id: uuid.UUID, name_format: str
) -> None:
    """A scan config whose event name format reads ``name_format``'s columns."""
    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add(data_source)
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                data_source_id=data_source.id,
                project_id=project_id,
                event_type_id=event_type_id,
                name="Old events (iOS)",
                base_query="SELECT * FROM events",
                time_column="time",
                event_name_format=name_format,
                cardinality_threshold=100,
            )
        )
        await session.commit()


async def _field_exists(event_type_id: uuid.UUID, name: str) -> bool:
    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == event_type_id,
                FieldDefinition.name == name,
            )
        )
        return row is not None


def _record_dropped_prefixes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    dropped: list[str] = []

    async def record(prefix: str) -> None:
        dropped.append(prefix)

    monkeypatch.setattr(cache, "delete_prefix", record)
    return dropped


# --- main spelled by its id is main ---------------------------------------------


@pytest.mark.asyncio
async def test_a_name_format_field_cannot_be_deleted_from_main_by_its_id(
    client: AsyncClient,
) -> None:
    """The .121 scenario end to end: a stale ``?branch=<main id>`` from the
    monitoring page's Edit link must not open the door the guard closes."""
    slug = "bctx-guard-http"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    field_id = await _field(client, slug, event_type_id, "action")
    await _scan_names_events_by(
        project_id=project_id, event_type_id=event_type_id, name_format="{action}"
    )
    main_id = (await _main_branch(client, slug))["id"]

    resp = await client.delete(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields/{field_id}?branch={main_id}"
    )

    assert resp.status_code == 409, resp.text
    assert "{action}" in resp.json()["detail"]
    assert await _field_exists(event_type_id, "action")


async def _main_write_sequence(
    client: AsyncClient, slug: str, query: str, dropped: list[str]
) -> list[tuple[str, list[str]]]:
    """One write per main cache-bust site, each with the prefixes it dropped.

    Prefixes are recorded with the slug factored out, so two projects' runs
    compare equal when the writes behave the same.
    """
    steps: list[tuple[str, list[str]]] = []

    async def step(label: str, method: str, path: str, **kwargs: Any) -> Any:
        dropped.clear()
        resp = await client.request(method, f"/api/v1/projects/{slug}{path}{query}", **kwargs)
        assert resp.status_code in (200, 201, 204), f"{label}: {resp.status_code} {resp.text}"
        steps.append((label, sorted(p.replace(slug, "{slug}") for p in dropped)))
        return resp.json() if resp.status_code != 204 else None

    et = await step(
        "create event type", "POST", "/event-types", json={"name": "signup", "display_name": "S"}
    )
    await step(
        "rename event type", "PATCH", f"/event-types/{et['id']}", json={"display_name": "Sign up"}
    )
    await step(
        "create field",
        "POST",
        f"/event-types/{et['id']}/fields",
        json={"name": "screen", "display_name": "Screen", "field_type": "string"},
    )
    mf = await step(
        "create meta field",
        "POST",
        "/meta-fields",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )
    await step(
        "update meta field", "PATCH", f"/meta-fields/{mf['id']}", json={"display_name": "Team"}
    )
    await step("delete meta field", "DELETE", f"/meta-fields/{mf['id']}")
    event = await step(
        "create event", "POST", "/events", json={"event_type_id": et["id"], "name": "signup:done"}
    )
    await step("update event", "PATCH", f"/events/{event['id']}", json={"description": "edited"})
    await step("delete event", "DELETE", f"/events/{event['id']}")
    await step("delete event type", "DELETE", f"/event-types/{et['id']}")
    return steps


@pytest.mark.asyncio
async def test_main_by_id_drops_every_cache_main_without_the_parameter_drops(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?branch=<main id>`` is exactly main, for every service's cache bust.

    Covers the event-type, meta-field and event services, which still key "main"
    on ``branch_id is None``, so only the dependency's normalisation reaches
    them. Before it, each of these writes landed on main and left the list
    caches serving pre-write data for up to 300 s.
    """
    dropped = _record_dropped_prefixes(monkeypatch)
    plain_project = await _project(client, "bctx-plain")
    byid_project = await _project(client, "bctx-byid")
    main_id = (await _main_branch(client, "bctx-byid"))["id"]

    plain = await _main_write_sequence(client, "bctx-plain", "", dropped)
    by_id = await _main_write_sequence(client, "bctx-byid", f"?branch={main_id}", dropped)

    # Cache keys carry the project id (F20 PR3), so compare with each project's
    # own id folded to one placeholder.
    def _same_shape(
        steps: list[tuple[str, list[str]]], project_id: uuid.UUID
    ) -> list[tuple[str, list[str]]]:
        return [
            (name, [prefix.replace(str(project_id), "<project>") for prefix in prefixes])
            for name, prefixes in steps
        ]

    # Not vacuous: every step invalidates something on the plain path.
    assert all(prefixes for _, prefixes in plain), plain
    assert _same_shape(by_id, byid_project) == _same_shape(plain, plain_project)


@pytest.mark.asyncio
async def test_main_stays_writable_by_id_although_it_is_stored_as_merged(
    client: AsyncClient,
) -> None:
    """Main's row carries ``status="merged"``. The read-only refusal must key on
    a working branch's status, never on main's."""
    slug = "bctx-main-merged"
    await _project(client, slug)
    main = await _main_branch(client, slug)
    assert main["status"] == "merged"

    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types?branch={main['id']}",
        json={"name": "track", "display_name": "Track"},
    )

    assert resp.status_code == 201, resp.text


_Ids = dict[str, uuid.UUID]
_FieldWrite = Callable[[AsyncSession, str, uuid.UUID, uuid.UUID, _Ids], Awaitable[object]]


async def _create(
    session: AsyncSession, slug: str, et: uuid.UUID, branch: uuid.UUID, ids: _Ids
) -> object:
    data = FieldDefinitionCreate(name="screen", display_name="Screen", field_type="string")
    return await field_service.create_field(session, slug, et, data, branch)


async def _bulk(
    session: AsyncSession, slug: str, et: uuid.UUID, branch: uuid.UUID, ids: _Ids
) -> object:
    data = FieldDefinitionBulkCreate(
        fields=[FieldDefinitionCreate(name="button", display_name="Button", field_type="string")]
    )
    return await field_service.bulk_create_fields(session, slug, et, data, branch)


async def _update(
    session: AsyncSession, slug: str, et: uuid.UUID, branch: uuid.UUID, ids: _Ids
) -> object:
    data = FieldDefinitionUpdate(display_name="Label")
    return await field_service.update_field(session, slug, et, ids["label"], data, branch)


async def _reorder(
    session: AsyncSession, slug: str, et: uuid.UUID, branch: uuid.UUID, ids: _Ids
) -> object:
    data = FieldReorder(field_ids=[ids["label"], ids["action"]])
    return await field_service.reorder_fields(session, slug, et, data, branch)


async def _delete(
    session: AsyncSession, slug: str, et: uuid.UUID, branch: uuid.UUID, ids: _Ids
) -> object:
    await field_service.delete_field(session, slug, et, ids["label"], branch)
    return None


_FIELD_WRITES: list[tuple[str, _FieldWrite]] = [
    ("create_field", _create),
    ("bulk_create_fields", _bulk),
    ("update_field", _update),
    ("reorder_fields", _reorder),
    ("delete_field", _delete),
]


async def _seed_field_plan(client: AsyncClient, slug: str) -> tuple[uuid.UUID, _Ids]:
    """A project whose ``track`` event type has a scan-named ``action`` field
    and a free ``label`` field; returns the type id and the ids to write with."""
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    ids: _Ids = {
        "action": await _field(client, slug, event_type_id, "action"),
        "label": await _field(client, slug, event_type_id, "label"),
    }
    await _scan_names_events_by(
        project_id=project_id, event_type_id=event_type_id, name_format="{action}"
    )
    ids["main"] = uuid.UUID((await _main_branch(client, slug))["id"])
    return event_type_id, ids


@pytest.mark.asyncio
async def test_field_service_guards_main_however_the_caller_spells_it(
    client: AsyncClient,
) -> None:
    """In-process, past the dependency: ``resolve_branch_id`` accepts main's
    own id, so the outage guard must read main-ness off the row."""
    slug = "bctx-guard-service"
    event_type_id, ids = await _seed_field_plan(client, slug)

    for spelling in (None, ids["main"]):
        async with TestSessionLocal() as session:
            with pytest.raises(HTTPException) as refused:
                await field_service.delete_field(
                    session, slug, event_type_id, ids["action"], spelling
                )
        assert refused.value.status_code == 409, spelling
        assert await _field_exists(event_type_id, "action")


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "write"), _FIELD_WRITES, ids=[w[0] for w in _FIELD_WRITES])
async def test_every_field_write_on_main_by_id_drops_the_event_type_list(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, label: str, write: _FieldWrite
) -> None:
    """Each field_service write decides main from the row, so the list cache
    goes for main's id exactly as it does for ``None``."""
    slug = f"bctx-fs-{label.replace('_', '-')}"
    event_type_id, ids = await _seed_field_plan(client, slug)
    dropped = _record_dropped_prefixes(monkeypatch)

    async with TestSessionLocal() as session:
        await write(session, slug, event_type_id, ids["main"], ids)

    assert cache.prefix_event_types(await project_id_by_slug(slug)) in dropped


@pytest.mark.asyncio
async def test_a_field_write_on_a_working_branch_leaves_mains_list_alone(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row-derived flag is not simply always true: a branch write keeps
    main's cache, and the branch may still stage a name-format field's removal."""
    slug = "bctx-fs-branch"
    await _seed_field_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    listed = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_type = next(t for t in listed.json() if t["name"] == "track")
    action = next(f for f in branch_type["field_definitions"] if f["name"] == "action")
    dropped = _record_dropped_prefixes(monkeypatch)

    async with TestSessionLocal() as session:
        await field_service.delete_field(
            session,
            slug,
            uuid.UUID(branch_type["id"]),
            uuid.UUID(action["id"]),
            uuid.UUID(branch_id),
        )

    assert cache.prefix_event_types(await project_id_by_slug(slug)) not in dropped


# --- a merged or closed branch takes no plan writes -------------


def _gate_calls(route: APIRoute) -> set[Any]:
    calls: set[Any] = set()

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            calls.add(sub.call)
            walk(sub)

    walk(route.dependant)
    return calls


def _branch_routes(*, writes: bool) -> list[tuple[str, str]]:
    """``(method, path)`` for every route taking ``?branch=``, split by write gate."""
    found: list[tuple[str, str]] = []
    for path, route in iter_api_routes():
        calls = _gate_calls(route)
        if get_branch_id_override not in calls or bool(calls & WRITE_GATES) != writes:
            continue
        found.extend((method, path) for method in sorted(route.methods or set()))
    return found


# Write-gated, but derives the search index from the plan without changing it.
_REINDEX = ("POST", "/api/v1/projects/{slug}/search/reindex")


def _fill(path: str, slug: str) -> str:
    """A concrete URL: the project's slug, and a fresh uuid for every other id."""
    path = path.replace("{slug}", slug)
    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid.uuid4()), path)


async def _read_only_branches(client: AsyncClient, slug: str) -> dict[str, str]:
    """A project with one merged and one closed branch, by status."""
    await _project(client, slug)
    await _event_type(client, slug)
    merged = await _create_branch(client, slug, "shipped")
    closed = await _create_branch(client, slug, "shelved")
    resp = await _approve_and_merge(client, slug, merged)
    assert resp.status_code == 200, resp.text
    assert (await _transition(client, slug, closed, "close"))["status"] == "closed"
    return {"merged": merged, "closed": closed}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["merged", "closed"])
async def test_every_write_route_refuses_a_read_only_branch(
    client: AsyncClient, state: str
) -> None:
    """Enumerated from the live app, so a new branch-scoped write route is
    covered the day it is added. The refusal comes from the dependency, ahead of
    the route's own path and body validation, so placeholder ids and an empty
    body are enough to reach it."""
    slug = f"bctx-ro-{state}"
    branch_id = (await _read_only_branches(client, slug))[state]
    write_gated = _branch_routes(writes=True)
    assert _REINDEX in write_gated
    routes = [route for route in write_gated if route != _REINDEX]
    assert len(routes) >= 30, routes

    wrong: dict[str, str] = {}
    for method, path in routes:
        resp = await client.request(method, f"{_fill(path, slug)}?branch={branch_id}", json={})
        if resp.status_code != 409 or state not in resp.json()["detail"]:
            wrong[f"{method} {path}"] = f"{resp.status_code} {resp.text[:120]}"
    assert not wrong, wrong


# Routes whose signature declares ``?branch=`` ahead of the write gate, so FastAPI
# reaches the branch first. The 409 used to come out of these before the gate
# could refuse the caller; the routes that declare the gate first gave its 403.
_BRANCH_BEFORE_GATE = [
    ("POST", "/api/v1/projects/{slug}/events"),
    ("PATCH", "/api/v1/projects/{slug}/events/{event_id}"),
    ("POST", "/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate_id}/accept"),
    ("POST", "/api/v1/projects/{slug}/ai/describe-event"),
]

# What the gate says to each caller. Every gate checks a key's scope first; a
# viewer (an organization member with a viewer project row, F20 PR4) fails the
# editor gate's project-role check, or the owner gate's organization-role check
# on retire-unused-variables, the one owner-gated route that takes ``?branch=``.
_GATE_REFUSALS = {
    "viewer": {"Editor access to this project is required", ORG_ADMIN_REQUIRED},
    "read-key": {"API key has read-only scope"},
}


def _resolves_the_branch_first(method: str, path: str) -> bool:
    """Whether FastAPI reaches ``?branch=`` before the route's first write gate."""
    route = next(r for p, r in iter_api_routes() if p == path and method in (r.methods or set()))
    order: list[Any] = []

    def walk(dependant: Any) -> None:
        # FastAPI solves a dependency's own dependencies before the dependency.
        for sub in dependant.dependencies:
            walk(sub)
            order.append(sub.call)

    walk(route.dependant)
    first_gate = min(i for i, call in enumerate(order) if call in WRITE_GATES)
    return order.index(get_branch_id_override) < first_gate


@asynccontextmanager
async def _signed_in_as(
    principal: str, owner: AsyncClient, slug: str
) -> AsyncIterator[tuple[AsyncClient, dict[str, str]]]:
    """A second client acting as ``principal``, with the headers it must send.

    The viewer is made a member of ``slug`` so the answer under test is the
    write gate's 403, not the membership gate's 404.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        if principal == "read-key":
            resp = await owner.post("/api/v1/me/api-keys", json={"name": "agent", "scope": "read"})
            assert resp.status_code == 201, resp.text
            yield other, {"Authorization": f"Bearer {resp.json()['token']}"}
            return
        await _register(other, "viewer@example.com")
        await add_member_by_slug(slug, "viewer@example.com", "viewer")
        # A role change ends the user's sessions, so sign in again after it.
        await _set_role(owner, "viewer@example.com", "viewer")
        resp = await other.post(
            "/api/v1/auth/login",
            json={"email": "viewer@example.com", "password": "Password123!"},
        )
        assert resp.status_code == 200, resp.text
        # A plain organization member whose row in this project is read-only.
        assert resp.json()["role"] == "member"
        project = await other.get(f"/api/v1/projects/{slug}")
        assert project.status_code == 200, project.text
        assert project.json()["my_role"] == "viewer"
        assert project.json()["can_mutate"] is False
        yield other, {}


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", ["viewer", "read-key"])
async def test_the_write_gate_answers_before_the_read_only_409(
    client: AsyncClient, principal: str
) -> None:
    """A caller the gate refuses gets the gate's 403 on a closed branch, on
    every write route. The 409 told it to reopen the branch, which it cannot:
    ``/transition`` is behind the same editor gate. Enumerated from the live
    app like the refusal test above, so a route added later in either order is
    held to it."""
    slug = f"bctx-authz-{principal}"
    branch_id = (await _read_only_branches(client, slug))["closed"]
    routes = _branch_routes(writes=True)
    assert len(routes) >= 30, routes
    # Not vacuous: these are the routes where the order ever mattered.
    for method, path in _BRANCH_BEFORE_GATE:
        assert (method, path) in routes, (method, path)
        assert _resolves_the_branch_first(method, path), (
            f"{method} {path} now runs its gate first; name a route that does "
            "not, or this test stops covering the order the refusal must not rely on"
        )

    wrong: dict[str, str] = {}
    async with _signed_in_as(principal, client, slug) as (caller, headers):
        for method, path in routes:
            resp = await caller.request(
                method, f"{_fill(path, slug)}?branch={branch_id}", json={}, headers=headers
            )
            if resp.status_code != 403 or resp.json()["detail"] not in _GATE_REFUSALS[principal]:
                wrong[f"{method} {path}"] = f"{resp.status_code} {resp.text[:120]}"
    assert not wrong, wrong


@pytest.mark.asyncio
async def test_reads_of_a_merged_branch_still_answer(client: AsyncClient) -> None:
    """Only the write gate makes a request a write. ``POST /ai/ask`` carries a
    body but no gate, and the command palette sends it with the active branch,
    so it must not be refused alongside the writes."""
    slug = "bctx-ro-reads"
    branch_id = (await _read_only_branches(client, slug))["merged"]
    routes = _branch_routes(writes=False)
    assert ("POST", "/api/v1/projects/{slug}/ai/ask") in routes
    assert len(routes) >= 10, routes

    refused: dict[str, str] = {}
    for method, path in routes:
        resp = await client.request(method, f"{_fill(path, slug)}?branch={branch_id}", json={})
        if resp.status_code == 409:
            refused[f"{method} {path}"] = resp.text[:120]
    assert not refused, refused

    listed = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    assert listed.status_code == 200, listed.text
    assert [t["name"] for t in listed.json()] == ["track"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["merged", "closed"])
async def test_a_read_only_branch_can_still_be_reindexed(client: AsyncClient, state: str) -> None:
    """The runbook rebuilds the search index "once per project AND per plan
    branch" after a migration. That touches a merged or closed branch
    legitimately, since it changes nothing in the plan."""
    slug = f"bctx-reindex-{state}"
    branch_id = (await _read_only_branches(client, slug))[state]
    method, path = _REINDEX

    resp = await client.request(method, f"{_fill(path, slug)}?branch={branch_id}")

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_merged_branch_does_not_drift_from_what_it_merged(client: AsyncClient) -> None:
    """The issue's scenario: ``PATCH /events/{id}?branch=<merged>`` used to land."""
    slug = "bctx-ro-drift"
    await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": str(event_type_id), "name": "purchase:success"},
    )
    assert created.status_code == 201, created.text
    branch_id = await _create_branch(client, slug)
    assert (await _approve_and_merge(client, slug, branch_id)).status_code == 200
    events = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    event = events.json()["items"][0]

    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}",
        json={"description": "edited after the merge"},
    )

    assert resp.status_code == 409, resp.text
    assert "merged" in resp.json()["detail"]
    after = await client.get(f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}")
    assert after.json()["description"] == event["description"]


@pytest.mark.asyncio
async def test_a_closed_branch_takes_writes_again_once_reopened(client: AsyncClient) -> None:
    """Closed is shelved, not final: reopening restores the branch's writes."""
    slug = "bctx-ro-reopen"
    await _project(client, slug)
    await _event_type(client, slug)
    branch_id = await _create_branch(client, slug)
    listed = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    url = f"/api/v1/projects/{slug}/event-types/{listed.json()[0]['id']}?branch={branch_id}"

    assert (await client.patch(url, json={"description": "draft"})).status_code == 200
    assert (await _transition(client, slug, branch_id, "close"))["status"] == "closed"
    closed = await client.patch(url, json={"description": "while closed"})
    assert closed.status_code == 409, closed.text
    assert "reopen" in closed.json()["detail"]
    assert (await _transition(client, slug, branch_id, "reopen"))["status"] == "draft"
    assert (await client.patch(url, json={"description": "reopened"})).status_code == 200
