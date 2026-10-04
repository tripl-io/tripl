"""Backend halves of the round-2 frontend review leftovers (B1-B9).

Each block names the finding it closes. They share one module because each is a
small contract the frontend now leans on: who may delete a comment,
what an update does to a webhook's stored secret, a blank chat id on a
non-Telegram destination, the photo limit a browser can read,
a branch event's main twin, replaying unsaved rule edits,
testing a connection before saving it, shadow-event paging and batch
triage, and the exact-name identity lookup.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from tripl.config import settings
from tripl.main import app
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_ACCEPTED,
    SHADOW_STATUS_DISMISSED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.models.user import User
from tripl.schemas.alerting import AlertDestinationCreate
from tripl.services import datasource_service
from tripl.services.event_photo_service import ensure_comment_deletable
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def editor_client(client: AsyncClient) -> AsyncGenerator[AsyncClient]:
    """A second signed-in user: a member of the owner ``client``'s organization.

    Since F20 PR4 there is no instance ``editor`` role to promote to; what the
    member may do inside a project is its project row, which the tests that need
    one grant with ``add_member_by_slug``.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as editor:
        registered = await editor.post(
            "/api/v1/auth/register",
            json={"email": "editor@example.com", "password": "Password123!", "name": "Ed"},
        )
        assert registered.status_code == 201, registered.text
        users = await client.get("/api/v1/users")
        target = next(u for u in users.json() if u["email"] == "editor@example.com")
        assert target["role"] == "member", target
        yield editor


async def _project_with_event(client: AsyncClient, slug: str) -> tuple[str, str]:
    """A project created by the owner, one type, one event.

    Editors only reach it once they are members; the comment tests grant that.
    """
    project = await client.post(
        "/api/v1/projects", json={"name": slug, "slug": slug, "description": ""}
    )
    assert project.status_code == 201, project.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    assert event_type.status_code == 201, event_type.text
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type.json()["id"], "name": "purchase:success"},
    )
    assert event.status_code == 201, event.text
    return event_type.json()["id"], event.json()["id"]


async def _project_id(slug: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
        return project_id


# --- B1 / EVT-29: comment delete is the author's or an owner's ---------------


async def test_an_editor_cannot_delete_another_users_event_comment(
    client: AsyncClient, editor_client: AsyncClient
) -> None:
    slug = "r2-comment-delete"
    _type_id, event_id = await _project_with_event(client, slug)
    await add_member_by_slug(slug, "editor@example.com", "editor")
    base = f"/api/v1/projects/{slug}/events/{event_id}/comments"

    owners = await client.post(base, json={"body": "owner's question"})
    assert owners.status_code == 201
    editors = await editor_client.post(base, json={"body": "editor's question"})
    assert editors.status_code == 201

    refused = await editor_client.delete(f"{base}/{owners.json()['id']}")
    assert refused.status_code == 403
    assert "author" in refused.json()["detail"]
    # Refused means untouched, and nothing was audited as deleted.
    async with TestSessionLocal() as session:
        assert await session.get(EventPhotoComment, uuid.UUID(owners.json()["id"])) is not None
        deletes = await session.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.action == "event_comment.delete")
        )
        assert deletes == 0

    # The author may delete their own, and an owner may delete anyone's.
    own = await editor_client.delete(f"{base}/{editors.json()['id']}")
    assert own.status_code == 204
    again = await editor_client.post(base, json={"body": "second try"})
    by_owner = await client.delete(f"{base}/{again.json()['id']}")
    assert by_owner.status_code == 204


async def test_an_editor_cannot_delete_another_users_branch_comment(
    client: AsyncClient, editor_client: AsyncClient
) -> None:
    """The branch review thread renders through the same CommentThread, which
    offers Delete to the author and owners only; the server now agrees."""
    slug = "r2-branch-comment-delete"
    await _project_with_event(client, slug)
    await add_member_by_slug(slug, "editor@example.com", "editor")
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "wip"})
    assert branch.status_code == 201, branch.text
    base = f"/api/v1/projects/{slug}/branches/{branch.json()['id']}/comments"

    owners = await client.post(base, json={"body": "owner's note"})
    assert owners.status_code == 201
    editors = await editor_client.post(base, json={"body": "editor's note"})
    assert editors.status_code == 201, editors.text

    refused = await editor_client.delete(f"{base}/{owners.json()['id']}")
    assert refused.status_code == 403
    own = await editor_client.delete(f"{base}/{editors.json()['id']}")
    assert own.status_code == 204
    by_owner = await client.delete(f"{base}/{owners.json()['id']}")
    assert by_owner.status_code == 204


async def test_comment_delete_rule_covers_orphaned_comments(
    client: AsyncClient, editor_client: AsyncClient
) -> None:
    """The photo threads share this check; a comment whose author was deleted is
    left to the project's owners (org owners/admins, F20 PR4)."""
    await _project_with_event(client, "orphan-comments")
    await add_member_by_slug("orphan-comments", "editor@example.com")
    async with TestSessionLocal() as session:
        project_id = await session.scalar(
            select(Project.id).where(Project.slug == "orphan-comments")
        )
        owner = await session.scalar(select(User).where(User.email == "test@example.com"))
        editor = await session.scalar(select(User).where(User.email == "editor@example.com"))
        assert project_id is not None and owner is not None and editor is not None
        orphan = EventPhotoComment(id=uuid.uuid4(), user_id=None, body="x")
        mine = EventPhotoComment(id=uuid.uuid4(), user_id=editor.id, body="y")

        await ensure_comment_deletable(session, orphan, owner, project_id)
        await ensure_comment_deletable(session, mine, editor, project_id)
        with pytest.raises(Exception) as refused:
            await ensure_comment_deletable(session, orphan, editor, project_id)
    assert getattr(refused.value, "status_code", None) == 403


# --- B2 / ALR-24 and B3 / ALR-1: webhook header secret and blank chat id ------


async def _webhook_destination(client: AsyncClient, slug: str) -> str:
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug, "description": ""})
    created = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "webhook",
            "name": "Ops",
            "target_url": "https://example.com/hook",
            "webhook_header_name": "Authorization",
            "webhook_header_value": "Bearer secret",
            # A form holding every channel's inputs sends this for a webhook;
            # it is "not given", not a missing Telegram chat id.
            "chat_id": "",
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_removing_the_webhook_header_clears_the_stored_secret(client: AsyncClient) -> None:
    slug = "r2-webhook-header"
    destination_id = await _webhook_destination(client, slug)
    url = f"/api/v1/projects/{slug}/alert-destinations/{destination_id}"

    # A rename with no value keeps the stored secret.
    renamed = await client.patch(url, json={"webhook_header_name": "X-Token"})
    assert renamed.status_code == 200, renamed.text
    async with TestSessionLocal() as session:
        row = await session.get(AlertDestination, uuid.UUID(destination_id))
        assert row is not None
        assert row.webhook_header_name == "X-Token"
        assert row.webhook_header_value_encrypted is not None

    removed = await client.patch(
        url, json={"webhook_header_name": None, "webhook_header_value": None}
    )
    assert removed.status_code == 200, removed.text
    async with TestSessionLocal() as session:
        row = await session.get(AlertDestination, uuid.UUID(destination_id))
        assert row is not None
        assert row.webhook_header_name is None
        assert row.webhook_header_value_encrypted is None


def test_a_blank_chat_id_is_not_given_but_telegram_still_needs_one() -> None:
    slack = AlertDestinationCreate.model_validate(
        {
            "type": "webhook",
            "name": "W",
            "target_url": "https://example.com/hook",
            "chat_id": "  ",
        }
    )
    assert slack.chat_id is None

    with pytest.raises(ValidationError, match="Telegram chat_id is required"):
        AlertDestinationCreate.model_validate(
            {
                "type": "telegram",
                "name": "T",
                "bot_token": "123456:ABCdefGhIJKlmNoPQRstuVWXyz",
                "chat_id": "",
            }
        )


# --- B4 / EVT-28: the photo limit is readable -----------------------------


async def test_every_signed_in_user_can_read_the_photo_limit(
    client: AsyncClient, editor_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "photo_max_size_mb", 25)
    for reader in (client, editor_client):
        resp = await reader.get("/api/v1/settings/photo-limits")
        assert resp.status_code == 200, resp.text
        # With the content types the upload takes (F20 PR11).
        assert resp.json() == {
            "photo_max_size_mb": 25,
            "photo_allowed_mime": ["image/jpeg", "image/png", "image/gif", "image/webp"],
        }

    # The rest of /settings stays owner-only.
    assert (await editor_client.get("/api/v1/settings")).status_code == 403


async def test_the_photo_limit_needs_a_session() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anonymous:
        assert (await anonymous.get("/api/v1/settings/photo-limits")).status_code == 401


# --- B5 / EVT-42: a branch event names its main twin ----------------------


async def test_a_branch_copy_names_its_main_twin(client: AsyncClient) -> None:
    slug = "r2-main-twin"
    _type_id, main_event_id = await _project_with_event(client, slug)
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]

    listed = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    copy = next(item for item in listed.json()["items"] if item["name"] == "purchase:success")
    assert copy["id"] != main_event_id

    on_branch = await client.get(f"/api/v1/projects/{slug}/events/{copy['id']}?branch={branch_id}")
    assert on_branch.status_code == 200
    assert on_branch.json()["main_event_id"] == main_event_id

    on_main = await client.get(f"/api/v1/projects/{slug}/events/{main_event_id}")
    assert on_main.json()["main_event_id"] is None


# --- B6 / ALR-12(a): replaying unsaved rule edits ---------------------------


async def test_simulate_replays_a_draft_without_writing_it(client: AsyncClient) -> None:
    slug = "r2-simulate-draft"
    destination_id = await _webhook_destination(client, slug)
    rule = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={"name": "Rule", "cooldown_minutes": 60, "min_percent_delta": 10},
    )
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["id"]
    url = f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate"

    saved = await client.post(f"{url}?days=7")
    assert saved.status_code == 200, saved.text
    assert saved.json()["cooldown_minutes_used"] == 60

    drafted = await client.post(
        f"{url}?days=7", json={"cooldown_minutes": 5, "min_percent_delta": 250}
    )
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["cooldown_minutes_used"] == 5
    assert body["cooldown_minutes_saved"] == 60
    assert body["min_percent_delta_used"] == 250
    assert body["min_percent_delta_saved"] == 10
    # An override still wins over the draft for its one knob.
    overridden = await client.post(
        f"{url}?days=7&cooldown_minutes_override=0", json={"cooldown_minutes": 5}
    )
    assert overridden.json()["cooldown_minutes_used"] == 0

    async with TestSessionLocal() as session:
        stored = await session.get(AlertRule, uuid.UUID(rule_id))
        assert stored is not None
        assert stored.cooldown_minutes == 60
        assert stored.min_percent_delta == 10


async def test_simulate_checks_a_draft_as_save_would(client: AsyncClient) -> None:
    slug = "r2-simulate-invalid"
    destination_id = await _webhook_destination(client, slug)
    rule = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={"name": "Rule", "notify_on_drop": False},
    )
    rule_id = rule.json()["id"]
    url = f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate"

    # Merged with the stored rule, this turns both directions off.
    refused = await client.post(f"{url}?days=7", json={"notify_on_spike": False})
    assert refused.status_code == 422
    assert "direction" in refused.json()["detail"]


# --- B7 / DATA-30: test a connection before saving it -----------------------


async def test_an_unsaved_connection_is_tested_and_nothing_is_stored(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[DataSource] = []

    def fake_probe(ds: DataSource) -> tuple[bool, str]:
        probed.append(ds)
        return False, "Connection test failed: authentication was rejected — check the credentials."

    monkeypatch.setattr(datasource_service, "_run_adapter_test", fake_probe)
    resp = await client.post(
        "/api/v1/data-sources/test",
        json={
            "db_type": "clickhouse",
            "host": "ch.internal",
            "port": 8123,
            "database_name": "analytics",
            "username": "reader",
            "password": "typed-here",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is False
    assert "authentication" in resp.json()["message"]
    assert len(probed) == 1
    assert probed[0].host == "ch.internal"
    assert probed[0].id is None  # a transient row, never flushed

    async with TestSessionLocal() as session:
        assert await session.scalar(select(func.count(DataSource.id))) == 0


async def test_an_unsaved_connection_test_is_validated_and_owner_only(
    client: AsyncClient, editor_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(datasource_service, "_run_adapter_test", lambda _ds: (True, "ok"))
    body = {"db_type": "clickhouse", "host": "ch.internal", "database_name": "d"}

    # The create body's host check applies.
    bad_host = await client.post(
        "/api/v1/data-sources/test", json={**body, "host": "http://ch.internal/x"}
    )
    assert bad_host.status_code == 422
    synthetic = await client.post(
        "/api/v1/data-sources/test", json={**body, "db_type": "synthetic"}
    )
    assert synthetic.status_code == 422
    # The create gate: owners only.
    assert (await editor_client.post("/api/v1/data-sources/test", json=body)).status_code == 403


# --- B8 / DATA-39: paging and batch triage for shadow events ----------------


async def _shadow_setup(client: AsyncClient, slug: str, count: int) -> tuple[str, list[uuid.UUID]]:
    type_id, _event_id = await _project_with_event(client, slug)
    project_id = await _project_id(slug)
    async with TestSessionLocal() as session:
        source = DataSource(
            name=f"wh-{slug}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
        )
        session.add(source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=source.id,
            name="scan",
            base_query="SELECT 1",
        )
        session.add(config)
        await session.flush()
        ids: list[uuid.UUID] = []
        for index in range(count):
            candidate = ShadowEventCandidate(
                project_id=project_id,
                scan_config_id=config.id,
                event_type_id=uuid.UUID(type_id),
                event_name=f"shadow_{index}",
                # Ties on purpose: paging must still be stable.
                observed_count=10,
                first_seen_at=NOW - timedelta(days=1),
                last_seen_at=NOW,
                status=SHADOW_STATUS_NEW,
            )
            session.add(candidate)
            await session.flush()
            ids.append(candidate.id)
        await session.commit()
    return type_id, ids


async def test_shadow_events_page_by_offset_without_repeats(client: AsyncClient) -> None:
    slug = "r2-shadow-paging"
    _type_id, ids = await _shadow_setup(client, slug, 5)
    base = f"/api/v1/projects/{slug}/reconciliation/shadow-events?status=new&limit=2"

    seen: list[str] = []
    for offset in (0, 2, 4):
        page = await client.get(f"{base}&offset={offset}")
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 5
        seen.extend(item["id"] for item in page.json()["items"])
    assert sorted(seen) == sorted(str(i) for i in ids)
    assert len(seen) == len(set(seen))


async def test_batch_dismiss_reports_each_row_and_audits_each(client: AsyncClient) -> None:
    slug = "r2-shadow-batch"
    _type_id, ids = await _shadow_setup(client, slug, 3)
    url = f"/api/v1/projects/{slug}/reconciliation/shadow-events"
    # One row is already resolved, so the batch refuses it and goes on.
    already = await client.post(f"{url}/{ids[1]}/dismiss")
    assert already.status_code == 200

    resp = await client.post(
        f"{url}/batch",
        json={"action": "dismiss", "items": [{"candidate_id": str(i)} for i in ids]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["succeeded"], body["failed"]) == (2, 1)
    by_id = {row["candidate_id"]: row for row in body["results"]}
    assert by_id[str(ids[0])]["ok"] is True
    assert by_id[str(ids[1])]["ok"] is False
    assert by_id[str(ids[1])]["error_status"] == 409
    assert by_id[str(ids[2])]["status"] == SHADOW_STATUS_DISMISSED

    async with TestSessionLocal() as session:
        statuses = {
            row.id: row.status
            for row in (await session.execute(select(ShadowEventCandidate))).scalars()
        }
        assert set(statuses.values()) == {SHADOW_STATUS_DISMISSED}
        audited = await session.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.action == "shadow_event.dismiss")
        )
        # The single dismiss above, and the two the batch did.
        assert audited == 3


async def test_batch_accept_creates_the_events(client: AsyncClient) -> None:
    slug = "r2-shadow-accept"
    _type_id, ids = await _shadow_setup(client, slug, 2)
    resp = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/batch",
        json={"action": "accept", "items": [{"candidate_id": str(i)} for i in ids]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["succeeded"] == 2
    async with TestSessionLocal() as session:
        names = set(
            (
                await session.execute(
                    select(Event.source_name).where(Event.source_name.like("shadow_%"))
                )
            ).scalars()
        )
        assert names == {"shadow_0", "shadow_1"}
        statuses = set((await session.execute(select(ShadowEventCandidate.status))).scalars())
        assert statuses == {SHADOW_STATUS_ACCEPTED}


async def test_batch_refuses_a_repeated_candidate(client: AsyncClient) -> None:
    slug = "r2-shadow-repeat"
    _type_id, ids = await _shadow_setup(client, slug, 1)
    repeated = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/batch",
        json={
            "action": "dismiss",
            "items": [{"candidate_id": str(ids[0])}, {"candidate_id": str(ids[0])}],
        },
    )
    assert repeated.status_code == 422


# --- B9 / EVT-37: exact-name identity lookup --------------------------------


async def test_by_names_answers_with_the_identity_rule_create_enforces(
    client: AsyncClient,
) -> None:
    slug = "r2-by-names"
    type_id, event_id = await _project_with_event(client, slug)
    # A longer name that merely CONTAINS a looked-up one is not a holder.
    await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": type_id, "name": "purchase:success:extra"},
    )

    resp = await client.get(
        f"/api/v1/projects/{slug}/events/by-names",
        params=[
            ("event_type_id", type_id),
            ("names", "purchase:success"),
            ("names", "purchase"),
            ("names", "nothing:here"),
        ],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "items": [
            {
                "identity": "purchase:success",
                "event_id": event_id,
                "name": "purchase:success",
                "source_name": None,
            }
        ]
    }


async def test_by_names_is_bounded(client: AsyncClient) -> None:
    slug = "r2-by-names-bound"
    type_id, _event_id = await _project_with_event(client, slug)
    too_many = [("names", f"n{i}") for i in range(201)]
    resp = await client.get(
        f"/api/v1/projects/{slug}/events/by-names",
        params=[("event_type_id", type_id), *too_many],
    )
    assert resp.status_code == 422
    none = await client.get(
        f"/api/v1/projects/{slug}/events/by-names", params={"event_type_id": type_id}
    )
    assert none.status_code == 422
