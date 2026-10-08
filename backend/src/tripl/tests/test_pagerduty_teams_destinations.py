"""PagerDuty and Microsoft Teams alert destinations.

* both are created, validated and masked like the other secret-bearing
  channels — the routing key and the Teams URL are write-only;
* both are external channels, so a demo project refuses them;
* a Teams URL is held to the generic webhook's SSRF rules: https at the
  schema, private-host refusal at save AND at send;
* a PagerDuty delivery sends one Events API v2 ``trigger`` per incident it
  carries, keyed on the incident handle, and a re-run does not re-page;
* an incident tripl closes — automatically when the scope stops firing, or by
  Resolve in the Inbox — sends ONE ``resolve`` per PagerDuty destination that
  paged it, and none for a disabled destination or a demo project;
* the Test button reaches both channels.

All HTTP is faked; nothing here opens a socket.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import tripl.alerting_validation as av
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.demo_service import create_demo_project
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alert_digest_delivery import _fire_anomaly, _seed
from tripl.worker.tasks import alerts, alerts_pagerduty
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch

ROUTING_KEY = "R0uT1nGkEy0123456789abcdefABCDEF"
TEAMS_URL = "https://contoso.example.com/webhookb2/abc/IncomingWebhook/def/ghi"

Posted = list[tuple[str, dict[str, Any]]]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _capture_pagerduty(monkeypatch: pytest.MonkeyPatch, *, status: int = 202) -> Posted:
    posted: Posted = []

    def fake(url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        posted.append((url, body))
        return status, None

    monkeypatch.setattr(alerts, "_post_json_with_status", fake)
    return posted


def _capture_post_json(monkeypatch: pytest.MonkeyPatch) -> Posted:
    posted: Posted = []

    def fake(url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        posted.append((url, body))

    monkeypatch.setattr(alerts, "_post_json", fake)
    return posted


def _capture_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, list[uuid.UUID]]]:
    queued: list[tuple[uuid.UUID, list[uuid.UUID]]] = []
    monkeypatch.setattr(
        alerts_pagerduty,
        "enqueue_resolve",
        lambda project_id, group_ids: queued.append((project_id, list(group_ids))),
    )
    return queued


async def _project(client: AsyncClient, slug: str) -> None:
    resp = await client.post(
        "/api/v1/projects", json={"name": slug, "slug": slug, "description": ""}
    )
    assert resp.status_code == 201, resp.text


def _pagerduty_body(**extra: object) -> dict[str, object]:
    return {"type": "pagerduty", "name": "On-call", "pagerduty_routing_key": ROUTING_KEY, **extra}


def _teams_body(**extra: object) -> dict[str, object]:
    return {"type": "teams", "name": "Ops channel", "teams_webhook_url": TEAMS_URL, **extra}


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'pagerduty_teams.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _route_worker_to(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]) -> None:
    monkeypatch.setitem(alerts.send_alert_delivery.run.__globals__, "_get_sync_session", factory)


def _seed_delivery(
    session: Session,
    *,
    destination_type: str,
    group_ids: list[uuid.UUID | None],
    is_demo: bool = False,
    details_path: str | None = None,
) -> AlertDelivery:
    """One pending delivery with one item per entry of ``group_ids``."""
    project = Project(
        id=uuid.uuid4(),
        name="Checkout",
        slug=f"checkout-{uuid.uuid4().hex[:6]}",
        description="",
        is_demo=is_demo,
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:6]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    scan = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Events scan",
        base_query="SELECT * FROM events",
        time_column="created_at",
        cardinality_threshold=100,
        interval="1h",
    )
    # The test crypto layer is a passthrough, so plaintext round-trips.
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=destination_type,
        name="Pager" if destination_type == "pagerduty" else "Teams",
        enabled=True,
        pagerduty_routing_key_encrypted=ROUTING_KEY if destination_type == "pagerduty" else None,
        pagerduty_severity="critical" if destination_type == "pagerduty" else None,
        teams_webhook_url_encrypted=TEAMS_URL if destination_type == "teams" else None,
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Checkout drops",
        enabled=True,
        message_format="plain",
    )
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=project.id,
        scan_config_id=scan.id,
        destination_id=destination.id,
        rule_id=rule.id,
        channel=destination_type,
        status="pending",
        matched_count=len(group_ids),
        payload_snapshot={},
    )
    items = [
        AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref=f"event-{index}",
            scope_name=f"checkout:step{index}",
            bucket=datetime(2026, 10, 1, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=details_path,
            monitoring_path=None,
            correlation_group_id=group_id,
        )
        for index, group_id in enumerate(group_ids)
    ]
    session.add_all([project, data_source, scan, destination, rule, delivery, *items])
    session.commit()
    return delivery


# ---------------------------------------------------------------------------
# create / validate / mask
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagerduty_destination_is_created_masked_and_defaults_to_error(
    client: AsyncClient,
) -> None:
    await _project(client, "pd-create")
    resp = await client.post(
        "/api/v1/projects/pd-create/alert-destinations", json=_pagerduty_body()
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "pagerduty"
    assert body["pagerduty_routing_key_set"] is True
    assert body["pagerduty_severity"] == "error"
    assert body["teams_webhook_set"] is False
    assert ROUTING_KEY not in resp.text
    async with TestSessionLocal() as session:
        row = await session.get(AlertDestination, uuid.UUID(body["id"]))
        assert row is not None
        assert row.pagerduty_routing_key_encrypted is not None
        # Other channels' columns stay empty even when the form sends them.
        assert row.teams_webhook_url_encrypted is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"pagerduty_routing_key": "not a key!"}, "routing_key"),
        ({"pagerduty_routing_key": "x" * 65}, "routing_key"),
        ({"pagerduty_routing_key": None}, "routing_key"),
        ({"pagerduty_severity": "sev1"}, "severity"),
    ],
)
async def test_pagerduty_destination_refuses_a_bad_key_or_severity(
    client: AsyncClient, extra: dict[str, object], needle: str
) -> None:
    await _project(client, "pd-bad")
    resp = await client.post(
        "/api/v1/projects/pd-bad/alert-destinations", json=_pagerduty_body(**extra)
    )
    assert resp.status_code == 422
    assert needle in resp.text


@pytest.mark.asyncio
async def test_pagerduty_update_keeps_the_key_and_null_severity_means_default(
    client: AsyncClient,
) -> None:
    await _project(client, "pd-update")
    created = await client.post(
        "/api/v1/projects/pd-update/alert-destinations",
        json=_pagerduty_body(pagerduty_severity="Warning"),
    )
    assert created.json()["pagerduty_severity"] == "warning"
    url = f"/api/v1/projects/pd-update/alert-destinations/{created.json()['id']}"

    kept = await client.patch(url, json={"pagerduty_routing_key": "", "name": "Renamed"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["pagerduty_routing_key_set"] is True

    reset = await client.patch(url, json={"pagerduty_severity": None})
    assert reset.status_code == 200, reset.text
    assert reset.json()["pagerduty_severity"] == "error"

    rotated = await client.patch(url, json={"pagerduty_routing_key": "N3wKey"})
    assert rotated.status_code == 200
    async with TestSessionLocal() as session:
        row = await session.get(AlertDestination, uuid.UUID(created.json()["id"]))
        assert row is not None
        assert row.pagerduty_routing_key_encrypted == "N3wKey"


@pytest.mark.asyncio
async def test_teams_destination_is_created_masked(client: AsyncClient) -> None:
    await _project(client, "teams-create")
    resp = await client.post("/api/v1/projects/teams-create/alert-destinations", json=_teams_body())

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "teams"
    assert body["teams_webhook_set"] is True
    assert body["pagerduty_routing_key_set"] is False
    assert TEAMS_URL not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://contoso.example.com/hook",  # not https
        "https://127.0.0.1/hook",  # loopback literal
        "https://169.254.169.254/latest",  # cloud metadata
    ],
)
async def test_teams_destination_refuses_a_non_https_or_private_url(
    client: AsyncClient, url: str
) -> None:
    await _project(client, "teams-bad")
    resp = await client.post(
        "/api/v1/projects/teams-bad/alert-destinations", json=_teams_body(teams_webhook_url=url)
    )
    assert resp.status_code == 422
    assert "Teams webhook_url" in resp.text


@pytest.mark.asyncio
async def test_teams_destination_refuses_a_host_that_resolves_private(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hostname check runs in the service, so it covers the update path too."""
    await _project(client, "teams-dns")
    created = await client.post("/api/v1/projects/teams-dns/alert-destinations", json=_teams_body())
    assert created.status_code == 201

    monkeypatch.setattr(av.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.7", 0))])
    resp = await client.post(
        "/api/v1/projects/teams-dns/alert-destinations",
        json=_teams_body(teams_webhook_url="https://rebind.attacker.test/hook"),
    )
    assert resp.status_code == 422
    assert "private or internal" in resp.text

    patched = await client.patch(
        f"/api/v1/projects/teams-dns/alert-destinations/{created.json()['id']}",
        json={"teams_webhook_url": "https://rebind.attacker.test/hook"},
    )
    assert patched.status_code == 422
    async with TestSessionLocal() as session:
        row = await session.get(AlertDestination, uuid.UUID(created.json()["id"]))
        assert row is not None
        assert row.teams_webhook_url_encrypted == TEAMS_URL


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_pagerduty_body(), _teams_body()])
async def test_demo_projects_refuse_both_channels(
    client: AsyncClient, body: dict[str, object]
) -> None:
    async with TestSessionLocal() as session:
        demo = await create_demo_project(session)
    resp = await client.post(f"/api/v1/projects/{demo.slug}/alert-destinations", json=body)
    assert resp.status_code == 422
    assert "PagerDuty or Microsoft Teams" in resp.text


def test_demo_sink_may_not_carry_the_new_fields() -> None:
    from pydantic import ValidationError

    from tripl.schemas.alerting import AlertDestinationCreate

    for field, value in (
        ("pagerduty_routing_key", ROUTING_KEY),
        ("pagerduty_severity", "info"),
        ("teams_webhook_url", TEAMS_URL),
    ):
        with pytest.raises(ValidationError, match="demo_sink"):
            AlertDestinationCreate.model_validate(
                {"type": "demo_sink", "name": "Local", field: value}
            )


# ---------------------------------------------------------------------------
# PagerDuty delivery
# ---------------------------------------------------------------------------


def test_pagerduty_delivery_triggers_one_event_per_incident(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    group_a, group_b = uuid.uuid4(), uuid.uuid4()
    link = "https://tripl.example.com/o/default/p/checkout/alerting/x"
    with sync_session_factory() as session:
        delivery = _seed_delivery(
            session,
            destination_type="pagerduty",
            group_ids=[group_a, group_a, group_b],
            details_path=link,
        )
        delivery_id, slug = str(delivery.id), session.get(Project, delivery.project_id).slug
    _route_worker_to(monkeypatch, sync_session_factory)
    posted = _capture_pagerduty(monkeypatch)

    result = alerts.send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent", result
    assert [url for url, _ in posted] == [alerts_pagerduty.PAGERDUTY_EVENTS_URL] * 2
    by_key = {body["dedup_key"]: body for _, body in posted}
    assert set(by_key) == {f"tripl-{group_a}", f"tripl-{group_b}"}
    first = by_key[f"tripl-{group_a}"]
    assert first["event_action"] == "trigger"
    assert first["routing_key"] == ROUTING_KEY
    payload = first["payload"]
    assert payload["source"] == "tripl"
    assert payload["severity"] == "critical"
    assert payload["component"] == slug
    assert payload["summary"].startswith("[Checkout] Checkout drops")
    assert len(payload["summary"]) <= 1024
    # custom_details is the webhook's structured body, narrowed to the incident.
    assert payload["custom_details"]["rule"]["name"] == "Checkout drops"
    assert len(payload["custom_details"]["items"]) == 2
    assert len(by_key[f"tripl-{group_b}"]["payload"]["custom_details"]["items"]) == 1
    assert first["links"] == [{"href": link, "text": "Open in tripl"}]

    with sync_session_factory() as session:
        row = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert row is not None
        assert sorted(row.payload_snapshot["pagerduty_dedup_keys"]) == sorted(by_key)


def test_items_without_an_incident_key_on_the_delivery(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sync_session_factory() as session:
        delivery = _seed_delivery(session, destination_type="pagerduty", group_ids=[None, None])
        delivery_id = str(delivery.id)
    _route_worker_to(monkeypatch, sync_session_factory)
    posted = _capture_pagerduty(monkeypatch)

    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "sent"
    assert [body["dedup_key"] for _, body in posted] == [f"tripl-delivery-{delivery_id}"]
    assert "links" not in posted[0][1]


def test_a_rerun_after_a_partial_send_pages_only_what_is_missing(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Jira/Linear idempotency pattern, per event."""
    group_a, group_b = uuid.uuid4(), uuid.uuid4()
    with sync_session_factory() as session:
        delivery_id = str(
            _seed_delivery(session, destination_type="pagerduty", group_ids=[group_a, group_b]).id
        )
    _route_worker_to(monkeypatch, sync_session_factory)
    posted: Posted = []

    def second_event_fails(url: str, body: dict[str, Any], headers: Any = None) -> Any:
        posted.append((url, body))
        if len(posted) == 2:
            raise ValueError("HTTP 500 from https://events.pagerduty.com")
        return 202, None

    monkeypatch.setattr(alerts, "_post_json_with_status", second_event_fails)
    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "failed"
    first_key = posted[0][1]["dedup_key"]

    with sync_session_factory() as session:
        row = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert row is not None
        assert row.payload_snapshot["pagerduty_dedup_keys"] == [first_key]
        row.status = "pending"  # what Retry in the Inbox does
        session.commit()

    retried = _capture_pagerduty(monkeypatch)
    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "sent"
    assert [body["dedup_key"] for _, body in retried] == [posted[1][1]["dedup_key"]]
    assert first_key not in {body["dedup_key"] for _, body in retried}


def test_a_drift_s_summary_names_its_kind_not_a_spike() -> None:
    """A drift's row says "spike" only so a rule's Spikes toggle gates it."""
    rule = AlertRule(name="Drift watch")
    project = Project(name="Checkout", slug="checkout")

    def summary(scope_type: str, scope_name: str, direction: str, drift_field: str | None) -> str:
        item = AlertDeliveryItem(
            scope_type=scope_type,
            scope_name=scope_name,
            direction=direction,
            drift_field=drift_field,
        )
        return alerts_pagerduty._group_summary(
            project=project, rule=rule, items=[item], matched_count=1
        )

    assert (
        summary("distribution", "Screen View.platform", "spike", "platform")
        == "[Checkout] Drift watch: Distribution drift Screen View.platform (platform)"
    )
    assert summary("event", "checkout:step1", "drop", None) == (
        "[Checkout] Drift watch: checkout:step1 drop"
    )


@pytest.mark.parametrize("failure", ["status", "raise"])
def test_a_refused_event_fails_without_echoing_the_routing_key(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    with sync_session_factory() as session:
        delivery_id = str(
            _seed_delivery(session, destination_type="pagerduty", group_ids=[uuid.uuid4()]).id
        )
    _route_worker_to(monkeypatch, sync_session_factory)
    if failure == "status":
        _capture_pagerduty(monkeypatch, status=200)
    else:

        def refuse(url: str, body: dict[str, Any], headers: Any = None) -> Any:
            raise ValueError(f"HTTP 400 from https://events.pagerduty.com: bad key {ROUTING_KEY}")

        monkeypatch.setattr(alerts, "_post_json_with_status", refuse)

    result = alerts.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed"
    with sync_session_factory() as session:
        row = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert row is not None
        assert row.error_message
        assert ROUTING_KEY not in row.error_message
        assert not row.payload_snapshot.get("pagerduty_dedup_keys")


# ---------------------------------------------------------------------------
# Teams delivery
# ---------------------------------------------------------------------------


def test_teams_delivery_posts_an_adaptive_card(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    link = "https://tripl.example.com/o/default/p/checkout/alerting/y"
    with sync_session_factory() as session:
        delivery_id = str(
            _seed_delivery(
                session, destination_type="teams", group_ids=[uuid.uuid4()], details_path=link
            ).id
        )
    _route_worker_to(monkeypatch, sync_session_factory)
    posted = _capture_post_json(monkeypatch)

    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "sent"
    ((url, body),) = posted
    assert url == TEAMS_URL
    assert body["type"] == "message"
    (attachment,) = body["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.4"
    title, facts, message = card["body"]
    assert title["text"].startswith("[Checkout] Checkout drops")
    assert {"title": "Rule", "value": "Checkout drops"} in facts["facts"]
    assert "checkout:step0" in message["text"]
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open in tripl", "url": link}]


def test_teams_delivery_rechecks_the_host_at_send_time(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DNS rebinding: public at save, private by the time the alert goes out."""
    with sync_session_factory() as session:
        delivery_id = str(
            _seed_delivery(session, destination_type="teams", group_ids=[uuid.uuid4()]).id
        )
    _route_worker_to(monkeypatch, sync_session_factory)
    posted = _capture_post_json(monkeypatch)
    monkeypatch.setattr(
        av.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))]
    )

    result = alerts.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed"
    assert posted == []


@pytest.mark.parametrize("destination_type", ["pagerduty", "teams"])
def test_a_demo_project_sends_nothing(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    destination_type: str,
) -> None:
    with sync_session_factory() as session:
        delivery_id = str(
            _seed_delivery(
                session, destination_type=destination_type, group_ids=[uuid.uuid4()], is_demo=True
            ).id
        )
    _route_worker_to(monkeypatch, sync_session_factory)
    pagerduty = _capture_pagerduty(monkeypatch)
    teams = _capture_post_json(monkeypatch)

    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "failed"
    assert pagerduty == [] and teams == []


# ---------------------------------------------------------------------------
# resolve on close
# ---------------------------------------------------------------------------


def _paged_incident(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> tuple[ScanConfig, AlertDestination, uuid.UUID]:
    """A scope that fired and paged PagerDuty; returns its incident handle."""
    with factory() as session:
        config, destination, _rule, event_type = _seed(session, cron=None)
        row = session.get(AlertDestination, destination.id)
        assert row is not None
        row.type = "pagerduty"
        row.webhook_url_encrypted = None
        row.pagerduty_routing_key_encrypted = ROUTING_KEY
        session.commit()
        _fire_anomaly(session, config, event_type, actual=200.0)
        session.commit()
        (delivery_id,) = metrics_dispatch._prepare_alert_deliveries(
            session, config, scan_job_id=None
        )
        session.commit()
    _route_worker_to(monkeypatch, factory)
    posted = _capture_pagerduty(monkeypatch)
    assert alerts.send_alert_delivery.run(str(delivery_id))["status"] == "sent"
    group_id = uuid.UUID(posted[0][1]["dedup_key"].removeprefix("tripl-"))
    return config, destination, group_id


def _stop_firing(factory: sessionmaker[Session], config: ScanConfig) -> None:
    with factory() as session:
        session.execute(
            MetricAnomaly.__table__.delete().where(MetricAnomaly.scan_config_id == config.id)
        )
        session.commit()
        assert metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None) == []
        session.commit()


def _run_resolve(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    queued: list[tuple[uuid.UUID, list[uuid.UUID]]],
) -> Posted:
    monkeypatch.setitem(
        alerts.resolve_pagerduty_incidents.run.__globals__, "_get_sync_session", factory
    )
    posted = _capture_pagerduty(monkeypatch)
    for project_id, group_ids in queued:
        result = alerts.resolve_pagerduty_incidents.run(
            str(project_id), [str(group_id) for group_id in group_ids]
        )
        assert result["status"] == "done", result
    return posted


def test_an_incident_that_stops_firing_is_resolved_once(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _destination, group_id = _paged_incident(sync_session_factory, monkeypatch)
    queued = _capture_enqueue(monkeypatch)

    _stop_firing(sync_session_factory, config)

    # Queued after the commit, carrying the paged incident among its candidates.
    assert len(queued) == 1
    assert queued[0][0] == config.project_id
    assert group_id in queued[0][1]
    posted = _run_resolve(sync_session_factory, monkeypatch, queued)
    assert posted == [
        (
            alerts_pagerduty.PAGERDUTY_EVENTS_URL,
            {
                "routing_key": ROUTING_KEY,
                "event_action": "resolve",
                "dedup_key": f"tripl-{group_id}",
            },
        )
    ]

    # The same close reported again (a duplicate task, an Inbox Resolve after
    # the automatic one) sends nothing more.
    assert _run_resolve(sync_session_factory, monkeypatch, queued) == []
    # ...and a quiet scope is not a new close on the next collection.
    queued.clear()
    _stop_firing(sync_session_factory, config)
    assert queued == []


def test_a_rolled_back_close_queues_nothing(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _destination, _group_id = _paged_incident(sync_session_factory, monkeypatch)
    queued = _capture_enqueue(monkeypatch)
    with sync_session_factory() as session:
        session.execute(
            MetricAnomaly.__table__.delete().where(MetricAnomaly.scan_config_id == config.id)
        )
        session.commit()
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.rollback()
    assert queued == []


@pytest.mark.parametrize("blocker", ["disabled", "demo"])
def test_no_resolve_through_a_disabled_destination_or_a_demo_project(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    config, destination, _group_id = _paged_incident(sync_session_factory, monkeypatch)
    queued = _capture_enqueue(monkeypatch)
    _stop_firing(sync_session_factory, config)
    with sync_session_factory() as session:
        if blocker == "disabled":
            row = session.get(AlertDestination, destination.id)
            assert row is not None
            row.enabled = False
        else:
            project = session.get(Project, config.project_id)
            assert project is not None
            project.is_demo = True
        session.commit()

    assert _run_resolve(sync_session_factory, monkeypatch, queued) == []


def test_a_close_that_never_paged_pagerduty_queues_nothing(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(session, cron=None)  # Slack
        _fire_anomaly(session, config, event_type, actual=200.0)
        session.commit()
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()
    queued = _capture_enqueue(monkeypatch)
    _stop_firing(sync_session_factory, config)
    assert queued == []


@pytest.mark.asyncio
async def test_resolving_in_the_inbox_queues_the_resolve(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _project(client, "pd-inbox")
    group_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        project = (
            await session.execute(select(Project).where(Project.slug == "pd-inbox"))
        ).scalar_one()
        project_id = project.id
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Inbox DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
            organization_id=project.organization_id,
        )
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Scan",
            base_query="SELECT 1",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="pagerduty",
            name="Pager",
            enabled=True,
            pagerduty_routing_key_encrypted=ROUTING_KEY,
        )
        rule = AlertRule(id=uuid.uuid4(), destination_id=destination.id, name="Drops", enabled=True)
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="pagerduty",
            status="sent",
            sent_at=datetime.now(UTC),
            matched_count=1,
            payload_snapshot={"pagerduty_dedup_keys": [f"tripl-{group_id}"]},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="checkout:pay",
            bucket=datetime.now(UTC),
            direction="drop",
            actual_count=1,
            expected_count=20,
            absolute_delta=19,
            percent_delta=95,
            correlation_group_id=group_id,
        )
        # Flushed parent-first: not every one of these foreign keys has an ORM
        # relationship for the unit of work to order the INSERTs by.
        for row in (data_source, scan, destination, rule, delivery, item):
            session.add(row)
            await session.flush()
        await session.commit()
    queued = _capture_enqueue(monkeypatch)

    acked = await client.post(
        f"/api/v1/projects/pd-inbox/alert-inbox/{group_id}/actions",
        json={"action": "acknowledge"},
    )
    assert acked.status_code == 200, acked.text
    assert queued == []

    resolved = await client.post(
        f"/api/v1/projects/pd-inbox/alert-inbox/{group_id}/actions",
        json={"action": "resolve"},
    )
    assert resolved.status_code == 200, resolved.text
    assert queued == [(project_id, [group_id])]

    queued.clear()
    bulk = await client.post(
        "/api/v1/projects/pd-inbox/alert-inbox/bulk-actions",
        json={"action": "resolve", "correlation_group_ids": [str(group_id)]},
    )
    assert bulk.status_code == 200, bulk.text
    assert queued == [(project_id, [group_id])]


# ---------------------------------------------------------------------------
# Test button
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagerduty_test_send_triggers_and_resolves_a_test_event(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _project(client, "pd-test")
    created = await client.post(
        "/api/v1/projects/pd-test/alert-destinations", json=_pagerduty_body()
    )
    posted = _capture_pagerduty(monkeypatch)

    resp = await client.post(
        f"/api/v1/projects/pd-test/alert-destinations/{created.json()['id']}/test"
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    trigger, resolve = (body for _, body in posted)
    assert trigger["event_action"] == "trigger"
    assert trigger["payload"]["severity"] == "info"
    assert "test" in trigger["payload"]["summary"].lower()
    assert resolve == {
        "routing_key": ROUTING_KEY,
        "event_action": "resolve",
        "dedup_key": trigger["dedup_key"],
    }
    assert trigger["dedup_key"].startswith("tripl-test-")


@pytest.mark.asyncio
async def test_teams_test_send_and_draft_test(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _project(client, "teams-test")
    created = await client.post(
        "/api/v1/projects/teams-test/alert-destinations", json=_teams_body()
    )
    posted = _capture_post_json(monkeypatch)

    saved = await client.post(
        f"/api/v1/projects/teams-test/alert-destinations/{created.json()['id']}/test"
    )
    assert saved.json()["ok"] is True, saved.text
    # An edit dialog leaves the URL blank: the stored one is used.
    draft = await client.post(
        "/api/v1/projects/teams-test/alert-destinations/test",
        json={"type": "teams", "destination_id": created.json()["id"], "teams_webhook_url": ""},
    )
    assert draft.json()["ok"] is True, draft.text
    assert [url for url, _ in posted] == [TEAMS_URL, TEAMS_URL]
    assert posted[0][1]["attachments"][0]["contentType"] == (
        "application/vnd.microsoft.card.adaptive"
    )

    private = await client.post(
        "/api/v1/projects/teams-test/alert-destinations/test",
        json={"type": "teams", "teams_webhook_url": "https://127.0.0.1/hook"},
    )
    assert private.json()["ok"] is False
    assert len(posted) == 2


@pytest.mark.asyncio
async def test_a_draft_test_refuses_an_unknown_severity(client: AsyncClient) -> None:
    await _project(client, "pd-draft")
    resp = await client.post(
        "/api/v1/projects/pd-draft/alert-destinations/test",
        json={
            "type": "pagerduty",
            "pagerduty_routing_key": ROUTING_KEY,
            "pagerduty_severity": "p1",
        },
    )
    assert resp.status_code == 422


def test_enqueue_publishes_inline_in_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.worker.celery_app import celery_app

    sent: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(celery_app, "send_task", lambda name, args: sent.append((name, args)))
    project_id, group_id = uuid.uuid4(), uuid.uuid4()

    alerts_pagerduty.enqueue_resolve(project_id, [group_id])

    assert sent == [(alerts_pagerduty.RESOLVE_TASK_NAME, [str(project_id), [str(group_id)]])]
    assert alerts_pagerduty.RESOLVE_TASK_NAME in celery_app.tasks


@pytest.mark.asyncio
async def test_enqueue_stays_off_the_event_loop_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the API side the publish is offloaded, and a broker failure is swallowed."""
    import asyncio
    import threading

    from tripl.worker.celery_app import celery_app

    loop_thread = threading.get_ident()
    threads: list[int] = []

    def broker_down(name: str, args: list[object]) -> None:
        threads.append(threading.get_ident())
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(celery_app, "send_task", broker_down)
    alerts_pagerduty.enqueue_resolve(uuid.uuid4(), [uuid.uuid4()])
    pending = list(alerts_pagerduty._BACKGROUND)
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)

    assert threads and threads[0] != loop_thread
    assert not alerts_pagerduty._BACKGROUND
