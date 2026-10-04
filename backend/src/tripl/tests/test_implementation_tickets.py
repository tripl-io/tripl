import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import tripl.alerting_validation as av
from tripl.crypto import encrypt_value
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import implementation_tickets as impl_tasks


def _patch_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve any tracker host to a public IP so the SSRF guard passes without
    real DNS (hermetic + no network dependency)."""

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(av.socket, "getaddrinfo", fake_getaddrinfo)


def _no_existing_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the pre-create lookup with "nothing there", without a network call.

    _find_existing_issue swallows a failed search on purpose, so leaving this
    unpatched would let every create test attempt a real HTTP request and still
    pass — slowly, and for the wrong reason.
    """

    def nothing(get_json, *, base_url, auth_email, api_token, label):  # noqa: ANN001, ANN202
        return (None, None)

    monkeypatch.setattr(impl_tasks, "_find_jira_issue_by_label", nothing)


def _tracker_config(project_id: uuid.UUID) -> ProjectTrackerConfig:
    return ProjectTrackerConfig(
        project_id=project_id,
        enabled=True,
        tracker_type="jira",
        base_url="https://example.atlassian.net",
        project_key="ENG",
        auth_email="alice@example.com",
        api_token_encrypted=encrypt_value("api-token-1"),
        issue_type="Task",
    )


async def _seed_event_parents(
    session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID
) -> uuid.UUID:
    """Create the PlanBranch + EventType that ``Event`` rows FK to.

    ``Event.branch_id`` and ``Event.event_type_id`` are non-null FKs; production
    Postgres rejects the fabricated ids these tests used, so seed real parents
    and flush them before their child events. Returns the event_type_id to use.
    """
    session.add(PlanBranch(id=branch_id, project_id=project_id, name="feature"))
    await session.flush()
    event_type_id = uuid.uuid4()
    session.add(
        EventType(
            id=event_type_id,
            project_id=project_id,
            branch_id=branch_id,
            name="impl-tickets",
            display_name="Impl Tickets",
        )
    )
    await session.flush()
    return event_type_id


def _event(
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_id: uuid.UUID,
    name: str,
    status: EventStatus,
) -> Event:
    return Event(
        id=uuid.uuid4(),
        project_id=project_id,
        branch_id=branch_id,
        event_type_id=event_type_id,
        name=name,
        status=status.value,
    )


@pytest.mark.asyncio
async def test_create_implementation_ticket_creates_row_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_public_dns(monkeypatch)
    _no_existing_issue(monkeypatch)
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    event = None
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Tick", slug="tick", description=""))
        session.add(_tracker_config(project_id))
        event_type_id = await _seed_event_parents(session, project_id, branch_id)
        event = _event(
            project_id, branch_id, event_type_id, "purchase:success", EventStatus.ready_for_dev
        )
        session.add(event)
        await session.commit()
    event_id = str(event.id)

    calls: dict[str, object] = {}

    def fake_post_json(url, body, headers=None):  # noqa: ANN001, ANN202
        calls["url"] = url
        calls["body"] = body
        return {"id": "10001", "key": "ENG-1"}

    monkeypatch.setattr(impl_tasks, "_post_json", fake_post_json)

    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(
            session,
            str(project_id),
            str(branch_id),
            [event_id],
            "Implement 1 event(s) from branch 'feature'",
        )

    assert calls["url"] == "https://example.atlassian.net/rest/api/3/issue"
    async with TestSessionLocal() as session:
        tickets = (await session.execute(select(ImplementationTicket))).scalars().all()
        assert len(tickets) == 1
        ticket = tickets[0]
        assert ticket.external_id == "10001"
        assert ticket.external_key == "ENG-1"
        assert ticket.external_url == "https://example.atlassian.net/browse/ENG-1"
        assert ticket.status == "open"
        assert ticket.tracker_type == "jira"
        assert ticket.event_ids == [event_id]
        assert ticket.branch_id == branch_id

    # Idempotency: a second call for the same branch must NOT create a duplicate.
    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(
            session, str(project_id), str(branch_id), [event_id], "again"
        )
    async with TestSessionLocal() as session:
        remaining = (await session.execute(select(ImplementationTicket))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_implementation_tickets_reject_a_second_row_for_the_same_branch() -> None:
    """The database, not the check-then-act above it, is what holds one per branch.

    Two deliveries that enqueue concurrently both pass the existence check, so
    without ``uq_implementation_ticket_branch`` both rows land and the branch
    shows two tickets.
    """
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Twice", slug="twice", description=""))
        await _seed_event_parents(session, project_id, branch_id)
        session.add(
            ImplementationTicket(
                project_id=project_id,
                branch_id=branch_id,
                tracker_type="jira",
                external_id="10001",
                external_key="ENG-1",
                external_url="https://example.atlassian.net/browse/ENG-1",
                status="open",
                summary="first",
                event_ids=[],
            )
        )
        await session.commit()

    async with TestSessionLocal() as session:
        session.add(
            ImplementationTicket(
                project_id=project_id,
                branch_id=branch_id,
                tracker_type="jira",
                external_id="10002",
                external_key="ENG-2",
                external_url="https://example.atlassian.net/browse/ENG-2",
                status="open",
                summary="second",
                event_ids=[],
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_create_implementation_ticket_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_public_dns(monkeypatch)
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Off", slug="off", description=""))
        config = _tracker_config(project_id)
        config.enabled = False
        session.add(config)
        await session.commit()

    def fake_post_json(url, body, headers=None):  # noqa: ANN001, ANN202
        raise AssertionError("must not call the tracker when disabled")

    monkeypatch.setattr(impl_tasks, "_post_json", fake_post_json)

    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(
            session, str(project_id), str(branch_id), [str(uuid.uuid4())], "s"
        )
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ImplementationTicket))).scalars().all() == []


@pytest.mark.asyncio
async def test_sync_implementation_tickets_closes_and_marks_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_public_dns(monkeypatch)
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    ticket_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Sync", slug="sync", description=""))
        session.add(_tracker_config(project_id))
        event_type_id = await _seed_event_parents(session, project_id, branch_id)
        ready = _event(project_id, branch_id, event_type_id, "ready-evt", EventStatus.ready_for_dev)
        live = _event(project_id, branch_id, event_type_id, "live-evt", EventStatus.live)
        session.add_all([ready, live])
        await session.flush()
        ready_id, live_id = ready.id, live.id
        session.add(
            ImplementationTicket(
                id=ticket_id,
                project_id=project_id,
                branch_id=branch_id,
                tracker_type="jira",
                external_id="10001",
                external_key="ENG-1",
                external_url="https://example.atlassian.net/browse/ENG-1",
                status="open",
                summary="s",
                event_ids=[str(ready_id), str(live_id)],
            )
        )
        await session.commit()

    def fake_status(get_json, *, base_url, auth_email, api_token, issue_key):  # noqa: ANN001, ANN202
        return "done"

    monkeypatch.setattr(impl_tasks, "_get_jira_issue_status", fake_status)

    async with TestSessionLocal() as session:
        await impl_tasks._sync_tickets(session)

    async with TestSessionLocal() as session:
        ticket = (
            await session.execute(
                select(ImplementationTicket).where(ImplementationTicket.id == ticket_id)
            )
        ).scalar_one()
        assert ticket.status == "closed"
        assert ticket.closed_at is not None

        ready_after = await session.get(Event, ready_id)
        live_after = await session.get(Event, live_id)
        # A pre-implemented event is promoted...
        assert ready_after.status == EventStatus.implemented.value
        # ...but a higher-ranked live event is NEVER downgraded.
        assert live_after.status == EventStatus.live.value


@pytest.mark.asyncio
async def test_a_redelivered_create_adopts_the_issue_it_already_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE kill window: the row is committed after the Jira POST, and the worker
    runs acks_late with a SIGKILL time limit, so a worker killed in between is
    redelivered — finds no row — and used to open a second issue.

    Jira's create takes no idempotency key, so nothing local can close that: the
    only durable record of the first attempt is the issue, and the only way to
    recognise it is to have labelled it with the branch.
    """
    _patch_public_dns(monkeypatch)
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Redeliver", slug="redeliver", description=""))
        session.add(_tracker_config(project_id))
        await _seed_event_parents(session, project_id, branch_id)
        await session.commit()

    searched: dict[str, str] = {}

    def found(get_json, *, base_url, auth_email, api_token, label):  # noqa: ANN001, ANN202
        searched["label"] = label
        return ("10001", "ENG-1")

    def must_not_post(url, body, headers=None):  # noqa: ANN001, ANN202
        raise AssertionError("a second issue must not be created for this branch")

    monkeypatch.setattr(impl_tasks, "_find_jira_issue_by_label", found)
    monkeypatch.setattr(impl_tasks, "_post_json", must_not_post)

    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(
            session, str(project_id), str(branch_id), [], "Implement branch"
        )

    # Searched for THIS branch, and adopted what it found rather than creating.
    assert searched["label"] == f"tripl-branch-{branch_id.hex}"
    async with TestSessionLocal() as session:
        tickets = (await session.execute(select(ImplementationTicket))).scalars().all()
        assert len(tickets) == 1
        assert tickets[0].external_key == "ENG-1"
        assert tickets[0].external_id == "10001"


@pytest.mark.asyncio
async def test_a_created_issue_carries_the_label_that_makes_it_findable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adopt path above is only reachable if the create wrote the marker."""
    _patch_public_dns(monkeypatch)
    _no_existing_issue(monkeypatch)
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Label", slug="label", description=""))
        session.add(_tracker_config(project_id))
        await _seed_event_parents(session, project_id, branch_id)
        await session.commit()

    sent: dict[str, object] = {}

    def fake_post_json(url, body, headers=None):  # noqa: ANN001, ANN202
        sent["body"] = body
        return {"id": "10002", "key": "ENG-2"}

    monkeypatch.setattr(impl_tasks, "_post_json", fake_post_json)

    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(
            session, str(project_id), str(branch_id), [], "Implement branch"
        )

    fields = sent["body"]["fields"]  # type: ignore[index]
    assert fields["labels"] == [f"tripl-branch-{branch_id.hex}"]


@pytest.mark.asyncio
async def test_one_failing_ticket_does_not_strand_the_rest_of_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-ticket handler used to end the sweep instead of containing it.

    ``rollback()`` expires every loaded instance — ``expire_on_commit=False``
    covers commits, not rollbacks — so after the first failure the next
    iteration's attribute access became a lazy refresh, which asyncio SQLAlchemy
    raises MissingGreenlet for. Ten tickets and a 500 on the first meant nine
    never polled, that run and every run after it.
    """
    _patch_public_dns(monkeypatch)
    project_id = uuid.uuid4()
    branch_one, branch_two = uuid.uuid4(), uuid.uuid4()
    failing_id, healthy_id = uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Sweep", slug="sweep", description=""))
        session.add(_tracker_config(project_id))
        # Only the PlanBranch parents matter here — these tickets cover no
        # events, so no EventType is needed.
        session.add(PlanBranch(id=branch_one, project_id=project_id, name="sweep-one"))
        session.add(PlanBranch(id=branch_two, project_id=project_id, name="sweep-two"))
        await session.flush()
        for ticket_id, branch_id, key in (
            (failing_id, branch_one, "ENG-1"),
            (healthy_id, branch_two, "ENG-2"),
        ):
            session.add(
                ImplementationTicket(
                    id=ticket_id,
                    project_id=project_id,
                    branch_id=branch_id,
                    tracker_type="jira",
                    external_id=key,
                    external_key=key,
                    external_url=f"https://example.atlassian.net/browse/{key}",
                    status="open",
                    summary="s",
                    event_ids=[],
                )
            )
        await session.commit()

    def flaky_status(get_json, *, base_url, auth_email, api_token, issue_key):  # noqa: ANN001, ANN202
        if issue_key == "ENG-1":
            raise RuntimeError("Jira returned 500")
        return "done"

    monkeypatch.setattr(impl_tasks, "_get_jira_issue_status", flaky_status)

    async with TestSessionLocal() as session:
        # Must not raise: the failure belongs to one ticket.
        await impl_tasks._sync_tickets(session)

    async with TestSessionLocal() as session:
        failing = await session.get(ImplementationTicket, failing_id)
        healthy = await session.get(ImplementationTicket, healthy_id)
        assert failing is not None and healthy is not None
        # The one that failed is untouched, and the one behind it was still polled.
        assert failing.status == "open"
        assert healthy.status == "closed"


@pytest.mark.asyncio
async def test_sync_implementation_tickets_leaves_open_when_not_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_public_dns(monkeypatch)
    project_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    ticket_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Project(id=project_id, name="Open", slug="open", description=""))
        session.add(_tracker_config(project_id))
        event_type_id = await _seed_event_parents(session, project_id, branch_id)
        evt = _event(project_id, branch_id, event_type_id, "still-open", EventStatus.ready_for_dev)
        session.add(evt)
        await session.flush()
        evt_id = evt.id
        session.add(
            ImplementationTicket(
                id=ticket_id,
                project_id=project_id,
                branch_id=branch_id,
                tracker_type="jira",
                external_id="10001",
                external_key="ENG-2",
                external_url="https://example.atlassian.net/browse/ENG-2",
                status="open",
                summary="s",
                event_ids=[str(evt_id)],
            )
        )
        await session.commit()

    def fake_status(get_json, *, base_url, auth_email, api_token, issue_key):  # noqa: ANN001, ANN202
        return "indeterminate"

    monkeypatch.setattr(impl_tasks, "_get_jira_issue_status", fake_status)

    async with TestSessionLocal() as session:
        await impl_tasks._sync_tickets(session)

    async with TestSessionLocal() as session:
        ticket = (
            await session.execute(
                select(ImplementationTicket).where(ImplementationTicket.id == ticket_id)
            )
        ).scalar_one()
        assert ticket.status == "open"
        assert ticket.closed_at is None
        evt_after = await session.get(Event, evt_id)
        assert evt_after.status == EventStatus.ready_for_dev.value
