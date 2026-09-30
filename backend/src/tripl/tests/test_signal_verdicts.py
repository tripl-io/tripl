"""Signal verdicts (F01, #254): expected / tracking_bug / false_positive / real_issue.

Covers setting and clearing a verdict on a signal no rule routed, the incident
as source of truth on one that was routed (the verdict writes through, and an
inbox status change reflects back), false-positive tuning through the shared
helper on both paths, the sidebar badge and the ``needs_verdict`` filter leaving
verdicted signals out, the verdict-counts route, the event activity entry, the
chart point payload, the audit row, and the viewer / non-member gates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.api.deps import get_current_user
from tripl.main import app
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.audit_log import AuditLog
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.signal_triage import SignalTriage
from tripl.models.user import User
from tripl.services import _alerting_deliveries, alerting_service
from tripl.tests._members import PASSWORD_HASH_PLACEHOLDER, persisted_member_user
from tripl.tests.conftest import TestSessionLocal

pytestmark = pytest.mark.asyncio

# Recent and hour-aligned, so every seeded scope classifies as an open signal.
_BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


class _Seeded:
    def __init__(self, ids: dict[str, str], slug: str) -> None:
        self.ids = ids
        self.slug = slug

    @property
    def verdict_url(self) -> str:
        return f"/api/v1/projects/{self.slug}/signals/verdict"

    def event_scope(self, **extra: Any) -> dict[str, Any]:
        return {
            "scan_config_id": self.ids["scan_config_id"],
            "scope_type": "event",
            "scope_ref": self.ids["event_id"],
            "bucket": _BUCKET.isoformat(),
            **extra,
        }

    def total_scope(self, **extra: Any) -> dict[str, Any]:
        return {
            "scan_config_id": self.ids["scan_config_id"],
            "scope_type": "project_total",
            "scope_ref": self.ids["scan_config_id"],
            "bucket": _BUCKET.isoformat(),
            **extra,
        }

    def params(self, scope: dict[str, Any]) -> dict[str, Any]:
        return {key: scope[key] for key in ("scan_config_id", "scope_type", "scope_ref", "bucket")}


async def _seed(client: AsyncClient, slug: str) -> _Seeded:
    """Project + event + scan with two open, significant signals: event and project_total."""
    created = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert created.status_code == 201, created.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type.status_code == 201, event_type.text
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type.json()["id"],
            "name": "Landing Viewed",
            "status": "implemented",
        },
    )
    assert event.status_code == 201, event.text
    data_source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Warehouse {slug}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source.status_code == 201, data_source.text
    scan = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source.json()["id"],
            "name": "Scan",
            "base_query": "SELECT 1",
        },
    )
    assert scan.status_code == 201, scan.text
    project = await client.get(f"/api/v1/projects/{slug}")
    ids = {
        "project_id": project.json()["id"],
        "event_type_id": event_type.json()["id"],
        "event_id": event.json()["id"],
        "scan_config_id": scan.json()["id"],
    }
    scan_id = uuid.UUID(ids["scan_config_id"])
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                scan_config_id=scan_id,
                event_id=None,
                event_type_id=uuid.UUID(ids["event_type_id"]),
                bucket=_BUCKET,
                count=480,
            )
        )
        session.add(
            EventMetric(
                scan_config_id=scan_id,
                event_id=uuid.UUID(ids["event_id"]),
                event_type_id=None,
                bucket=_BUCKET,
                count=480,
            )
        )
        # Opposite directions, so the event signal is not folded under the total.
        for scope_type, scope_ref, event_id, direction in (
            ("project_total", ids["scan_config_id"], None, "drop"),
            ("event", ids["event_id"], uuid.UUID(ids["event_id"]), "spike"),
        ):
            session.add(
                MetricAnomaly(
                    scan_config_id=scan_id,
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    event_id=event_id,
                    event_type_id=None,
                    bucket=_BUCKET,
                    actual_count=480 if direction == "spike" else 30,
                    expected_count=120,
                    stddev=10,
                    z_score=9 if direction == "spike" else -9,
                    direction=direction,
                    created_at=_BUCKET,
                )
            )
        await session.commit()
    return _Seeded(ids, slug)


async def _route_total_to_incident(
    client: AsyncClient, seeded: _Seeded, *, delivered_at: datetime = _BUCKET
) -> uuid.UUID:
    """Deliver the project_total signal into an inbox incident; return its group id."""
    destination = await client.post(
        f"/api/v1/projects/{seeded.slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Slack",
            "enabled": True,
            "webhook_url": f"https://hooks.slack.com/services/T1/B1/{uuid.uuid4().hex[:8]}",
        },
    )
    assert destination.status_code == 201, destination.text
    rule = await client.post(
        f"/api/v1/projects/{seeded.slug}/alert-destinations/{destination.json()['id']}/rules",
        json={"name": "Slack rule", "enabled": True, "filters": []},
    )
    assert rule.status_code == 201, rule.text
    group_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=uuid.UUID(seeded.ids["project_id"]),
            scan_config_id=uuid.UUID(seeded.ids["scan_config_id"]),
            destination_id=uuid.UUID(destination.json()["id"]),
            rule_id=uuid.UUID(rule.json()["id"]),
            status="sent",
            channel="slack",
            matched_count=1,
            created_at=delivered_at,
        )
        session.add(delivery)
        await session.flush()
        session.add(
            AlertDeliveryItem(
                delivery_id=delivery.id,
                scope_type="project_total",
                scope_ref=seeded.ids["scan_config_id"],
                scope_name="Project total",
                event_type_id=None,
                event_id=None,
                bucket=_BUCKET,
                direction="drop",
                actual_count=30,
                expected_count=120,
                absolute_delta=90,
                percent_delta=75.0,
                correlation_group_id=group_id,
            )
        )
        await session.commit()
    return group_id


async def _badge(client: AsyncClient, slug: str) -> int:
    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200, resp.text
    return resp.json()["summary"]["monitoring_signal_count"]


async def _expanded(client: AsyncClient, slug: str, **params: Any) -> dict[str, dict[str, Any]]:
    resp = await client.get(
        f"/api/v1/projects/{slug}/anomalies/signals", params={"expanded": "true", **params}
    )
    assert resp.status_code == 200, resp.text
    return {signal["scope_type"]: signal for signal in resp.json()}


async def _incident_status(group_id: uuid.UUID) -> str | None:
    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        return None if state is None else str(state.status)


async def _verdict_rows() -> list[SignalTriage]:
    async with TestSessionLocal() as session:
        return list((await session.execute(select(SignalTriage))).scalars())


def _as(user: User) -> None:
    async def _override() -> User:
        return user

    app.dependency_overrides[get_current_user] = _override


# --- unrouted signals ----------------------------------------------------------


async def test_set_replace_and_clear_a_verdict(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-basic")
    assert await _badge(client, seeded.slug) == 2

    resp = await client.post(
        seeded.verdict_url,
        json=seeded.event_scope(verdict="tracking_bug", note="  SDK sends it twice  "),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"]["verdict"] == "tracking_bug"
    assert body["verdict"]["note"] == "SDK sends it twice"
    assert body["verdict"]["source"] == "signal"
    assert body["verdict"]["author_name"] == "Test User"
    assert body["verdict"]["created_at"] is not None
    assert body["incident"] is None
    assert body["hidden"] is False

    signals = await _expanded(client, seeded.slug)
    assert signals["event"]["verdict"]["verdict"] == "tracking_bug"
    assert signals["project_total"]["verdict"] is None
    # A verdicted signal leaves the badge and the default "Needs verdict" view.
    assert await _badge(client, seeded.slug) == 1
    assert set(await _expanded(client, seeded.slug, needs_verdict="true")) == {"project_total"}

    # A second verdict replaces the first: one verdict per signal.
    resp = await client.post(seeded.verdict_url, json=seeded.event_scope(verdict="real_issue"))
    assert resp.status_code == 200, resp.text
    rows = await _verdict_rows()
    assert [str(row.action) for row in rows] == ["real_issue"]

    cleared = await client.delete(seeded.verdict_url, params=seeded.params(seeded.event_scope()))
    assert cleared.status_code == 204, cleared.text
    assert await _verdict_rows() == []
    assert (await _expanded(client, seeded.slug))["event"]["verdict"] is None
    assert await _badge(client, seeded.slug) == 2
    # Clearing again is a no-op.
    again = await client.delete(seeded.verdict_url, params=seeded.params(seeded.event_scope()))
    assert again.status_code == 204


async def test_expected_with_a_reason_annotates_and_hides(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-expected")
    resp = await client.post(
        seeded.verdict_url,
        json=seeded.event_scope(verdict="expected", expected_reason="campaign", note="Promo"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"]["verdict"] == "expected"
    assert body["verdict"]["expected_reason"] == "campaign"
    assert body["hidden"] is True
    assert body["expected"] is True
    async with TestSessionLocal() as session:
        (annotation,) = (await session.execute(select(ChartAnnotation))).scalars().all()
    assert annotation.description == "Promo"

    # Switching to another verdict takes the annotation (and the hiding) away.
    resp = await client.post(seeded.verdict_url, json=seeded.event_scope(verdict="real_issue"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["hidden"] is False
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ChartAnnotation))).scalars().all() == []


async def test_reason_is_refused_on_other_verdicts(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-reason")
    resp = await client.post(
        seeded.verdict_url,
        json=seeded.event_scope(verdict="real_issue", expected_reason="release"),
    )
    assert resp.status_code == 422, resp.text


async def test_unknown_signal_is_404(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-404")
    stale = (_BUCKET - timedelta(days=3)).isoformat()
    body = seeded.event_scope(verdict="real_issue", bucket=stale)
    resp = await client.post(seeded.verdict_url, json=body)
    assert resp.status_code == 404, resp.text


async def test_false_positive_tunes_the_scope_through_the_shared_helper(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed(client, "verdict-fp")
    calls: list[list[Any]] = []
    real = alerting_service.tune_false_positive_scopes

    async def _spy(session: Any, *, project_id: uuid.UUID, scopes: Any) -> int:
        scopes = list(scopes)
        calls.append(scopes)
        return await real(session, project_id=project_id, scopes=scopes)

    monkeypatch.setattr(alerting_service, "tune_false_positive_scopes", _spy)

    resp = await client.post(seeded.verdict_url, json=seeded.event_scope(verdict="false_positive"))
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    (scope,) = calls[0]
    assert (scope.scope_type, scope.scope_ref) == ("event", seeded.ids["event_id"])
    assert scope.scope_name == "Landing Viewed"

    # A repeat of the same verdict does not ratchet detection a second time.
    again = await client.post(seeded.verdict_url, json=seeded.event_scope(verdict="false_positive"))
    assert again.status_code == 200, again.text
    assert len(calls) == 1
    async with TestSessionLocal() as session:
        (override,) = (await session.execute(select(AnomalyScopeOverride))).scalars().all()
        audited = (
            (await session.execute(select(AuditLog).where(AuditLog.action == "signal.verdict")))
            .scalars()
            .all()
        )
    assert override.scope_ref == seeded.ids["event_id"]
    assert override.false_positive_count == 1
    assert len(audited) == 1
    assert audited[0].payload["verdict"] == "false_positive"


# --- routed signals: the incident is the source of truth -----------------------


async def test_verdict_on_a_routed_signal_writes_through_to_the_incident(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed(client, "verdict-routed")
    group_id = await _route_total_to_incident(client, seeded)
    assert await _badge(client, seeded.slug) == 2

    resp = await client.post(
        seeded.verdict_url, json=seeded.total_scope(verdict="tracking_bug", note="dup events")
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert await _incident_status(group_id) == "acknowledged"
    assert body["incident"] == {"id": str(group_id), "status": "acknowledged"}
    # The signal's own row refines what "acknowledged" means.
    assert body["verdict"]["verdict"] == "tracking_bug"
    assert body["verdict"]["source"] == "signal"
    assert await _badge(client, seeded.slug) == 1

    # false_positive -> incident false_positive, tuned through the incident path,
    # which feeds the same shared helper.
    calls: list[Any] = []
    real = _alerting_deliveries.tune_false_positive_scopes

    async def _spy(session: Any, *, project_id: uuid.UUID, scopes: Any) -> int:
        scopes = list(scopes)
        calls.append(scopes)
        return await real(session, project_id=project_id, scopes=scopes)

    monkeypatch.setattr(_alerting_deliveries, "tune_false_positive_scopes", _spy)
    resp = await client.post(seeded.verdict_url, json=seeded.total_scope(verdict="false_positive"))
    assert resp.status_code == 200, resp.text
    assert await _incident_status(group_id) == "false_positive"
    assert len(calls) == 1
    assert [scope.scope_type for scope in calls[0]] == ["project_total"]

    # expected -> resolved.
    resp = await client.post(seeded.verdict_url, json=seeded.total_scope(verdict="expected"))
    assert resp.status_code == 200, resp.text
    assert await _incident_status(group_id) == "resolved"
    assert resp.json()["verdict"]["verdict"] == "expected"

    # Clearing reopens the incident, so no verdict reads off it any more.
    cleared = await client.delete(seeded.verdict_url, params=seeded.params(seeded.total_scope()))
    assert cleared.status_code == 204, cleared.text
    assert await _incident_status(group_id) == "open"
    total = (await _expanded(client, seeded.slug))["project_total"]
    assert total["verdict"] is None
    assert total["incident"] == {"id": str(group_id), "status": "open"}
    assert await _badge(client, seeded.slug) == 2


async def test_incident_status_change_reflects_onto_its_signal(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-reflect")
    group_id = await _route_total_to_incident(client, seeded)
    actions = f"/api/v1/projects/{seeded.slug}/alert-inbox/{group_id}/actions"

    resolved = await client.post(actions, json={"action": "resolve", "note": "deploy fixed it"})
    assert resolved.status_code == 200, resolved.text
    total = (await _expanded(client, seeded.slug))["project_total"]
    assert total["verdict"]["verdict"] == "expected"
    assert total["verdict"]["source"] == "incident"
    assert total["verdict"]["note"] == "deploy fixed it"
    assert total["verdict"]["author_name"] == "Test User"
    assert total["incident"]["status"] == "resolved"
    assert await _badge(client, seeded.slug) == 1

    # ``real_issue`` agrees with a resolved incident (fixed and resolved), so
    # it refines the status without moving the incident.
    resp = await client.post(seeded.verdict_url, json=seeded.total_scope(verdict="real_issue"))
    assert resp.status_code == 200, resp.text
    assert await _incident_status(group_id) == "resolved"
    assert resp.json()["verdict"]["verdict"] == "real_issue"
    assert resp.json()["verdict"]["source"] == "signal"
    # The inbox moves the incident away from the row: the row is deleted and
    # the verdict reads off the incident.
    fp = await client.post(actions, json={"action": "false_positive"})
    assert fp.status_code == 200, fp.text
    assert await _verdict_rows() == []
    total = (await _expanded(client, seeded.slug))["project_total"]
    assert total["verdict"]["verdict"] == "false_positive"
    assert total["verdict"]["source"] == "incident"

    reopened = await client.post(actions, json={"action": "reopen"})
    assert reopened.status_code == 200, reopened.text
    assert (await _expanded(client, seeded.slug))["project_total"]["verdict"] is None
    assert await _badge(client, seeded.slug) == 2


async def test_reposting_an_agreeing_verdict_leaves_the_incident_where_it_is(
    client: AsyncClient,
) -> None:
    seeded = await _seed(client, "verdict-repost")
    group_id = await _route_total_to_incident(client, seeded)
    actions = f"/api/v1/projects/{seeded.slug}/alert-inbox/{group_id}/actions"
    resolved = await client.post(actions, json={"action": "resolve"})
    assert resolved.status_code == 200, resolved.text

    for note in ("campaign launch", "campaign launch, edited"):
        resp = await client.post(
            seeded.verdict_url, json=seeded.total_scope(verdict="expected", note=note)
        )
        assert resp.status_code == 200, resp.text
        assert await _incident_status(group_id) == "resolved"
        assert resp.json()["verdict"]["verdict"] == "expected"
        assert resp.json()["verdict"]["note"] == note
    # Nothing moved the incident, so no inbox action was audited for it.
    async with TestSessionLocal() as session:
        inbox_actions = list(
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.action.like("alert_inbox.%"))
                )
            ).scalars()
        )
    assert [str(action) for action in inbox_actions] == ["alert_inbox.resolve"]


async def test_inbox_reopen_then_acknowledge_does_not_resurrect_the_signal_row(
    client: AsyncClient,
) -> None:
    seeded = await _seed(client, "verdict-resurrect")
    group_id = await _route_total_to_incident(client, seeded)
    actions = f"/api/v1/projects/{seeded.slug}/alert-inbox/{group_id}/actions"

    resp = await client.post(seeded.verdict_url, json=seeded.total_scope(verdict="tracking_bug"))
    assert resp.status_code == 200, resp.text
    assert await _incident_status(group_id) == "acknowledged"
    assert [str(row.action) for row in await _verdict_rows()] == ["tracking_bug"]

    reopened = await client.post(actions, json={"action": "reopen"})
    assert reopened.status_code == 200, reopened.text
    assert await _verdict_rows() == []

    acknowledged = await client.post(actions, json={"action": "acknowledge"})
    assert acknowledged.status_code == 200, acknowledged.text
    total = (await _expanded(client, seeded.slug))["project_total"]
    assert total["verdict"]["verdict"] == "real_issue"
    assert total["verdict"]["source"] == "incident"


async def test_bulk_inbox_move_deletes_stale_rows_and_their_annotations(
    client: AsyncClient,
) -> None:
    seeded = await _seed(client, "verdict-bulk-prune")
    group_id = await _route_total_to_incident(client, seeded)
    resp = await client.post(seeded.verdict_url, json=seeded.total_scope(verdict="expected"))
    assert resp.status_code == 200, resp.text
    assert await _incident_status(group_id) == "resolved"
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ChartAnnotation))).scalars().first() is not None

    bulk = await client.post(
        f"/api/v1/projects/{seeded.slug}/alert-inbox/bulk-actions",
        json={"correlation_group_ids": [str(group_id)], "action": "reopen"},
    )
    assert bulk.status_code == 200, bulk.text
    assert await _verdict_rows() == []
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ChartAnnotation))).scalars().first() is None

    resolved = await client.post(
        f"/api/v1/projects/{seeded.slug}/alert-inbox/{group_id}/actions",
        json={"action": "resolve"},
    )
    assert resolved.status_code == 200, resolved.text
    total = (await _expanded(client, seeded.slug))["project_total"]
    assert total["verdict"]["verdict"] == "expected"
    assert total["verdict"]["source"] == "incident"


async def test_a_verdict_set_before_routing_survives_it(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-before-routing")
    resp = await client.post(
        seeded.verdict_url, json=seeded.total_scope(verdict="tracking_bug", note="known dup")
    )
    assert resp.status_code == 200, resp.text
    assert await _badge(client, seeded.slug) == 1

    # A rule routes the signal afterwards; nobody has acted on the incident.
    group_id = await _route_total_to_incident(
        client, seeded, delivered_at=datetime.now(UTC) + timedelta(seconds=5)
    )
    assert await _incident_status(group_id) is None
    total = (await _expanded(client, seeded.slug))["project_total"]
    assert total["incident"] == {"id": str(group_id), "status": "open"}
    assert total["verdict"]["verdict"] == "tracking_bug"
    assert total["verdict"]["source"] == "signal"
    assert total["verdict"]["note"] == "known dup"
    # Every surface agrees: the badge, the needs-verdict filter and the counts.
    assert await _badge(client, seeded.slug) == 1
    assert set(await _expanded(client, seeded.slug, needs_verdict="true")) == {"event"}
    counts = await client.get(f"/api/v1/projects/{seeded.slug}/signals/verdict-counts")
    assert counts.status_code == 200, counts.text
    assert counts.json()["needs_verdict"] == 1
    assert counts.json()["tracking_bug"] == 1

    # Once the inbox acts on the incident, the incident is the source of truth.
    actions = f"/api/v1/projects/{seeded.slug}/alert-inbox/{group_id}/actions"
    fp = await client.post(actions, json={"action": "false_positive"})
    assert fp.status_code == 200, fp.text
    total = (await _expanded(client, seeded.slug))["project_total"]
    assert total["verdict"]["verdict"] == "false_positive"
    assert total["verdict"]["source"] == "incident"


async def test_a_sub_threshold_signal_needs_no_verdict_anywhere(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-sub-threshold")
    # Shrink the event signal below the "Significant" magnitude gate (0.5):
    # still an open signal, but one the badge does not count.
    async with TestSessionLocal() as session:
        anomaly = await session.scalar(
            select(MetricAnomaly).where(
                MetricAnomaly.scope_type == "event",
                MetricAnomaly.scope_ref == seeded.ids["event_id"],
            )
        )
        assert anomaly is not None
        anomaly.actual_count = 150
        await session.commit()

    badge = await _badge(client, seeded.slug)
    counts = await client.get(f"/api/v1/projects/{seeded.slug}/signals/verdict-counts")
    assert counts.status_code == 200, counts.text
    assert badge == 1
    assert counts.json()["needs_verdict"] == badge
    assert set(await _expanded(client, seeded.slug, needs_verdict="true")) == {"project_total"}


async def test_a_verdict_that_moves_an_incident_audits_the_inbox_action(
    client: AsyncClient,
) -> None:
    seeded = await _seed(client, "verdict-incident-audit")
    group_id = await _route_total_to_incident(client, seeded)

    resp = await client.post(seeded.verdict_url, json=seeded.total_scope(verdict="real_issue"))
    assert resp.status_code == 200, resp.text
    cleared = await client.delete(seeded.verdict_url, params=seeded.params(seeded.total_scope()))
    assert cleared.status_code == 204, cleared.text

    async with TestSessionLocal() as session:
        rows = list(
            (
                await session.execute(select(AuditLog).where(AuditLog.action.like("alert_inbox.%")))
            ).scalars()
        )
    assert sorted(str(row.action) for row in rows) == [
        "alert_inbox.acknowledge",
        "alert_inbox.reopen",
    ]
    for row in rows:
        assert row.target_type == "alert_correlation_group"
        assert row.target_id == group_id


# --- counts, activity, chart, audit --------------------------------------------


async def test_verdict_counts(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-counts")
    url = f"/api/v1/projects/{seeded.slug}/signals/verdict-counts"
    resp = await client.get(url)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "needs_verdict": 2,
        "expected": 0,
        "tracking_bug": 0,
        "false_positive": 0,
        "real_issue": 0,
    }
    await client.post(
        seeded.verdict_url, json=seeded.event_scope(verdict="expected", expected_reason="release")
    )
    await client.post(seeded.verdict_url, json=seeded.total_scope(verdict="real_issue"))
    resp = await client.get(url)
    assert resp.json() == {
        "needs_verdict": 0,
        "expected": 1,
        "tracking_bug": 0,
        "false_positive": 0,
        "real_issue": 1,
    }


async def test_verdict_shows_in_the_event_activity_feed_and_audit(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-activity")
    resp = await client.post(
        seeded.verdict_url, json=seeded.event_scope(verdict="tracking_bug", note="double fire")
    )
    assert resp.status_code == 200, resp.text
    await client.delete(seeded.verdict_url, params=seeded.params(seeded.event_scope()))

    history = await client.get(
        f"/api/v1/projects/{seeded.slug}/events/{seeded.ids['event_id']}/history"
    )
    assert history.status_code == 200, history.text
    entries = [entry for entry in history.json() if entry["field"] == "signal_verdict"]
    # Newest first: the clear, then the verdict.
    assert [(entry["old_value"], entry["new_value"]) for entry in entries] == [
        ("tracking_bug — double fire", None),
        (None, "tracking_bug — double fire"),
    ]
    async with TestSessionLocal() as session:
        actions = sorted(
            str(action)
            for action in (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.action.like("signal.%"))
                )
            ).scalars()
        )
    assert actions == ["signal.clear_verdict", "signal.verdict"]


async def test_chart_points_carry_the_verdict(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-chart")
    await client.post(seeded.verdict_url, json=seeded.event_scope(verdict="real_issue"))
    # A window around the seeded bucket: its metric row is the series head and
    # its anomaly the newest, so the anomaly is the open ``latest_signal``.
    resp = await client.get(
        f"/api/v1/projects/{seeded.slug}/events/{seeded.ids['event_id']}/metrics",
        params={
            "from": (_BUCKET - timedelta(days=1)).isoformat(),
            "to": (_BUCKET + timedelta(hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    flagged = [point for point in body["data"] if point["is_anomaly"]]
    assert flagged, body
    assert all(point["verdict"]["verdict"] == "real_issue" for point in flagged)
    # Unflagged points leave the key out entirely.
    assert all("verdict" not in point for point in body["data"] if not point["is_anomaly"])
    latest = body["latest_signal"]
    assert latest is not None, body
    assert latest["bucket"] is not None
    assert latest["verdict"]["verdict"] == "real_issue"
    assert latest["verdict"]["source"] == "signal"


# --- gates -----------------------------------------------------------------------


async def test_viewer_reads_but_cannot_set_verdicts(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-viewer")
    viewer = await persisted_member_user(uuid.UUID(seeded.ids["project_id"]), role="viewer")
    _as(viewer)
    try:
        posted = await client.post(seeded.verdict_url, json=seeded.event_scope(verdict="expected"))
        deleted = await client.delete(
            seeded.verdict_url, params=seeded.params(seeded.event_scope())
        )
        counts = await client.get(f"/api/v1/projects/{seeded.slug}/signals/verdict-counts")
        listed = await client.get(
            f"/api/v1/projects/{seeded.slug}/anomalies/signals",
            params={"expanded": "true", "needs_verdict": "true"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert (posted.status_code, deleted.status_code) == (403, 403)
    assert (counts.status_code, listed.status_code) == (200, 200)
    assert await _verdict_rows() == []


async def test_non_member_gets_404(client: AsyncClient) -> None:
    seeded = await _seed(client, "verdict-outsider")
    async with TestSessionLocal() as session:
        outsider = User(
            id=uuid.uuid4(),
            email=f"outsider-{uuid.uuid4().hex[:8]}@example.com",
            name="Outsider",
            password_hash=PASSWORD_HASH_PLACEHOLDER,
        )
        session.add(outsider)
        await session.commit()
    _as(outsider)
    try:
        posted = await client.post(
            seeded.verdict_url, json=seeded.event_scope(verdict="real_issue")
        )
        deleted = await client.delete(
            seeded.verdict_url, params=seeded.params(seeded.event_scope())
        )
        counts = await client.get(f"/api/v1/projects/{seeded.slug}/signals/verdict-counts")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert [posted.status_code, deleted.status_code, counts.status_code] == [404, 404, 404]
    assert await _verdict_rows() == []
