"""Triage on open signals no rule routed to an incident (MO-4 / JR-5).

Covers the three verdicts (acknowledge, mute, mark as expected) and their
undo: the editor gate, the 409 for a signal that is an incident, the NULL-space
uniqueness of the ``signal_triage`` key, the ``hidden`` flag on the expanded
list, the collapsed list and the sidebar / Overview badge leaving hidden
signals out, the chart annotation an "expected" verdict writes, mute expiry and
the audit row every write leaves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tripl.api.deps import get_current_user
from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import SignalTriageAction, UserRole
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.signal_triage import SignalTriage
from tripl.models.user import User
from tripl.services import alerting_service, signal_triage_service
from tripl.services.metrics_insights_service import get_active_signals
from tripl.tests._members import persisted_member_user
from tripl.tests.conftest import TestSessionLocal

pytestmark = pytest.mark.asyncio

# Recent and hour-aligned, so both seeded scopes classify as open signals.
_BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


class _Seeded:
    def __init__(self, slug: str, scan_config_id: str, event_type_id: str) -> None:
        self.slug = slug
        self.scan_config_id = scan_config_id
        self.event_type_id = event_type_id

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.slug}/anomalies/signals"

    def event_type_scope(self, **extra: Any) -> dict[str, Any]:
        return {
            "scan_config_id": self.scan_config_id,
            "scope_type": "event_type",
            "scope_ref": self.event_type_id,
            "bucket": _BUCKET.isoformat(),
            **extra,
        }

    def project_total_scope(self, **extra: Any) -> dict[str, Any]:
        return {
            "scan_config_id": self.scan_config_id,
            "scope_type": "project_total",
            "scope_ref": self.scan_config_id,
            "bucket": _BUCKET.isoformat(),
            **extra,
        }


async def _seed(client: AsyncClient, slug: str = "triage") -> _Seeded:
    """One scan with two open, significant signals: project_total and event_type."""
    assert (
        await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    ).status_code == 201
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
    assert data_source.status_code == 201
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "checkout", "display_name": "Checkout"},
    )
    assert event_type.status_code == 201
    scan = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source.json()["id"],
            "name": "Scan",
            "base_query": "SELECT 1",
        },
    )
    assert scan.status_code == 201
    scan_config_id = scan.json()["id"]
    event_type_id = event_type.json()["id"]

    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                scan_config_id=uuid.UUID(scan_config_id),
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=_BUCKET,
                count=42,
            )
        )
        for scope_type, scope_ref, et_id in (
            ("project_total", scan_config_id, None),
            ("event_type", event_type_id, uuid.UUID(event_type_id)),
        ):
            session.add(
                MetricAnomaly(
                    scan_config_id=uuid.UUID(scan_config_id),
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    event_id=None,
                    event_type_id=et_id,
                    bucket=_BUCKET,
                    actual_count=99,
                    expected_count=40,
                    stddev=5,
                    z_score=8,
                    direction="spike",
                )
            )
        await session.commit()
    return _Seeded(slug, scan_config_id, event_type_id)


def _open_incident() -> alerting_service.SignalIncidentRef:
    return alerting_service.SignalIncidentRef(correlation_group_id=uuid.uuid4(), status="open")


async def _badge(client: AsyncClient, slug: str) -> int:
    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200, resp.text
    return resp.json()["summary"]["monitoring_signal_count"]


async def _expanded(slug: str) -> dict[str, Any]:
    async with TestSessionLocal() as session:
        signals = await get_active_signals(session, slug, expanded=True)
    return {str(signal.scope_type): signal for signal in signals}


async def _collapsed_scopes(slug: str) -> set[str]:
    async with TestSessionLocal() as session:
        return {str(signal.scope_type) for signal in await get_active_signals(session, slug)}


async def _as_viewer(slug: str) -> None:
    # A persisted viewer MEMBER: a non-member would get 404, not 403.
    viewer = await persisted_member_user(await _project_id(slug), role=UserRole.viewer.value)

    async def _viewer() -> User:
        return viewer

    app.dependency_overrides[get_current_user] = _viewer


# --- gates -------------------------------------------------------------------


async def test_viewer_cannot_triage(client: AsyncClient) -> None:
    seeded = await _seed(client)
    await _as_viewer(seeded.slug)
    try:
        responses = [
            await client.post(f"{seeded.base}/acknowledge", json=seeded.event_type_scope()),
            await client.post(f"{seeded.base}/mute", json=seeded.event_type_scope(duration="24h")),
            await client.post(f"{seeded.base}/expected", json=seeded.event_type_scope()),
            await client.delete(
                f"{seeded.base}/mute",
                params={"scope_type": "event_type", "scope_ref": seeded.event_type_id},
            ),
        ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert [resp.status_code for resp in responses] == [403, 403, 403, 403]
    async with TestSessionLocal() as session:
        assert (await session.execute(select(SignalTriage))).scalars().all() == []


async def test_unknown_signal_is_404(client: AsyncClient) -> None:
    seeded = await _seed(client)
    body = seeded.event_type_scope(bucket=(_BUCKET - timedelta(days=3)).isoformat())
    resp = await client.post(f"{seeded.base}/acknowledge", json=body)
    assert resp.status_code == 404, resp.text


async def test_scope_shape_is_validated(client: AsyncClient) -> None:
    seeded = await _seed(client)
    missing_config = seeded.event_type_scope(scan_config_id=None)
    resp = await client.post(f"{seeded.base}/acknowledge", json=missing_config)
    assert resp.status_code == 422, resp.text
    drift = seeded.event_type_scope(scope_type="schema")
    resp = await client.post(f"{seeded.base}/acknowledge", json=drift)
    assert resp.status_code == 422, resp.text


async def test_signal_routed_to_an_incident_is_refused(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed(client)

    async def _routed(_session: object, _project_id: object, keys: Any) -> dict[Any, Any]:
        # An open incident: it carries no verdict (F01), so the signal stays counted.
        return {key: _open_incident() for key in keys}

    monkeypatch.setattr(alerting_service, "incident_refs_for_signals", _routed)
    resp = await client.post(f"{seeded.base}/expected", json=seeded.event_type_scope())
    assert resp.status_code == 409, resp.text
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ChartAnnotation))).scalars().all() == []


# --- acknowledge ---------------------------------------------------------------


async def test_acknowledge_keeps_the_signal_listed_and_counted(client: AsyncClient) -> None:
    seeded = await _seed(client)
    assert await _badge(client, seeded.slug) == 2

    resp = await client.post(f"{seeded.base}/acknowledge", json=seeded.event_type_scope())
    assert resp.status_code == 200, resp.text
    assert resp.json()["acknowledged_at"] is not None
    assert resp.json()["hidden"] is False

    signal = (await _expanded(seeded.slug))["event_type"]
    assert signal.acknowledged_at is not None
    assert signal.hidden is False
    assert await _badge(client, seeded.slug) == 2

    # Acknowledging twice is one verdict.
    again = await client.post(f"{seeded.base}/acknowledge", json=seeded.event_type_scope())
    assert again.status_code == 200
    async with TestSessionLocal() as session:
        assert len((await session.execute(select(SignalTriage))).scalars().all()) == 1

    undo = await client.delete(
        f"{seeded.base}/acknowledge",
        params={
            "scan_config_id": seeded.scan_config_id,
            "scope_type": "event_type",
            "scope_ref": seeded.event_type_id,
            "bucket": _BUCKET.isoformat(),
        },
    )
    assert undo.status_code == 204, undo.text
    assert (await _expanded(seeded.slug))["event_type"].acknowledged_at is None


# --- mute ----------------------------------------------------------------------


async def test_mute_hides_the_scope_from_lists_and_counts(client: AsyncClient) -> None:
    seeded = await _seed(client)
    resp = await client.post(f"{seeded.base}/mute", json=seeded.event_type_scope(duration="7d"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["muted"] is True
    assert body["hidden"] is True
    muted_until = datetime.fromisoformat(body["muted_until"])
    assert timedelta(days=6, hours=23) < muted_until - datetime.now(UTC) <= timedelta(days=7)

    # Expanded keeps it, flagged; collapsed and the badge leave it out.
    expanded = await _expanded(seeded.slug)
    assert expanded["event_type"].hidden is True
    assert expanded["event_type"].muted is True
    assert expanded["project_total"].hidden is False
    assert await _collapsed_scopes(seeded.slug) == {"project_total"}
    assert await _badge(client, seeded.slug) == 1

    undo = await client.delete(
        f"{seeded.base}/mute",
        params={
            "scan_config_id": seeded.scan_config_id,
            "scope_type": "event_type",
            "scope_ref": seeded.event_type_id,
        },
    )
    assert undo.status_code == 204, undo.text
    assert await _badge(client, seeded.slug) == 2
    # Undoing again is a no-op, not an error.
    again = await client.delete(
        f"{seeded.base}/mute",
        params={
            "scan_config_id": seeded.scan_config_id,
            "scope_type": "event_type",
            "scope_ref": seeded.event_type_id,
        },
    )
    assert again.status_code == 204


async def test_remuting_replaces_the_duration(client: AsyncClient) -> None:
    seeded = await _seed(client)
    first = await client.post(f"{seeded.base}/mute", json=seeded.event_type_scope(duration="24h"))
    assert first.status_code == 200
    second = await client.post(
        f"{seeded.base}/mute", json=seeded.event_type_scope(duration="until_unmuted")
    )
    assert second.status_code == 200
    assert second.json()["muted"] is True
    assert second.json()["muted_until"] is None
    async with TestSessionLocal() as session:
        rows = (await session.execute(select(SignalTriage))).scalars().all()
    assert len(rows) == 1
    assert rows[0].muted_until is None


async def test_lapsed_mute_no_longer_hides(client: AsyncClient) -> None:
    seeded = await _seed(client)
    resp = await client.post(f"{seeded.base}/mute", json=seeded.event_type_scope(duration="24h"))
    assert resp.status_code == 200
    assert await _badge(client, seeded.slug) == 1

    async with TestSessionLocal() as session:
        row = (await session.execute(select(SignalTriage))).scalar_one()
        row.muted_until = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    # The badge rides on the cached project summary; drop it like a write would.
    from tripl import cache

    await cache.delete_prefix(cache.prefix_projects())

    expanded = await _expanded(seeded.slug)
    assert expanded["event_type"].muted is False
    assert expanded["event_type"].hidden is False
    assert await _badge(client, seeded.slug) == 2


# --- mark as expected ------------------------------------------------------------


async def test_expected_writes_an_annotation_and_hides_one_signal(client: AsyncClient) -> None:
    seeded = await _seed(client)
    resp = await client.post(
        f"{seeded.base}/expected",
        json=seeded.project_total_scope(note="  Spring campaign launch  "),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["expected"] is True
    assert resp.json()["expected_note"] == "Spring campaign launch"
    assert resp.json()["hidden"] is True

    async with TestSessionLocal() as session:
        annotation = (await session.execute(select(ChartAnnotation))).scalar_one()
    assert annotation.scope_type == "project_total"
    assert annotation.scope_ref == seeded.scan_config_id
    assert annotation.description == "Spring campaign launch"

    expanded = await _expanded(seeded.slug)
    assert expanded["project_total"].hidden is True
    assert expanded["event_type"].hidden is False
    assert await _badge(client, seeded.slug) == 1

    # Re-marking edits the note in place rather than stacking a second marker.
    again = await client.post(
        f"{seeded.base}/expected", json=seeded.project_total_scope(note="Launch")
    )
    assert again.status_code == 200
    async with TestSessionLocal() as session:
        annotations = (await session.execute(select(ChartAnnotation))).scalars().all()
    assert [a.description for a in annotations] == ["Launch"]

    undo = await client.delete(
        f"{seeded.base}/expected",
        params={
            "scan_config_id": seeded.scan_config_id,
            "scope_type": "project_total",
            "scope_ref": seeded.scan_config_id,
            "bucket": _BUCKET.isoformat(),
        },
    )
    assert undo.status_code == 204, undo.text
    async with TestSessionLocal() as session:
        assert (await session.execute(select(ChartAnnotation))).scalars().all() == []
        assert (await session.execute(select(SignalTriage))).scalars().all() == []
    assert await _badge(client, seeded.slug) == 2


async def test_verdict_on_a_signal_later_routed_stays_counted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal that became an incident is the inbox's: its old verdict is ignored."""
    seeded = await _seed(client)
    resp = await client.post(f"{seeded.base}/mute", json=seeded.event_type_scope(duration="7d"))
    assert resp.status_code == 200
    assert await _badge(client, seeded.slug) == 1

    async def _routed(_session: object, _project_id: object, keys: Any) -> dict[Any, Any]:
        # An open incident: it carries no verdict (F01), so the signal stays counted.
        return {key: _open_incident() for key in keys}

    monkeypatch.setattr(alerting_service, "incident_refs_for_signals", _routed)
    from tripl import cache

    await cache.delete_prefix(cache.prefix_projects())
    assert await _badge(client, seeded.slug) == 2


# --- audit -----------------------------------------------------------------------


async def test_every_write_is_audited(client: AsyncClient) -> None:
    seeded = await _seed(client)
    params = {
        "scan_config_id": seeded.scan_config_id,
        "scope_type": "event_type",
        "scope_ref": seeded.event_type_id,
    }
    with_bucket = {**params, "bucket": _BUCKET.isoformat()}
    calls = [
        client.post(f"{seeded.base}/acknowledge", json=seeded.event_type_scope()),
        client.delete(f"{seeded.base}/acknowledge", params=with_bucket),
        client.post(f"{seeded.base}/mute", json=seeded.event_type_scope(duration="24h")),
        client.delete(f"{seeded.base}/mute", params=params),
        client.post(f"{seeded.base}/expected", json=seeded.event_type_scope(note="n")),
        client.delete(f"{seeded.base}/expected", params=with_bucket),
    ]
    for call in calls:
        assert (await call).status_code in (200, 204)
    async with TestSessionLocal() as session:
        actions = (
            (
                await session.execute(
                    select(AuditLog.action)
                    .where(AuditLog.target_type == "signal")
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert sorted(actions) == sorted(
        [
            "signal.acknowledge",
            "signal.unacknowledge",
            "signal.mute",
            "signal.unmute",
            "signal.mark_expected",
            "signal.unmark_expected",
        ]
    )


# --- NULL-space uniqueness ----------------------------------------------------------


async def _project_id(slug: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        return (await session.execute(select(Project.id).where(Project.slug == slug))).scalar_one()


async def _insert(row: SignalTriage) -> None:
    async with TestSessionLocal() as session:
        session.add(row)
        await session.commit()


async def test_metric_scope_verdict_is_unique_despite_null_scan_config(
    client: AsyncClient,
) -> None:
    await _seed(client)
    project_id = await _project_id("triage")
    metric_ref = str(uuid.uuid4())

    def _expected() -> SignalTriage:
        return SignalTriage(
            project_id=project_id,
            scan_config_id=None,
            scope_type="metric",
            scope_ref=metric_ref,
            action=SignalTriageAction.expected.value,
            bucket=_BUCKET,
        )

    await _insert(_expected())
    with pytest.raises(IntegrityError):
        await _insert(_expected())


async def test_one_mute_per_scope_in_both_null_spaces(client: AsyncClient) -> None:
    seeded = await _seed(client)
    project_id = await _project_id(seeded.slug)
    metric_ref = str(uuid.uuid4())

    def _mute(scan_config_id: uuid.UUID | None, scope_type: str, scope_ref: str) -> SignalTriage:
        return SignalTriage(
            project_id=project_id,
            scan_config_id=scan_config_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            action=SignalTriageAction.muted.value,
            bucket=None,
        )

    scan_config_id = uuid.UUID(seeded.scan_config_id)
    await _insert(_mute(scan_config_id, "event_type", seeded.event_type_id))
    with pytest.raises(IntegrityError):
        await _insert(_mute(scan_config_id, "event_type", seeded.event_type_id))

    await _insert(_mute(None, "metric", metric_ref))
    with pytest.raises(IntegrityError):
        await _insert(_mute(None, "metric", metric_ref))


async def test_a_mute_carries_no_bucket(client: AsyncClient) -> None:
    seeded = await _seed(client)
    project_id = await _project_id(seeded.slug)
    with pytest.raises(IntegrityError):
        await _insert(
            SignalTriage(
                project_id=project_id,
                scan_config_id=uuid.UUID(seeded.scan_config_id),
                scope_type="event_type",
                scope_ref=seeded.event_type_id,
                action=SignalTriageAction.muted.value,
                bucket=_BUCKET,
            )
        )


async def test_uncounted_signal_keys_is_empty_without_verdicts(client: AsyncClient) -> None:
    seeded = await _seed(client)
    project_id = await _project_id(seeded.slug)
    key = signal_triage_service.signal_key(
        uuid.UUID(seeded.scan_config_id), "event_type", seeded.event_type_id, _BUCKET
    )
    async with TestSessionLocal() as session:
        assert await signal_triage_service.uncounted_signal_keys(session, {project_id: [key]}) == {}
