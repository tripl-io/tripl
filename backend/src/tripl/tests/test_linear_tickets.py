"""Linear implementation tickets (GH #258).

* the tracker config accepts ``tracker_type: linear`` with a ``team_id`` and a
  Linear API key that is encrypted, owner-gated and never echoed — exactly the
  Jira token's handling — and switching vendors drops the stored credential;
* a merged branch opens a Linear issue (GraphQL mocked) and a redelivery adopts
  the issue carrying the branch marker instead of opening a second one;
* the status sync maps Linear's ``completed`` state type to ``implemented``;
* ``add_ticket_comment`` posts on the tracker that opened the ticket (Linear and
  Jira), and never raises.

All HTTP is mocked; nothing here opens a socket.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import tripl.alerting_validation as av
from tripl.crypto import decrypt_value, encrypt_value
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.services import implementation_ticket_service
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts_channels, tracker_clients
from tripl.worker.tasks import implementation_tickets as impl_tasks

LINEAR_KEY = "lin_api_secret_key_123"
TEAM_ID = "9cfb482a-81e3-4154-b5b9-2c805e70a02d"


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------


async def _create_project(client: AsyncClient, slug: str) -> None:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_linear_config_stores_encrypted_key_and_never_echoes_it(
    client: AsyncClient,
) -> None:
    await _create_project(client, "linear-cfg")

    resp = await client.patch(
        "/api/v1/projects/linear-cfg/tracker-config",
        json={
            "enabled": True,
            "tracker_type": "linear",
            "team_id": TEAM_ID,
            "api_token": LINEAR_KEY,
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tracker_type"] == "linear"
    assert body["team_id"] == TEAM_ID
    # The team id is not reported as a Jira project key.
    assert body["project_key"] == ""
    assert body["api_token_set"] is True
    assert LINEAR_KEY not in resp.text

    get_resp = await client.get("/api/v1/projects/linear-cfg/tracker-config")
    assert get_resp.json()["team_id"] == TEAM_ID
    assert LINEAR_KEY not in get_resp.text

    async with TestSessionLocal() as session:
        row = (await session.execute(select(ProjectTrackerConfig))).scalar_one()
        assert row.api_token_encrypted
        assert row.api_token_encrypted != LINEAR_KEY
        assert decrypt_value(row.api_token_encrypted) == LINEAR_KEY


@pytest.mark.asyncio
async def test_linear_config_ignores_jira_fields_and_validates_team_id(
    client: AsyncClient,
) -> None:
    await _create_project(client, "linear-fields")

    resp = await client.patch(
        "/api/v1/projects/linear-fields/tracker-config",
        json={
            "tracker_type": "linear",
            "team_id": TEAM_ID,
            # A Jira key must not overwrite the team id stored in the same column,
            # and the SSRF-checked base_url is irrelevant for Linear.
            "project_key": "ENG",
            "base_url": "http://127.0.0.1/",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["team_id"] == TEAM_ID
    assert resp.json()["base_url"] == ""

    bad = await client.patch(
        "/api/v1/projects/linear-fields/tracker-config",
        json={"team_id": "not a team id!"},
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_switching_tracker_drops_the_other_vendors_credential(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_public_dns(monkeypatch)
    await _create_project(client, "linear-switch")
    jira = await client.patch(
        "/api/v1/projects/linear-switch/tracker-config",
        json={
            "enabled": True,
            "base_url": "https://example.atlassian.net",
            "auth_email": "alice@example.com",
            "api_token": "jira-token-1",
            "project_key": "ENG",
        },
    )
    assert jira.status_code == 200, jira.text
    assert jira.json()["api_token_set"] is True

    switched = await client.patch(
        "/api/v1/projects/linear-switch/tracker-config",
        json={"tracker_type": "linear", "team_id": TEAM_ID},
    )
    assert switched.status_code == 200, switched.text
    body = switched.json()
    # The Jira token would otherwise be sent to Linear as its API key.
    assert body["api_token_set"] is False
    assert body["team_id"] == TEAM_ID


@pytest.mark.asyncio
async def test_linear_config_is_owner_only(anon_client: AsyncClient) -> None:
    owner = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "linear-owner@example.com", "password": "Password123!", "name": "Owner"},
    )
    assert owner.status_code == 201, owner.text
    await anon_client.post("/api/v1/projects", json={"name": "LinGuard", "slug": "linear-rbac"})
    await anon_client.post("/api/v1/auth/logout")
    editor = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "linear-editor@example.com", "password": "Password123!", "name": "Ed"},
    )
    assert editor.status_code == 201, editor.text
    await add_member_by_slug("linear-rbac", "linear-editor@example.com", "editor")

    denied = await anon_client.patch(
        "/api/v1/projects/linear-rbac/tracker-config",
        json={"tracker_type": "linear", "team_id": TEAM_ID, "api_token": LINEAR_KEY},
    )
    assert denied.status_code == 403, denied.text


# ---------------------------------------------------------------------------
# Worker: create + sync
# ---------------------------------------------------------------------------


def _patch_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *args: object, **kwargs: object):  # noqa: ANN202
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(av.socket, "getaddrinfo", fake_getaddrinfo)


def _linear_config(project_id: uuid.UUID) -> ProjectTrackerConfig:
    return ProjectTrackerConfig(
        project_id=project_id,
        enabled=True,
        tracker_type="linear",
        project_key=TEAM_ID,
        api_token_encrypted=encrypt_value(LINEAR_KEY),
    )


def _jira_config(project_id: uuid.UUID) -> ProjectTrackerConfig:
    return ProjectTrackerConfig(
        project_id=project_id,
        enabled=True,
        tracker_type="jira",
        base_url="https://example.atlassian.net",
        project_key="ENG",
        auth_email="alice@example.com",
        api_token_encrypted=encrypt_value("jira-token"),
        issue_type="Task",
    )


ConfigFactory = Callable[[uuid.UUID], ProjectTrackerConfig]


async def _seed(
    session: AsyncSession, *, config: ConfigFactory = _linear_config
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Project + tracker config + branch + one ready_for_dev event.

    Returns ``(project_id, branch_id, event_id)``.
    """
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    session.add(
        Project(id=project_id, name="Lin", slug=f"lin-{project_id.hex[:8]}", description="")
    )
    session.add(config(project_id))
    session.add(PlanBranch(id=branch_id, project_id=project_id, name="feature"))
    await session.flush()
    event_type_id = uuid.uuid4()
    session.add(
        EventType(
            id=event_type_id,
            project_id=project_id,
            branch_id=branch_id,
            name="lin-type",
            display_name="Lin Type",
        )
    )
    await session.flush()
    event_id = uuid.uuid4()
    session.add(
        Event(
            id=event_id,
            project_id=project_id,
            branch_id=branch_id,
            event_type_id=event_type_id,
            name="signup",
            status=EventStatus.ready_for_dev.value,
        )
    )
    await session.commit()
    return project_id, branch_id, event_id


class _FakeLinear:
    """Records GraphQL calls and answers by operation name."""

    def __init__(self, *, existing: dict[str, str] | None = None, state: str = "started") -> None:
        self.calls: list[tuple[str, dict[str, object], dict[str, str]]] = []
        self.existing = existing
        self.state = state

    def __call__(self, url, body, headers=None):  # noqa: ANN001, ANN204
        self.calls.append((url, body, dict(headers or {})))
        query = str(body["query"])
        if "FindIssue" in query:
            nodes = [self.existing] if self.existing else []
            return {"data": {"issues": {"nodes": nodes}}}
        if "IssueCreate" in query:
            return {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "lin-issue-uuid",
                            "identifier": "ENG-7",
                            "url": "https://linear.app/acme/issue/ENG-7",
                        },
                    }
                }
            }
        if "IssueState" in query:
            return {"data": {"issue": {"state": {"type": self.state}}}}
        if "CommentCreate" in query:
            return {"data": {"commentCreate": {"success": True}}}
        return {"errors": [{"message": "unexpected operation"}]}

    def operations(self) -> list[str]:
        names = []
        for _url, body, _headers in self.calls:
            query = str(body["query"])
            for name in ("FindIssue", "IssueCreate", "IssueState", "CommentCreate"):
                if name in query:
                    names.append(name)
        return names


@pytest.mark.asyncio
async def test_merge_opens_a_linear_issue_with_the_branch_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLinear()
    monkeypatch.setattr(impl_tasks, "_post_json", fake)
    async with TestSessionLocal() as session:
        project_id, branch_id, event_id = await _seed(session)

    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(
            session, str(project_id), str(branch_id), [str(event_id)], "Implement signup"
        )

    assert fake.operations() == ["FindIssue", "IssueCreate"]
    url, body, headers = fake.calls[1]
    assert url == tracker_clients.LINEAR_GRAPHQL_URL
    # Same auth as the Linear alert destination: the raw key.
    assert headers["Authorization"] == LINEAR_KEY
    issue_input = body["variables"]["input"]  # type: ignore[index]
    assert issue_input["teamId"] == TEAM_ID
    assert issue_input["title"] == "Implement signup"
    assert f"tripl-branch-{branch_id.hex}" in issue_input["description"]
    assert "- signup" in issue_input["description"]

    async with TestSessionLocal() as session:
        ticket = (await session.execute(select(ImplementationTicket))).scalar_one()
    assert ticket.tracker_type == "linear"
    assert ticket.external_id == "lin-issue-uuid"
    assert ticket.external_key == "ENG-7"
    assert ticket.external_url == "https://linear.app/acme/issue/ENG-7"
    assert ticket.event_ids == [str(event_id)]


@pytest.mark.asyncio
async def test_redelivered_merge_adopts_the_existing_linear_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLinear(
        existing={
            "id": "already-there",
            "identifier": "ENG-3",
            "url": "https://linear.app/acme/issue/ENG-3",
        }
    )
    monkeypatch.setattr(impl_tasks, "_post_json", fake)
    async with TestSessionLocal() as session:
        project_id, branch_id, event_id = await _seed(session)

    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(
            session, str(project_id), str(branch_id), [str(event_id)], "Implement signup"
        )

    assert fake.operations() == ["FindIssue"]
    async with TestSessionLocal() as session:
        ticket = (await session.execute(select(ImplementationTicket))).scalar_one()
    assert ticket.external_key == "ENG-3"


@pytest.mark.asyncio
async def test_linear_graphql_errors_do_not_create_a_ticket_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(url, body, headers=None):  # noqa: ANN001, ANN202
        return {"errors": [{"message": "Entity not found: Team"}]}

    monkeypatch.setattr(impl_tasks, "_post_json", failing)
    async with TestSessionLocal() as session:
        project_id, branch_id, event_id = await _seed(session)

    with pytest.raises(tracker_clients.LinearGraphQLError):
        async with TestSessionLocal() as session:
            await impl_tasks._create_ticket(
                session, str(project_id), str(branch_id), [str(event_id)], "x"
            )
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ImplementationTicket))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "issue_create",
    [
        # Linear answered 200 without errors but did not create the issue.
        {"success": False, "issue": None},
        # "Success", but no issue to point at.
        {"success": True, "issue": None},
        {"success": True, "issue": {"id": "lin-issue-uuid", "url": "https://x"}},
        {"success": True, "issue": {"identifier": "ENG-7", "url": "https://x"}},
        # ``success`` missing altogether is not a success.
        {"issue": {"id": "lin-issue-uuid", "identifier": "ENG-7", "url": "https://x"}},
    ],
)
async def test_linear_create_without_success_persists_nothing(
    monkeypatch: pytest.MonkeyPatch, issue_create: dict[str, object]
) -> None:
    def unsuccessful(url, body, headers=None):  # noqa: ANN001, ANN202
        if "FindIssue" in str(body["query"]):
            return {"data": {"issues": {"nodes": []}}}
        return {"data": {"issueCreate": issue_create}}

    monkeypatch.setattr(impl_tasks, "_post_json", unsuccessful)
    async with TestSessionLocal() as session:
        project_id, branch_id, event_id = await _seed(session)

    with pytest.raises(tracker_clients.LinearGraphQLError):
        async with TestSessionLocal() as session:
            await impl_tasks._create_ticket(
                session, str(project_id), str(branch_id), [str(event_id)], "x"
            )
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ImplementationTicket))).scalars().all() == []


def test_linear_client_create_requires_success_and_ids() -> None:
    def refused(url, body, headers=None):  # noqa: ANN001, ANN202
        return {"data": {"issueCreate": {"success": False, "issue": None}}}

    with pytest.raises(tracker_clients.LinearGraphQLError):
        tracker_clients.create_linear_issue(
            refused, api_key="k", team_id="t", title="x", description="y"
        )


async def _seed_open_linear_ticket(
    config: ConfigFactory = _linear_config,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A Linear ticket covering one ready_for_dev event; ``(ticket_id, event_id)``."""
    async with TestSessionLocal() as session:
        project_id, branch_id, event_id = await _seed(session, config=config)
        ticket = ImplementationTicket(
            project_id=project_id,
            branch_id=branch_id,
            tracker_type="linear",
            external_id="lin-issue-uuid",
            external_key="ENG-7",
            external_url="https://linear.app/acme/issue/ENG-7",
            status="open",
            summary="Implement signup",
            event_ids=[str(event_id)],
        )
        session.add(ticket)
        await session.commit()
        return ticket.id, event_id


@pytest.mark.asyncio
async def test_sync_maps_linear_completed_to_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLinear(state="completed")
    monkeypatch.setattr(impl_tasks, "_post_json", fake)
    ticket_id, event_id = await _seed_open_linear_ticket()

    async with TestSessionLocal() as session:
        await impl_tasks._sync_tickets(session)

    assert fake.operations() == ["IssueState"]
    assert fake.calls[0][1]["variables"] == {"id": "lin-issue-uuid"}
    async with TestSessionLocal() as session:
        ticket = await session.get(ImplementationTicket, ticket_id)
        event = await session.get(Event, event_id)
    assert ticket is not None and ticket.status == "closed"
    assert ticket.closed_at is not None
    assert event is not None and event.status == EventStatus.implemented.value


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["started", "unstarted", "canceled", "backlog"])
async def test_sync_leaves_non_completed_linear_issues_open(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    fake = _FakeLinear(state=state)
    monkeypatch.setattr(impl_tasks, "_post_json", fake)
    ticket_id, event_id = await _seed_open_linear_ticket()

    async with TestSessionLocal() as session:
        await impl_tasks._sync_tickets(session)

    async with TestSessionLocal() as session:
        ticket = await session.get(ImplementationTicket, ticket_id)
        event = await session.get(Event, event_id)
    assert ticket is not None and ticket.status == "open"
    assert event is not None and event.status == EventStatus.ready_for_dev.value


@pytest.mark.asyncio
async def test_sync_skips_a_linear_ticket_once_the_project_moved_to_jira(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Jira token must never be sent to Linear to poll an old ticket."""
    fake = _FakeLinear(state="completed")
    monkeypatch.setattr(impl_tasks, "_post_json", fake)
    ticket_id, _event_id = await _seed_open_linear_ticket(config=_jira_config)

    async with TestSessionLocal() as session:
        await impl_tasks._sync_tickets(session)

    assert fake.calls == []
    async with TestSessionLocal() as session:
        ticket = await session.get(ImplementationTicket, ticket_id)
    assert ticket is not None and ticket.status == "open"


# ---------------------------------------------------------------------------
# add_ticket_comment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_ticket_comment_posts_on_linear(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLinear()
    monkeypatch.setattr(alerts_channels, "_post_json", fake)
    ticket_id, _event_id = await _seed_open_linear_ticket()

    async with TestSessionLocal() as session:
        ticket = await session.get(ImplementationTicket, ticket_id)
        assert ticket is not None
        ok = await implementation_ticket_service.add_ticket_comment_async(
            session, ticket, "Seen in production data at 2026-09-27 10:00 UTC; marked live."
        )

    assert ok is True
    assert fake.operations() == ["CommentCreate"]
    _url, body, headers = fake.calls[0]
    assert headers["Authorization"] == LINEAR_KEY
    assert body["variables"] == {
        "input": {
            "issueId": "lin-issue-uuid",
            "body": "Seen in production data at 2026-09-27 10:00 UTC; marked live.",
        }
    }


@pytest.mark.asyncio
async def test_add_ticket_comment_posts_on_jira(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_public_dns(monkeypatch)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_post(url, body, headers=None):  # noqa: ANN001, ANN202
        calls.append((url, body))
        return {"id": "20001"}

    monkeypatch.setattr(alerts_channels, "_post_json", fake_post)
    async with TestSessionLocal() as session:
        project_id, branch_id, event_id = await _seed(session, config=_jira_config)
        ticket = ImplementationTicket(
            project_id=project_id,
            branch_id=branch_id,
            tracker_type="jira",
            external_id="10001",
            external_key="ENG-1",
            status="open",
            summary="s",
            event_ids=[str(event_id)],
        )
        session.add(ticket)
        await session.commit()
        ok = await implementation_ticket_service.add_ticket_comment_async(
            session, ticket, "Seen in production data; marked live."
        )

    assert ok is True
    assert calls[0][0] == "https://example.atlassian.net/rest/api/3/issue/ENG-1/comment"
    assert "body" in calls[0][1]


@pytest.mark.asyncio
async def test_add_ticket_comment_skips_a_ticket_from_the_other_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLinear()
    monkeypatch.setattr(alerts_channels, "_post_json", fake)
    ticket_id, _event_id = await _seed_open_linear_ticket(config=_jira_config)

    async with TestSessionLocal() as session:
        ticket = await session.get(ImplementationTicket, ticket_id)
        assert ticket is not None
        ok = await implementation_ticket_service.add_ticket_comment_async(session, ticket, "hi")

    assert ok is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_add_ticket_comment_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url, body, headers=None):  # noqa: ANN001, ANN202
        raise ValueError("HTTP 500 from https://api.linear.app")

    monkeypatch.setattr(alerts_channels, "_post_json", boom)
    ticket_id, _event_id = await _seed_open_linear_ticket()

    async with TestSessionLocal() as session:
        ticket = await session.get(ImplementationTicket, ticket_id)
        assert ticket is not None
        ok = await implementation_ticket_service.add_ticket_comment_async(session, ticket, "hi")

    assert ok is False


def test_linear_client_raises_on_graphql_errors() -> None:
    def errors(url, body, headers=None):  # noqa: ANN001, ANN202
        return {"errors": [{"message": "Authentication required"}]}

    with pytest.raises(tracker_clients.LinearGraphQLError, match="Authentication required"):
        tracker_clients.get_linear_issue_state_type(errors, api_key="k", issue_id="x")


@pytest.mark.asyncio
async def test_seen_in_data_task_comments_on_the_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The task the scan's auto-live publishes (``metrics.collect``) lands one comment."""
    fake = _FakeLinear()
    monkeypatch.setattr(alerts_channels, "_post_json", fake)
    ticket_id, event_id = await _seed_open_linear_ticket()

    async with TestSessionLocal() as session:
        ok = await impl_tasks._comment_seen_in_data(
            session, str(ticket_id), [str(event_id)], "2026-09-27T10:05:00+00:00"
        )

    assert ok is True
    assert fake.operations() == ["CommentCreate"]
    body = fake.calls[0][1]["variables"]["input"]["body"]  # type: ignore[index]
    assert body.startswith("Seen in production data at 2026-09-27 10:05 UTC; marked live.")
    assert "signup" in body


def test_seen_in_data_comment_text() -> None:
    assert impl_tasks.seen_in_data_comment_text("2026-09-27T10:05:00+00:00", []) == (
        "Seen in production data at 2026-09-27 10:05 UTC; marked live."
    )
