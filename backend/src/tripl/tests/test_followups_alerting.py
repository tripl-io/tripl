"""Follow-ups to the design review's alerting findings (lane F2-alerting).

"Send test" from the destination dialog, before the destination is
saved — ``POST /projects/{slug}/alert-destinations/test``. The draft is sent
through the same channel senders, the same demo zero-egress predicate and the
same private-host refusal as a saved destination's Test; an edit dialog lends
the stored secrets it cannot show through ``destination_id``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.alert_destination import AlertDestination
from tripl.models.audit_log import AuditLog
from tripl.models.project import Project
from tripl.tests.conftest import TestSessionLocal

SLACK_WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXX"
OTHER_SLACK_WEBHOOK = "https://hooks.slack.com/services/T111/B111/YYY"

Posted = list[tuple[str, dict[str, object], dict[str, str] | None]]


def _capture_posts(monkeypatch: pytest.MonkeyPatch) -> Posted:
    from tripl.worker.tasks import alerts

    posted: Posted = []

    def capture_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        posted.append((url, body, headers))
        return None

    monkeypatch.setattr(alerts, "_post_json", capture_post_json)
    return posted


async def _project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug.replace("-", " ").title(), "slug": slug, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _destination(client: AsyncClient, slug: str, body: dict[str, object]) -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/alert-destinations", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _test_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/alert-destinations/test"


@pytest.mark.asyncio
async def test_an_unsaved_destination_is_tested_without_being_saved(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    await _project(client, "draft-send")

    resp = await client.post(
        _test_url("draft-send"),
        json={
            "type": "slack",
            "name": "Ops Slack",
            "webhook_url": SLACK_WEBHOOK,
            # What the dialog's form carries and a test does not use.
            "enabled": True,
            "delivery_schedule_cron": None,
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["sent_at"] is not None
    assert [url for url, _body, _headers in posted] == [SLACK_WEBHOOK]
    text = posted[0][1]["text"]
    assert isinstance(text, str)
    assert "Tripl test message" in text
    assert "Destination: Ops Slack" in text

    async with TestSessionLocal() as session:
        # Testing is not saving: nothing is written but the audit entry.
        assert (await session.execute(select(AlertDestination))).scalars().all() == []
        audits = [
            row
            for row in (await session.execute(select(AuditLog))).scalars().all()
            if row.action == "alert_destination.test"
        ]
        assert len(audits) == 1
        assert audits[0].target_id is None
        assert audits[0].target_name == "Ops Slack"


@pytest.mark.asyncio
async def test_an_edit_draft_sends_with_the_stored_secret_it_left_blank(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    await _project(client, "draft-edit")
    destination_id = await _destination(
        client,
        "draft-edit",
        {"type": "slack", "name": "Ops Slack", "enabled": True, "webhook_url": SLACK_WEBHOOK},
    )

    # The dialog never holds the webhook: blank means "the one on file".
    kept = await client.post(
        _test_url("draft-edit"),
        json={
            "destination_id": destination_id,
            "type": "slack",
            "name": "Renamed",
            "webhook_url": "",
        },
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["ok"] is True

    # A typed one is the one being tested, not the stored one.
    replaced = await client.post(
        _test_url("draft-edit"),
        json={
            "destination_id": destination_id,
            "type": "slack",
            "name": "Renamed",
            "webhook_url": OTHER_SLACK_WEBHOOK,
        },
    )
    assert replaced.json()["ok"] is True

    assert [url for url, _body, _headers in posted] == [SLACK_WEBHOOK, OTHER_SLACK_WEBHOOK]
    # The name the form shows, not the stored one.
    assert "Destination: Renamed" in str(posted[0][1]["text"])

    async with TestSessionLocal() as session:
        stored = await session.get(AlertDestination, uuid.UUID(destination_id))
        assert stored is not None
        # A test changes nothing on the row.
        assert stored.name == "Ops Slack"


@pytest.mark.asyncio
async def test_a_webhook_header_secret_follows_its_name(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    await _project(client, "draft-header")
    destination_id = await _destination(
        client,
        "draft-header",
        {
            "type": "webhook",
            "name": "Hook",
            "enabled": True,
            "target_url": "https://hooks.example.com/tripl",
            "webhook_header_name": "X-Secret",
            "webhook_header_value": "s3cret",
        },
    )

    # A kept name with a blank value sends the stored value, as a PATCH keeps it.
    kept = await client.post(
        _test_url("draft-header"),
        json={
            "destination_id": destination_id,
            "type": "webhook",
            "target_url": "",
            "webhook_header_name": "X-Secret",
            "webhook_header_value": "",
        },
    )
    assert kept.json()["ok"] is True
    # A header the form removed goes out with neither half.
    removed = await client.post(
        _test_url("draft-header"),
        json={
            "destination_id": destination_id,
            "type": "webhook",
            "webhook_header_name": None,
            "webhook_header_value": None,
        },
    )
    assert removed.json()["ok"] is True

    assert [url for url, _body, _headers in posted] == [
        "https://hooks.example.com/tripl",
        "https://hooks.example.com/tripl",
    ]
    assert posted[0][2] == {"X-Secret": "s3cret"}
    assert not posted[1][2]
    # Typed so a receiver can drop it without reading prose.
    assert posted[0][1]["event"] == "tripl.destination_test"


@pytest.mark.asyncio
async def test_a_half_filled_draft_is_an_answer_not_a_422(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    await _project(client, "draft-missing")

    resp = await client.post(
        _test_url("draft-missing"),
        json={"type": "slack", "name": "No URL yet", "webhook_url": "  "},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error_kind"] == "config"
    assert body["sent_at"] is None
    assert posted == []


@pytest.mark.asyncio
async def test_a_draft_webhook_cannot_reach_a_private_host(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test send is egress like any other: the draft path must not become a
    way around the private-host refusal a saved destination gets."""
    posted = _capture_posts(monkeypatch)
    await _project(client, "draft-ssrf")

    for target in ("https://127.0.0.1/hook", "https://169.254.169.254/latest/meta-data"):
        resp = await client.post(
            _test_url("draft-ssrf"),
            json={"type": "webhook", "name": "Probe", "target_url": target},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is False

    assert posted == []


@pytest.mark.asyncio
async def test_a_draft_header_that_could_inject_is_refused(client: AsyncClient) -> None:
    await _project(client, "draft-inject")

    resp = await client.post(
        _test_url("draft-inject"),
        json={
            "type": "webhook",
            "target_url": "https://hooks.example.com/tripl",
            "webhook_header_name": "X-Secret",
            "webhook_header_value": "value\r\nX-Injected: 1",
        },
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_draft_cannot_change_a_saved_destinations_channel(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    await _project(client, "draft-type")
    destination_id = await _destination(
        client,
        "draft-type",
        {"type": "slack", "name": "Ops Slack", "enabled": True, "webhook_url": SLACK_WEBHOOK},
    )

    resp = await client.post(
        _test_url("draft-type"),
        json={
            "destination_id": destination_id,
            "type": "webhook",
            "target_url": "https://hooks.example.com/tripl",
        },
    )

    assert resp.status_code == 422
    assert posted == []


@pytest.mark.asyncio
async def test_a_draft_cannot_borrow_another_projects_secrets(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    await _project(client, "draft-owner")
    await _project(client, "draft-other")
    foreign_id = await _destination(
        client,
        "draft-owner",
        {"type": "slack", "name": "Owner Slack", "enabled": True, "webhook_url": SLACK_WEBHOOK},
    )

    resp = await client.post(
        _test_url("draft-other"),
        json={"destination_id": foreign_id, "type": "slack"},
    )

    assert resp.status_code == 404
    assert posted == []


@pytest.mark.asyncio
async def test_a_draft_on_a_demo_project_never_leaves_the_box(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    project_id = await _project(client, "draft-demo")
    async with TestSessionLocal() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        project.is_demo = True
        await session.commit()

    slack = await client.post(
        _test_url("draft-demo"),
        json={"type": "slack", "name": "Ops Slack", "webhook_url": SLACK_WEBHOOK},
    )
    assert slack.status_code == 200
    assert slack.json()["ok"] is False
    assert slack.json()["error_kind"] == "policy"

    sink = await client.post(
        _test_url("draft-demo"),
        json={"type": "demo_sink", "name": "Local sink"},
    )
    assert sink.json()["ok"] is True

    assert posted == []


@pytest.mark.asyncio
async def test_the_saved_destination_test_still_answers_as_before(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saved Test and the draft Test now share one send; the saved one's
    contract is unchanged."""
    posted = _capture_posts(monkeypatch)
    await _project(client, "saved-send")
    destination_id = await _destination(
        client,
        "saved-send",
        {"type": "slack", "name": "Ops Slack", "enabled": True, "webhook_url": SLACK_WEBHOOK},
    )

    resp = await client.post(
        f"/api/v1/projects/saved-send/alert-destinations/{destination_id}/test"
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert [url for url, _body, _headers in posted] == [SLACK_WEBHOOK]
