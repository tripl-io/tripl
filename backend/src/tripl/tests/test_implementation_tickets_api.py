"""Read surface for the merge-created tracker tickets.

The worker-side behaviour (create-on-merge, poll sync) lives in
``test_implementation_tickets.py``; this file covers only
``GET /projects/{slug}/branches/{branch_id}/implementation-tickets`` and its
per-event counterpart.
"""

import uuid

import pytest
from httpx import AsyncClient

from tripl.models.implementation_ticket import ImplementationTicket
from tripl.tests.conftest import TestSessionLocal


async def _create_project(client: AsyncClient, slug: str) -> str:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_branch(client: AsyncClient, slug: str, name: str = "feature") -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _seed_ticket(
    project_id: str,
    branch_id: str,
    *,
    external_key: str = "ENG-42",
    status: str = "open",
    event_ids: list[str] | None = None,
) -> uuid.UUID:
    """Insert the row the merge worker would have written."""
    ticket_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            ImplementationTicket(
                id=ticket_id,
                project_id=uuid.UUID(project_id),
                branch_id=uuid.UUID(branch_id),
                tracker_type="jira",
                external_id="10042",
                external_key=external_key,
                external_url=f"https://example.atlassian.net/browse/{external_key}",
                status=status,
                summary="Implement checkout-v2",
                event_ids=event_ids if event_ids is not None else [str(uuid.uuid4())],
            )
        )
        await session.commit()
    return ticket_id


def _url(slug: str, branch_id: str) -> str:
    return f"/api/v1/projects/{slug}/branches/{branch_id}/implementation-tickets"


@pytest.mark.asyncio
async def test_list_returns_the_ticket_opened_for_the_branch(client: AsyncClient) -> None:
    project_id = await _create_project(client, "tickets-found")
    branch_id = await _create_branch(client, "tickets-found")
    ticket_id = await _seed_ticket(project_id, branch_id)

    resp = await client.get(_url("tickets-found", branch_id))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    ticket = body[0]
    assert ticket["id"] == str(ticket_id)
    assert ticket["branch_id"] == branch_id
    assert ticket["external_key"] == "ENG-42"
    assert ticket["status"] == "open"
    # The link is the whole point of the endpoint — the UI renders it as an href.
    assert ticket["external_url"] == "https://example.atlassian.net/browse/ENG-42"
    assert ticket["tracker_type"] == "jira"
    assert ticket["summary"] == "Implement checkout-v2"
    assert ticket["closed_at"] is None


@pytest.mark.asyncio
async def test_list_is_empty_for_a_branch_with_no_ticket(client: AsyncClient) -> None:
    """A branch that never merged (or merged with the tracker off) is 200 + [],
    not 404 — the branch exists, it simply has no ticket."""
    await _create_project(client, "tickets-empty")
    branch_id = await _create_branch(client, "tickets-empty")

    resp = await client.get(_url("tickets-empty", branch_id))

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_404s_for_a_branch_belonging_to_another_project(client: AsyncClient) -> None:
    """Cross-project id: the branch is real but not this project's, so the ticket
    (and its Jira URL) must not leak through the other project's slug."""
    other_project_id = await _create_project(client, "tickets-owner")
    other_branch_id = await _create_branch(client, "tickets-owner")
    await _seed_ticket(other_project_id, other_branch_id, external_key="SECRET-1")
    await _create_project(client, "tickets-outsider")

    resp = await client.get(_url("tickets-outsider", other_branch_id))

    assert resp.status_code == 404
    assert "SECRET-1" not in resp.text


@pytest.mark.asyncio
async def test_list_404s_for_an_unknown_project(client: AsyncClient) -> None:
    await _create_project(client, "tickets-slug")
    branch_id = await _create_branch(client, "tickets-slug")

    resp = await client.get(_url("tickets-nope", branch_id))

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_requires_authentication(client: AsyncClient) -> None:
    project_id = await _create_project(client, "tickets-auth")
    branch_id = await _create_branch(client, "tickets-auth")
    await _seed_ticket(project_id, branch_id)

    await client.post("/api/v1/auth/logout")
    resp = await client.get(_url("tickets-auth", branch_id))

    assert resp.status_code == 401


# --- the same rows, read per EVENT ---------------------------
#
# One ticket per BRANCH and `event_ids` naming what that branch touched means an
# event carried by three merged branches was already named by three rows. Only
# the branch question could be asked of them.


async def _seed_event(client: AsyncClient, slug: str) -> str:
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    assert et.status_code == 201
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et.json()["id"], "name": "purchase:success"},
    )
    assert event.status_code == 201
    return event.json()["id"]


def _event_url(slug: str, event_id: str) -> str:
    return f"/api/v1/projects/{slug}/events/{event_id}/implementation-tickets"


@pytest.mark.asyncio
async def test_event_list_returns_every_branch_that_named_the_event(
    client: AsyncClient,
) -> None:
    project_id = await _create_project(client, "ev-tickets")
    event_id = await _seed_event(client, "ev-tickets")
    first_branch = await _create_branch(client, "ev-tickets", "feature-one")
    second_branch = await _create_branch(client, "ev-tickets", "feature-two")
    unrelated_branch = await _create_branch(client, "ev-tickets", "feature-three")
    await _seed_ticket(project_id, first_branch, external_key="ENG-1", event_ids=[event_id])
    await _seed_ticket(project_id, second_branch, external_key="ENG-2", event_ids=[event_id])
    await _seed_ticket(
        project_id, unrelated_branch, external_key="ENG-3", event_ids=[str(uuid.uuid4())]
    )

    resp = await client.get(_event_url("ev-tickets", event_id))

    assert resp.status_code == 200
    assert sorted(t["external_key"] for t in resp.json()) == ["ENG-1", "ENG-2"]


@pytest.mark.asyncio
async def test_event_list_is_empty_rather_than_404_when_no_ticket_named_it(
    client: AsyncClient,
) -> None:
    """The normal state. Rows exist only where the tracker is on and a branch
    merged, so an event with no ticket is a 200 and an empty list."""
    await _create_project(client, "ev-tickets-empty")
    event_id = await _seed_event(client, "ev-tickets-empty")

    resp = await client.get(_event_url("ev-tickets-empty", event_id))

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_a_branch_copy_reads_its_main_twins_ticket_history(client: AsyncClient) -> None:
    """`event_ids` holds MAIN ids, and the history belongs to the event rather
    than to one copy of it — the same rule the discussion follows."""
    project_id = await _create_project(client, "ev-tickets-branch")
    main_event_id = await _seed_event(client, "ev-tickets-branch")
    merged_branch = await _create_branch(client, "ev-tickets-branch", "already-merged")
    await _seed_ticket(project_id, merged_branch, external_key="ENG-9", event_ids=[main_event_id])

    open_branch = await _create_branch(client, "ev-tickets-branch", "still-open")
    branch_events = await client.get(
        f"/api/v1/projects/ev-tickets-branch/events?branch={open_branch}"
    )
    branch_event_id = branch_events.json()["items"][0]["id"]
    assert branch_event_id != main_event_id

    resp = await client.get(
        f"{_event_url('ev-tickets-branch', branch_event_id)}?branch={open_branch}"
    )

    assert resp.status_code == 200
    assert [t["external_key"] for t in resp.json()] == ["ENG-9"]


@pytest.mark.asyncio
async def test_event_list_404s_for_an_event_in_another_project(client: AsyncClient) -> None:
    """The leak that matters: another project's Jira key must not come back
    through this project's slug."""
    other_project_id = await _create_project(client, "ev-tickets-owner")
    other_event_id = await _seed_event(client, "ev-tickets-owner")
    other_branch = await _create_branch(client, "ev-tickets-owner")
    await _seed_ticket(
        other_project_id, other_branch, external_key="SECRET-2", event_ids=[other_event_id]
    )
    await _create_project(client, "ev-tickets-outsider")

    resp = await client.get(_event_url("ev-tickets-outsider", other_event_id))

    assert resp.status_code == 404
    assert "SECRET-2" not in resp.text


@pytest.mark.asyncio
async def test_event_list_requires_authentication(client: AsyncClient) -> None:
    await _create_project(client, "ev-tickets-auth")
    event_id = await _seed_event(client, "ev-tickets-auth")

    await client.post("/api/v1/auth/logout")
    resp = await client.get(_event_url("ev-tickets-auth", event_id))

    assert resp.status_code == 401
