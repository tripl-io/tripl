import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tripl.models import Base
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import AnomalyDirection, MetricScopeType
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import (
    DEFAULT_SIGMA_THRESHOLD,
    ProjectAnomalySettings,
)
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.models.user import User
from tripl.services._alerting_deliveries import InboxFilters
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import metrics
from tripl.worker.tasks.alerts import check_deprecated_sunset_events


@pytest.mark.parametrize(
    ("message_format", "expected_mrkdwn"),
    [
        ("plain", False),
        ("slack_mrkdwn", True),
    ],
)
def test_send_slack_message_sets_mrkdwn_per_format(
    monkeypatch: pytest.MonkeyPatch, message_format: str, expected_mrkdwn: bool
) -> None:
    """The Slack payload must honor message_format via the ``mrkdwn`` flag:
    plain disables Slack markup, slack_mrkdwn enables it."""
    from tripl.worker.tasks import alerts

    captured: dict[str, object] = {}

    def capture_post_json(url: str, body: dict[str, object]) -> None:
        captured["url"] = url
        captured["body"] = body

    monkeypatch.setattr(alerts, "_post_json", capture_post_json)
    alerts._send_slack_message(
        "https://hooks.slack.com/services/T/B/sim",
        "hello",
        message_format=message_format,
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["text"] == "hello"
    assert body["mrkdwn"] is expected_mrkdwn


@pytest.mark.asyncio
async def test_schema_drift_accept_action_updates_plan(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/projects",
        json={"name": "Drift Workflow", "slug": "drift-workflow", "description": ""},
    )
    event_type_resp = await client.post(
        "/api/v1/projects/drift-workflow/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    event_type_id = event_type_resp.json()["id"]
    async with TestSessionLocal() as session:
        drift = SchemaDrift(
            event_type_id=uuid.UUID(event_type_id),
            scan_config_id=None,
            field_name="payload",
            drift_type="new_field",
            observed_type="JSON",
            declared_type=None,
            sample_value='{"x":1}',
        )
        session.add(drift)
        await session.commit()
        drift_id = drift.id

    resp = await client.post(
        f"/api/v1/projects/drift-workflow/event-types/drifts/{drift_id}/actions",
        json={"action": "accept", "note": "Looks expected"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    async with TestSessionLocal() as session:
        field = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == uuid.UUID(event_type_id),
                FieldDefinition.name == "payload",
            )
        )
        assert field is not None
        assert field.field_type == "json"


@pytest.mark.asyncio
async def test_alert_inbox_false_positive_updates_state_and_thresholds(
    client: AsyncClient,
) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Inbox Workflow", "slug": "inbox-workflow", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    group_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        event_type = EventType(
            project_id=project_id,
            name="track",
            display_name="Track",
            description="",
        )
        data_source = DataSource(
            name="Warehouse",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add_all([event_type, data_source])
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            event_type_id=event_type.id,
            name="Events",
            base_query="SELECT * FROM events",
            time_column="time",
            interval="1h",
            sigma_threshold=3.0,
            min_expected_count=10,
        )
        destination = AlertDestination(
            project_id=project_id,
            type="slack",
            name="Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        session.add_all([config, destination])
        await session.flush()
        rule = AlertRule(
            destination_id=destination.id,
            name="Rule",
            enabled=True,
            include_project_total=True,
            include_event_types=True,
            include_events=True,
            notify_on_spike=True,
            notify_on_drop=True,
            min_percent_delta=0,
            min_absolute_delta=0,
            min_expected_count=0,
            cooldown_minutes=60,
        )
        settings = ProjectAnomalySettings(
            project_id=project_id,
            anomaly_detection_enabled=True,
            sigma_threshold=3.0,
            min_expected_count=10,
        )
        session.add_all([rule, settings])
        await session.flush()
        delivery = AlertDelivery(
            project_id=project_id,
            scan_config_id=config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            status="sent",
            channel="slack",
            matched_count=2,
        )
        session.add(delivery)
        await session.flush()
        for name in ("purchase", "refund"):
            session.add(
                AlertDeliveryItem(
                    delivery_id=delivery.id,
                    scope_type="event_type",
                    scope_ref=str(event_type.id),
                    scope_name=name,
                    event_type_id=event_type.id,
                    event_id=None,
                    bucket=datetime(2026, 1, 1, tzinfo=UTC),
                    direction="spike",
                    actual_count=20,
                    expected_count=10,
                    absolute_delta=10,
                    percent_delta=100.0,
                    correlation_group_id=group_id,
                )
            )
        await session.commit()
        config_id = config.id

    list_resp = await client.get("/api/v1/projects/inbox-workflow/alert-inbox")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    action_resp = await client.post(
        f"/api/v1/projects/inbox-workflow/alert-inbox/{group_id}/actions",
        json={"action": "false_positive", "note": "Noisy deploy window"},
    )

    assert action_resp.status_code == 200
    # The action reports what it DID alongside the group: two event_type rows
    # collapse to one ratchetable scope, so exactly one override was written.
    assert action_resp.json()["group"]["status"] == "false_positive"
    assert action_resp.json()["overrides_written"] == 1
    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        assert state is not None
        assert state.false_positive_count == 1
        # The ratchet is per scope now: the scan keeps the sensitivity the
        # operator gave it and the dismissed event type gets its own override.
        config = await session.get(ScanConfig, config_id)
        assert config is not None
        assert config.sigma_threshold == 3.0
        assert config.min_expected_count == 10
        override = await session.scalar(
            select(AnomalyScopeOverride).where(AnomalyScopeOverride.scan_config_id == config_id)
        )
        assert override is not None
        assert override.scope_type == "event_type"
        assert override.sigma_threshold == 3.5
        assert override.min_expected_count == 15

    # A second action carrying no note must not erase the first one's. The
    # assignment was unconditional, so every follow-up action silently wiped the
    # note written with the previous one.
    await client.post(
        f"/api/v1/projects/inbox-workflow/alert-inbox/{group_id}/actions",
        json={"action": "acknowledge"},
    )
    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        assert state is not None
        assert state.status == "acknowledged"
        assert state.note == "Noisy deploy window"

    # An explicit new note still replaces it.
    await client.post(
        f"/api/v1/projects/inbox-workflow/alert-inbox/{group_id}/actions",
        json={"action": "resolve", "note": "Rolled back"},
    )
    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        assert state is not None
        assert state.note == "Rolled back"


@pytest.mark.asyncio
async def test_alert_inbox_false_positive_ratchets_only_the_marked_scope(
    client: AsyncClient,
) -> None:
    """One "false positive" click tightens the scope it was clicked on — and
    nothing else.

    The ratchet used to raise ``sigma_threshold`` / ``min_expected_count`` on the
    project's monitoring settings AND on every scan the group touched, so
    dismissing one noisy event made the detector stricter on every other event,
    event type, project total and catalog metric in the project. Per-scope
    correlation groups made that button easy to reach, so the
    blast radius had to shrink to the scope that was actually dismissed.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Scoped Ratchet", "slug": "scoped-ratchet", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    group_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        event_type = EventType(
            project_id=project_id,
            name="track",
            display_name="Track",
            description="",
        )
        data_source = DataSource(
            name="Warehouse",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add_all([event_type, data_source])
        await session.flush()
        noisy = Event(
            project_id=project_id,
            event_type_id=event_type.id,
            name="noisy_event",
            description="",
            status=EventStatus.implemented.value,
        )
        quiet = Event(
            project_id=project_id,
            event_type_id=event_type.id,
            name="quiet_event",
            description="",
            status=EventStatus.implemented.value,
        )
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            event_type_id=event_type.id,
            name="Events",
            base_query="SELECT * FROM events",
            time_column="time",
            interval="1h",
            sigma_threshold=3.0,
            min_expected_count=10,
        )
        destination = AlertDestination(
            project_id=project_id,
            type="slack",
            name="Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        session.add_all([noisy, quiet, config, destination])
        await session.flush()
        rule = AlertRule(
            destination_id=destination.id,
            name="Rule",
            enabled=True,
            include_events=True,
            notify_on_spike=True,
            notify_on_drop=True,
            min_percent_delta=0,
            min_absolute_delta=0,
            min_expected_count=0,
            cooldown_minutes=60,
        )
        settings = ProjectAnomalySettings(
            project_id=project_id,
            anomaly_detection_enabled=True,
            sigma_threshold=3.0,
            min_expected_count=10,
        )
        session.add_all([rule, settings])
        await session.flush()
        delivery = AlertDelivery(
            project_id=project_id,
            scan_config_id=config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            status="sent",
            channel="slack",
            matched_count=1,
        )
        session.add(delivery)
        await session.flush()
        session.add(
            AlertDeliveryItem(
                delivery_id=delivery.id,
                scope_type="event",
                scope_ref=str(noisy.id),
                scope_name="noisy_event",
                event_type_id=None,
                event_id=noisy.id,
                bucket=datetime(2026, 1, 1, tzinfo=UTC),
                direction="spike",
                actual_count=20,
                expected_count=10,
                absolute_delta=10,
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        )
        await session.commit()
        config_id = config.id
        noisy_id = noisy.id
        quiet_id = quiet.id

    action_resp = await client.post(
        f"/api/v1/projects/scoped-ratchet/alert-inbox/{group_id}/actions",
        json={"action": "false_positive"},
    )
    assert action_resp.status_code == 200

    async with TestSessionLocal() as session:
        overrides = list(
            (
                await session.execute(
                    select(AnomalyScopeOverride).where(
                        AnomalyScopeOverride.project_id == project_id
                    )
                )
            ).scalars()
        )
        # Exactly one override, on the dismissed scope, keyed the way the
        # anomaly keys itself.
        assert len(overrides) == 1
        override = overrides[0]
        assert override.scan_config_id == config_id
        assert override.scope_type == "event"
        assert override.scope_ref == str(noisy_id)
        assert override.scope_ref != str(quiet_id)
        assert override.sigma_threshold == 3.5
        assert override.min_expected_count == 15
        assert override.false_positive_count == 1

        # The project-wide knobs the ratchet used to move are untouched, so
        # every other scope keeps the sensitivity the operator chose.
        project_settings = await session.scalar(
            select(ProjectAnomalySettings).where(ProjectAnomalySettings.project_id == project_id)
        )
        assert project_settings is not None
        assert project_settings.sigma_threshold == 3.0
        assert project_settings.min_expected_count == 10
        config = await session.get(ScanConfig, config_id)
        assert config is not None
        assert config.sigma_threshold == 3.0
        assert config.min_expected_count == 10

    # A second click on the same group ratchets the SAME row further rather
    # than stacking duplicates.
    await client.post(
        f"/api/v1/projects/scoped-ratchet/alert-inbox/{group_id}/actions",
        json={"action": "false_positive"},
    )
    async with TestSessionLocal() as session:
        overrides = list(
            (
                await session.execute(
                    select(AnomalyScopeOverride).where(
                        AnomalyScopeOverride.project_id == project_id
                    )
                )
            ).scalars()
        )
        assert len(overrides) == 1
        assert overrides[0].sigma_threshold == 4.0
        assert overrides[0].min_expected_count == 20
        assert overrides[0].false_positive_count == 2


def test_every_drift_type_the_pipeline_writes_exists_in_the_enum() -> None:
    """alert_delivery_items.drift_type is a NATIVE Postgres enum.

    A value the candidate builders write but the type does not contain fails the
    INSERT, and since dispatch runs inside collect_metrics the whole collection
    transaction dies with it. That is exactly what 'value_drift' did: the scope
    shipped in d1c2b3a4f5e6, which extended metric_scope_type and forgot
    alert_drift_type.

    SQLite stores enums as unvalidated text, so no behavioural test on this
    suite can catch it — hence checking the literals against the enum directly.
    """
    import re
    from pathlib import Path

    from tripl.models.domain_enums import AlertDriftType

    source = Path(__file__).resolve().parents[1] / "worker" / "tasks" / "metrics" / "signals.py"
    written = set(re.findall(r'drift_type=["\']([a-z_]+)["\']', source.read_text()))
    assert written, "expected to find literal drift_type assignments to check"

    known = {member.value for member in AlertDriftType}
    assert written <= known, f"drift_type values with no enum member: {sorted(written - known)}"


def test_delivery_errors_never_carry_the_destination_secret() -> None:
    """A failed delivery's error text reaches the API and the UI verbatim.

    Masking used to be a Telegram-shaped regex, so a Slack incoming-webhook URL
    — which IS the credential — was written to alert_deliveries.error_message in
    full and readable by any project member.
    """
    from tripl.worker.tasks.alerts_channels import _safe_url_for_error

    slack = "https://hooks.slack.com/services/T00000000/B00000000/abcdef123456SECRET"
    assert _safe_url_for_error(slack) == "https://hooks.slack.com"

    telegram = "https://api.telegram.org/bot123456:AAH-TOKEN/sendMessage"
    assert _safe_url_for_error(telegram) == "https://api.telegram.org"

    # Tokens hide in the query string just as often as in the path.
    webhook = "https://hooks.example.com/ingest?signature=deadbeef"
    assert _safe_url_for_error(webhook) == "https://hooks.example.com"

    # Nothing parseable: say so rather than echoing whatever was passed.
    assert _safe_url_for_error("not-a-url") == "the destination URL"


@pytest.mark.asyncio
async def test_alerting_destination_rule_crud_and_secret_masking(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Alerting Project", "slug": "alerting-project", "description": ""},
    )
    assert project_resp.status_code == 201

    event_type_resp = await client.post(
        "/api/v1/projects/alerting-project/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    assert event_type_resp.status_code == 201
    event_type_id = event_type_resp.json()["id"]

    field_resp = await client.post(
        f"/api/v1/projects/alerting-project/event-types/{event_type_id}/fields",
        json={
            "name": "name",
            "display_name": "Name",
            "field_type": "string",
            "is_required": True,
        },
    )
    assert field_resp.status_code == 201
    field_id = field_resp.json()["id"]

    event_resp = await client.post(
        "/api/v1/projects/alerting-project/events",
        json={
            "event_type_id": event_type_id,
            "name": "purchase:success",
            "field_values": [{"field_definition_id": field_id, "value": "purchase:success"}],
        },
    )
    assert event_resp.status_code == 201
    event_id = event_resp.json()["id"]

    destination_resp = await client.post(
        "/api/v1/projects/alerting-project/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination_resp.status_code == 201
    destination = destination_resp.json()
    assert destination["type"] == "slack"
    assert destination["webhook_set"] is True
    assert "webhook_url" not in destination
    destination_id = destination["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/alerting-project/alert-destinations/{destination_id}/rules",
        json={
            "name": "Main Rule",
            "enabled": True,
            "include_project_total": True,
            "include_event_types": True,
            "include_events": True,
            "notify_on_spike": True,
            "notify_on_drop": False,
            "min_percent_delta": 15,
            "min_absolute_delta": 5,
            "min_expected_count": 10,
            "cooldown_minutes": 60,
            "message_format": "slack_mrkdwn",
            "message_template": "*Matched:* ${matched_count}\n${items_text}",
            "items_template": "*${scope_name}* ${actual_count}/${expected_count}",
            "filters": [
                {"field": "event_type", "operator": "not_in", "values": [event_type_id]},
                {"field": "event", "operator": "not_in", "values": [event_id]},
            ],
        },
    )
    assert rule_resp.status_code == 201
    rule = rule_resp.json()
    assert rule["notify_on_drop"] is False
    assert rule["message_format"] == "slack_mrkdwn"
    assert rule["message_template"] == "*Matched:* ${matched_count}\n${items_text}"
    assert rule["items_template"] == "*${scope_name}* ${actual_count}/${expected_count}"
    assert rule["filters"] == [
        {
            "field": "event_type",
            "operator": "not_in",
            "values": [event_type_id],
            "id": rule["filters"][0]["id"],
        },
        {
            "field": "event",
            "operator": "not_in",
            "values": [event_id],
            "id": rule["filters"][1]["id"],
        },
    ]

    list_resp = await client.get("/api/v1/projects/alerting-project/alert-destinations")
    assert list_resp.status_code == 200
    listed = list_resp.json()
    assert len(listed) == 1
    assert listed[0]["rules"][0]["id"] == rule["id"]

    update_resp = await client.patch(
        f"/api/v1/projects/alerting-project/alert-destinations/{destination_id}",
        json={"enabled": False},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["enabled"] is False


async def _seed_scan_config(project_id: uuid.UUID, name: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"{name} DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name=name,
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        await session.commit()
        return scan_config.id


@pytest.mark.asyncio
async def test_alert_rule_can_be_narrowed_to_one_scan(client: AsyncClient) -> None:
    """The HTTP surface round-trips ``scan_config_id`` and validates it.

    NULL means "every scan in the project" — today's behaviour and the default,
    so an existing rule keeps working untouched.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Scan Bound", "slug": "scan-bound", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    other_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Other Project", "slug": "scan-bound-other", "description": ""},
    )
    assert other_resp.status_code == 201
    foreign_scan_id = await _seed_scan_config(uuid.UUID(other_resp.json()["id"]), "Foreign Scan")

    scan_id = await _seed_scan_config(project_id, "Old events (iOS)")

    destination_resp = await client.post(
        "/api/v1/projects/scan-bound/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    # Default: unbound, i.e. the whole project.
    default_resp = await client.post(
        f"/api/v1/projects/scan-bound/alert-destinations/{destination_id}/rules",
        json={"name": "Project rule"},
    )
    assert default_resp.status_code == 201
    assert default_resp.json()["scan_config_id"] is None

    created_resp = await client.post(
        f"/api/v1/projects/scan-bound/alert-destinations/{destination_id}/rules",
        json={"name": "iOS only", "scan_config_id": str(scan_id)},
    )
    assert created_resp.status_code == 201
    rule = created_resp.json()
    assert rule["scan_config_id"] == str(scan_id)

    # A scan from another project is not addressable from here.
    foreign_resp = await client.post(
        f"/api/v1/projects/scan-bound/alert-destinations/{destination_id}/rules",
        json={"name": "Cross project", "scan_config_id": str(foreign_scan_id)},
    )
    assert foreign_resp.status_code == 404

    foreign_patch = await client.patch(
        f"/api/v1/projects/scan-bound/alert-destinations/{destination_id}/rules/{rule['id']}",
        json={"scan_config_id": str(foreign_scan_id)},
    )
    assert foreign_patch.status_code == 404

    # An explicit null widens the rule back to the project.
    widened = await client.patch(
        f"/api/v1/projects/scan-bound/alert-destinations/{destination_id}/rules/{rule['id']}",
        json={"scan_config_id": None},
    )
    assert widened.status_code == 200
    assert widened.json()["scan_config_id"] is None


@pytest.mark.asyncio
async def test_deleting_a_scan_disables_the_rules_bound_to_it(client: AsyncClient) -> None:
    """A deleted scan must neither destroy nor silently widen its rules.

    CASCADE would take the rule's name, thresholds, templates and filters with
    it (and its delivery history through ``AlertDelivery.rule_id``). A bare SET
    NULL would re-point a rule that was deliberately narrowed to the noisiest
    scan at the whole project, so deleting that scan would START paging on every
    other one. The rule is therefore kept, unbound AND disabled: visible in the
    UI, inert until someone re-aims it.
    """
    from tripl.services.scan_service import delete_scan_config

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Scan Delete", "slug": "scan-delete", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_id = await _seed_scan_config(project_id, "Doomed Scan")

    destination_resp = await client.post(
        "/api/v1/projects/scan-delete/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    bound_resp = await client.post(
        f"/api/v1/projects/scan-delete/alert-destinations/{destination_id}/rules",
        json={"name": "Bound rule", "scan_config_id": str(scan_id)},
    )
    assert bound_resp.status_code == 201
    bound_rule_id = uuid.UUID(bound_resp.json()["id"])

    untouched_resp = await client.post(
        f"/api/v1/projects/scan-delete/alert-destinations/{destination_id}/rules",
        json={"name": "Project rule"},
    )
    assert untouched_resp.status_code == 201
    untouched_rule_id = uuid.UUID(untouched_resp.json()["id"])

    async with TestSessionLocal() as session:
        await delete_scan_config(session, "scan-delete", scan_id)

    async with TestSessionLocal() as session:
        bound = await session.get(AlertRule, bound_rule_id)
        assert bound is not None, "deleting a scan must not delete its alert rule"
        assert bound.scan_config_id is None
        assert bound.enabled is False, "an orphaned rule must not widen to the project"

        untouched = await session.get(AlertRule, untouched_rule_id)
        assert untouched is not None
        assert untouched.enabled is True


@pytest.mark.asyncio
async def test_deleting_a_data_source_disables_the_rules_bound_to_its_scans(
    client: AsyncClient,
) -> None:
    """The other way a scan config dies, and the one that skipped the unbind.

    ``DataSource.scan_configs`` is delete-orphan, so removing a source takes its
    scans with it WITHOUT going through ``delete_scan_config``. The FK is ON
    DELETE SET NULL and NULL means "the whole project", so a rule narrowed to the
    noisiest scan would come back re-aimed at every other scan the moment its
    source was removed — paging on exactly what the operator was silencing.
    """
    from tripl.services.datasource_service import delete_data_source

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "DS Delete", "slug": "ds-delete", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_id = await _seed_scan_config(project_id, "Doomed Scan")

    async with TestSessionLocal() as session:
        seeded = await session.get(ScanConfig, scan_id)
        assert seeded is not None
        data_source_id = seeded.data_source_id

    destination_resp = await client.post(
        "/api/v1/projects/ds-delete/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    bound_resp = await client.post(
        f"/api/v1/projects/ds-delete/alert-destinations/{destination_id}/rules",
        json={"name": "Bound rule", "scan_config_id": str(scan_id)},
    )
    assert bound_resp.status_code == 201
    bound_rule_id = uuid.UUID(bound_resp.json()["id"])

    async with TestSessionLocal() as session:
        await delete_data_source(session, data_source_id)

    async with TestSessionLocal() as session:
        assert await session.get(ScanConfig, scan_id) is None, "the cascade did not run"
        bound = await session.get(AlertRule, bound_rule_id)
        assert bound is not None, "deleting a data source must not delete its alert rule"
        assert bound.enabled is False, "an orphaned rule must not widen to the project"


@pytest.mark.asyncio
async def test_alerting_destination_validates_credentials(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Alert Validation", "slug": "alert-validation", "description": ""},
    )
    assert project_resp.status_code == 201

    telegram_resp = await client.post(
        "/api/v1/projects/alert-validation/alert-destinations",
        json={
            "type": "telegram",
            "name": "Ops Bot",
            "enabled": True,
            "bot_token": "123456:abc def",
            "chat_id": "-100123",
        },
    )
    assert telegram_resp.status_code == 422
    assert "bot_token" in telegram_resp.text

    slack_resp = await client.post(
        "/api/v1/projects/alert-validation/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert slack_resp.status_code == 201
    destination_id = slack_resp.json()["id"]

    update_resp = await client.patch(
        f"/api/v1/projects/alert-validation/alert-destinations/{destination_id}",
        json={"webhook_url": "https://hooks.slack.com/services/T000 /B000/XXX"},
    )
    assert update_resp.status_code == 422
    assert "webhook_url" in update_resp.text


@pytest.mark.asyncio
async def test_alerting_webhook_destination_crud_and_validation(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Webhook Alerts", "slug": "webhook-alerts", "description": ""},
    )
    assert project_resp.status_code == 201

    # Non-https target rejected.
    bad_url_resp = await client.post(
        "/api/v1/projects/webhook-alerts/alert-destinations",
        json={"type": "webhook", "name": "Bad", "target_url": "http://example.com/hook"},
    )
    assert bad_url_resp.status_code == 422
    assert "target_url" in bad_url_resp.text

    # A secret header requires both name and value.
    pair_resp = await client.post(
        "/api/v1/projects/webhook-alerts/alert-destinations",
        json={
            "type": "webhook",
            "name": "Pair",
            "target_url": "https://example.com/hook",
            "webhook_header_name": "Authorization",
        },
    )
    assert pair_resp.status_code == 422

    create_resp = await client.post(
        "/api/v1/projects/webhook-alerts/alert-destinations",
        json={
            "type": "webhook",
            "name": "Ops Webhook",
            "target_url": "https://example.com/hook",
            "webhook_header_name": "Authorization",
            "webhook_header_value": "Bearer secret",
        },
    )
    assert create_resp.status_code == 201
    destination = create_resp.json()
    assert destination["type"] == "webhook"
    assert destination["target_url_set"] is True
    assert destination["webhook_header_name"] == "Authorization"
    # Secrets are never echoed back.
    assert "target_url" not in destination
    assert "webhook_header_value" not in destination
    destination_id = destination["id"]

    bad_update = await client.patch(
        f"/api/v1/projects/webhook-alerts/alert-destinations/{destination_id}",
        json={"target_url": "ftp://example.com/hook"},
    )
    assert bad_update.status_code == 422
    assert "target_url" in bad_update.text

    good_update = await client.patch(
        f"/api/v1/projects/webhook-alerts/alert-destinations/{destination_id}",
        json={"target_url": "https://example.com/hook2", "name": "Renamed"},
    )
    assert good_update.status_code == 200
    assert good_update.json()["name"] == "Renamed"
    assert good_update.json()["target_url_set"] is True


@pytest.mark.asyncio
async def test_alert_deliveries_filter_by_incident_and_reach_the_ungrouped(
    client: AsyncClient,
) -> None:
    """Deliveries are listed under the incident they belong to, and the ones with
    no incident stay reachable.

    The alerting page folds "what was sent" into the incident card so the actions
    sit next to the alert instead of in a second panel further up.
    That needs a per-incident query — and, because ``correlation_group_id`` is
    nullable, a way to ask for the rows no incident id can select. Without the
    second filter, nesting would quietly drop every ungrouped delivery (rows that
    predate correlation, at minimum) and the audit trail would be incomplete
    while looking complete.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Incident Nesting", "slug": "incident-nesting", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = project_resp.json()["id"]
    group_id = uuid.uuid4()

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Nesting DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(project_id),
            name="Nesting Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=uuid.UUID(project_id),
            type="slack",
            name="Nesting Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Nesting Rule",
            enabled=True,
        )

        # The incident lives on the ITEM, not the delivery — one message can carry
        # rows from several incidents — so each fixture is a delivery plus the one
        # item that places it.
        def _delivery(preview: str) -> AlertDelivery:
            return AlertDelivery(
                id=uuid.uuid4(),
                project_id=uuid.UUID(project_id),
                scan_config_id=scan_config.id,
                destination_id=destination.id,
                rule_id=rule.id,
                channel="slack",
                status="sent",
                matched_count=1,
                payload_snapshot={"preview": preview},
                sent_at=datetime(2026, 4, 11, 10, tzinfo=UTC),
            )

        def _item(
            delivery: AlertDelivery, correlation_group_id: uuid.UUID | None
        ) -> AlertDeliveryItem:
            return AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type="event",
                scope_ref="event-1",
                scope_name="purchase:success",
                event_id=None,
                event_type_id=None,
                bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
                direction="drop",
                actual_count=10,
                expected_count=20,
                absolute_delta=10,
                percent_delta=50,
                correlation_group_id=correlation_group_id,
            )

        grouped = _delivery("belongs to the incident")
        other_incident = _delivery("a different incident")
        orphan = _delivery("no incident at all")

        session.add_all([data_source, scan_config, destination, rule])
        await session.flush()
        session.add_all([grouped, other_incident, orphan])
        await session.flush()
        session.add_all(
            [
                _item(grouped, group_id),
                _item(other_incident, uuid.uuid4()),
                # Pre-tripl-jfm3.91 shape: an item with no incident at all.
                _item(orphan, None),
            ]
        )
        await session.commit()

    unfiltered = await client.get("/api/v1/projects/incident-nesting/alert-deliveries")
    assert unfiltered.status_code == 200
    assert unfiltered.json()["total"] == 3

    scoped = await client.get(
        "/api/v1/projects/incident-nesting/alert-deliveries",
        params={"correlation_group_id": str(group_id)},
    )
    assert scoped.status_code == 200
    scoped_body = scoped.json()
    assert scoped_body["total"] == 1
    assert scoped_body["items"][0]["payload_snapshot"]["preview"] == "belongs to the incident"

    ungrouped = await client.get(
        "/api/v1/projects/incident-nesting/alert-deliveries",
        params={"ungrouped": "true"},
    )
    assert ungrouped.status_code == 200
    ungrouped_body = ungrouped.json()
    assert ungrouped_body["total"] == 1
    assert ungrouped_body["items"][0]["payload_snapshot"]["preview"] == "no incident at all"

    # Asking for both is contradictory — a delivery in the group necessarily has
    # a grouped item, which `ungrouped` excludes — so it can only ever match zero
    # rows. Say so, rather than returning an empty list a caller would read as
    # "this incident sent nothing".
    conflicting = await client.get(
        "/api/v1/projects/incident-nesting/alert-deliveries",
        params={"correlation_group_id": str(group_id), "ungrouped": "true"},
    )
    assert conflicting.status_code == 422
    assert "mutually exclusive" in str(conflicting.json()["detail"])


async def test_alert_delivery_list_and_detail(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Alert Audit", "slug": "alert-audit", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = project_resp.json()["id"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Audit DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(project_id),
            name="Audit Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=uuid.UUID(project_id),
            type="slack",
            name="Audit Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Audit Rule",
            enabled=True,
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=uuid.UUID(project_id),
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="slack",
            status="sent",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
            sent_at=datetime(2026, 4, 11, 10, tzinfo=UTC),
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            event_id=None,
            event_type_id=None,
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path="http://localhost:5173/p/alert-audit/monitoring/event/event-1",
            monitoring_path="http://localhost:5173/p/alert-audit/monitoring/event/event-1",
        )
        # Flush each FK level before its children (parents -> delivery -> item);
        # the unit of work has no ORM relationship ordering them, so a single
        # flush inserts children first and trips the FK under SQLite.
        session.add_all([data_source, scan_config, destination, rule])
        await session.flush()
        session.add(delivery)
        await session.flush()
        session.add(item)
        await session.commit()
        delivery_id = str(delivery.id)
        destination_id = str(destination.id)

    list_resp = await client.get(
        f"/api/v1/projects/alert-audit/alert-deliveries?channel=slack&destination_id={destination_id}"
    )
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] == 1
    assert body["items"][0]["destination_name"] == "Audit Slack"
    assert body["items"][0]["rule_name"] == "Audit Rule"

    detail_resp = await client.get(f"/api/v1/projects/alert-audit/alert-deliveries/{delivery_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["items"][0]["direction"] == "drop"
    assert detail["items"][0]["monitoring_path"].endswith("/monitoring/event/event-1")


def test_send_alert_delivery_fails_with_invalid_stored_telegram_token(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name="Alert Runtime",
            slug="alert-runtime",
            description="",
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="telegram",
            name="Ops Bot",
            enabled=True,
            bot_token_encrypted="123456:abc def",
            chat_id="-100123",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="telegram",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="event-1",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed"
    assert "Telegram destination configuration is invalid" in result["error"]

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_renders_telegram_html_template(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_template_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_payload: dict[str, object] = {}

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name="Alert Runtime",
            slug="alert-runtime",
            description="",
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="telegram",
            name="Ops Bot",
            enabled=True,
            bot_token_encrypted="123456:ABC_def",
            chat_id="-100123",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_template=("<b>Items</b>\n${items_text}"),
            items_template=(
                "<b>${scope_name}</b> ${actual_count}/${expected_count}"
                "${details_line}${monitoring_line}"
            ),
            message_format="telegram_html",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="telegram",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase & success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path="https://app.example.com/details/1",
            monitoring_path="https://app.example.com/monitoring/1",
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def capture_post_json(url: str, body: dict[str, object]) -> None:
        sent_payload["url"] = url
        sent_payload["body"] = body

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        capture_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent"
    assert sent_payload["url"] == "https://api.telegram.org/bot123456:ABC_def/sendMessage"
    assert sent_payload["body"] == {
        "chat_id": "-100123",
        "text": (
            "<b>Items</b>\n"
            "<b>purchase &amp; success</b> 10/20\n"
            "  details: https://app.example.com/details/1\n"
            "  monitoring: https://app.example.com/monitoring/1"
        ),
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        assert persisted.error_message is None
        assert persisted.payload_snapshot is not None
        assert persisted.payload_snapshot["message_format"] == "telegram_html"
        assert persisted.payload_snapshot["rendered_message"] == (
            "<b>Items</b>\n"
            "<b>purchase &amp; success</b> 10/20\n"
            "  details: https://app.example.com/details/1\n"
            "  monitoring: https://app.example.com/monitoring/1"
        )

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_uses_default_template_for_selected_format(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_default_template_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_payload: dict[str, object] = {}

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name="Alert Runtime",
            slug="alert-runtime",
            description="",
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="telegram",
            name="Ops Bot",
            enabled=True,
            bot_token_encrypted="123456:ABC_def",
            chat_id="-100123",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_template=None,
            message_format="telegram_html",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="telegram",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase & success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path="https://app.example.com/details/1",
            monitoring_path="https://app.example.com/monitoring/1",
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def capture_post_json(url: str, body: dict[str, object]) -> None:
        sent_payload["url"] = url
        sent_payload["body"] = body

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        capture_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent"
    assert sent_payload["body"] == {
        "chat_id": "-100123",
        "text": (
            "<b>[tripl] 1 alerts</b>\n"
            "Project delivery via telegram: Ops Bot\n"
            "Rule: <b>Main Rule</b>\n"
            "Scan: <code>Runtime Scan</code>\n\n"
            "- Event purchase &amp; success: down, actual=10, expected=20, delta=10 (50.0%)\n"
            "  details: https://app.example.com/details/1\n"
            "  monitoring: https://app.example.com/monitoring/1"
        ),
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.payload_snapshot is not None
        assert persisted.payload_snapshot["message_format"] == "telegram_html"
        assert persisted.payload_snapshot["rendered_message"] == (
            "<b>[tripl] 1 alerts</b>\n"
            "Project delivery via telegram: Ops Bot\n"
            "Rule: <b>Main Rule</b>\n"
            "Scan: <code>Runtime Scan</code>\n\n"
            "- Event purchase &amp; success: down, actual=10, expected=20, delta=10 (50.0%)\n"
            "  details: https://app.example.com/details/1\n"
            "  monitoring: https://app.example.com/monitoring/1"
        )

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_persists_rendered_message_on_send_failure(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_send_failure_snapshot.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name="Alert Runtime",
            slug="alert-runtime",
            description="",
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="telegram",
            name="Ops Bot",
            enabled=True,
            bot_token_encrypted="123456:ABC_def",
            chat_id="-100123",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_template="[tripl] ${matched_count} alerts\n${items_text}",
            message_format="telegram_markdownv2",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="telegram",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def fail_post_json(url: str, body: dict[str, object]) -> None:
        raise ValueError("HTTP 400 from https://api.telegram.org/bot***/sendMessage: Bad Request")

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        fail_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed"
    assert "Bad Request" in result["error"]

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.payload_snapshot is not None
        assert persisted.payload_snapshot["message_format"] == "telegram_markdownv2"
        assert isinstance(persisted.payload_snapshot.get("rendered_message"), str)
        assert persisted.payload_snapshot["rendered_message"]

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_falls_back_from_telegram_markdownv2_to_plain(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_markdown_fallback.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_payloads: list[dict[str, object]] = []

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name="Alert Runtime",
            slug="alert-runtime",
            description="",
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="telegram",
            name="Ops Bot",
            enabled=True,
            bot_token_encrypted="123456:ABC_def",
            chat_id="-100123",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_template="[tripl] ${matched_count} alerts\n${items_text}",
            message_format="telegram_markdownv2",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="telegram",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def flaky_post_json(url: str, body: dict[str, object]) -> None:
        sent_payloads.append(body)
        if len(sent_payloads) == 1:
            raise ValueError(
                "HTTP 400 from https://api.telegram.org/bot***/sendMessage: "
                "Bad Request: can't parse entities: Character '-' is reserved "
                "and must be escaped with the preceding '\\'"
            )

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        flaky_post_json,
    )

    # The sparkline/top-movers DB build must run ONCE even though we render the
    # message twice (MarkdownV2 then the plain fallback) — the fallback reuses
    # the cached context instead of re-querying.
    from tripl.worker.tasks import alerts_messages as alerts_messages_module

    real_build_context = alerts_messages_module.build_alert_item_context
    build_context_calls = 0

    def counting_build_context(*args: object, **kwargs: object) -> tuple[str, str]:
        nonlocal build_context_calls
        build_context_calls += 1
        return real_build_context(*args, **kwargs)

    monkeypatch.setattr(alerts_messages_module, "build_alert_item_context", counting_build_context)

    result = metrics.send_alert_delivery.run(delivery_id)

    assert build_context_calls == 1
    assert result["status"] == "sent"
    assert len(sent_payloads) == 2
    assert sent_payloads[0]["parse_mode"] == "MarkdownV2"
    assert "parse_mode" not in sent_payloads[1]
    assert sent_payloads[1]["text"] == (
        "[tripl] 1 alerts\n- Event purchase:success: down, actual=10, expected=20, delta=10 (50.0%)"
    )

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        assert persisted.payload_snapshot is not None
        assert persisted.payload_snapshot["requested_message_format"] == "telegram_markdownv2"
        assert persisted.payload_snapshot["message_format"] == "plain"
        assert persisted.payload_snapshot["fallback_reason"] == "telegram_markdown_parse_error"

    Base.metadata.drop_all(engine)
    engine.dispose()


def _telegram_units(text: str) -> int:
    """Length the way Telegram counts it.

    The 4096 ceiling — like the entity offsets in the same API — is counted in
    UTF-16 code units, so anything outside the BMP costs two and ``len`` (code
    points) under-counts it.
    """
    return len(text.encode("utf-16-le")) // 2


def _seed_telegram_length_case(
    sync_session_factory,
    *,
    item_count: int,
    message_template: str,
    ai_explanation_enabled: bool = False,
    suffix: str = "",
) -> tuple[str, list[str]]:
    """One pending Telegram delivery whose items are the size live ones are.

    The optional details/monitoring lines are what make a real item ~330
    characters (97-389 across the deliveries this instance has sent), so the
    URLs carry the production shape rather than None. Returns the delivery id
    and every item's scope_name, in seeded order.

    ``suffix`` distinguishes a SECOND seeding in the same database. Both
    ``data_sources.name`` (uq_data_source_name) and ``projects.slug`` are unique
    across the whole database, so a test that seeds two deliveries into one
    engine — a resume beside its control, say — dies on an IntegrityError from
    the fixture rather than on the thing it set out to measure. The default is
    empty, so every existing caller seeds exactly the names it always did.
    """
    scope_names: list[str] = []
    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name=f"Alert Runtime{suffix}",
            slug=f"alert-runtime{suffix}",
            description="",
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Runtime DS{suffix}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name=f"Runtime Scan{suffix}",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="telegram",
            name="Ops Bot",
            enabled=True,
            bot_token_encrypted="123456:ABC_def",
            chat_id="-100123",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_template=message_template,
            message_format="plain",
            ai_explanation_enabled=ai_explanation_enabled,
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="telegram",
            status="pending",
            matched_count=item_count,
            payload_snapshot={},
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery])
        for index in range(item_count):
            # Zero-padded so no scope name is a prefix of another and the
            # "delivered exactly once" count below cannot match ":1" inside
            # ":10".
            scope_name = f"acme_ios:map:layer_switch:precipitation_overlay:{index:03d}"
            scope_names.append(scope_name)
            session.add(
                AlertDeliveryItem(
                    id=uuid.uuid4(),
                    delivery_id=delivery.id,
                    scope_type="event",
                    scope_ref=f"event-{index}",
                    scope_name=scope_name,
                    bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
                    direction="drop",
                    actual_count=15403,
                    expected_count=32048,
                    absolute_delta=16645,
                    percent_delta=51.9,
                    details_path=(
                        f"https://tripl.example.com/p/acme-ios/monitoring/event/{uuid.uuid4()}"
                    ),
                    monitoring_path=(
                        f"https://tripl.example.com/p/acme-ios/events/detail/{uuid.uuid4()}"
                    ),
                )
            )
        session.commit()
        return str(delivery.id), scope_names


def test_send_alert_delivery_splits_a_long_telegram_delivery_across_messages(
    tmp_path,
    monkeypatch,
) -> None:
    """Every matched item reaches the reader, across as many messages as it takes.

    That is what alerting.md promises. The renderer used to stop at the first
    item that would not fit and append "+N more of 14 not shown (message length
    limit)" — while the success path stamps ``last_notified_at`` on EVERY item
    of the delivery, so the cut scopes were recorded as told and stayed silent
    until they stopped firing and re-opened.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_split.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_payloads: list[dict[str, object]] = []

    delivery_id, scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=14,
        message_template="[tripl] ${matched_count} alerts\n${items_text}",
    )

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        sent_payloads.append(body)
        # Telegram's own rule, in Telegram's own units and wording.
        if _telegram_units(str(body["text"])) > 4096:
            raise ValueError(
                "HTTP 400 from https://api.telegram.org/bot***/sendMessage: "
                "Bad Request: message is too long"
            )

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    bodies = [str(payload["text"]) for payload in sent_payloads]
    delivered = "\n".join(bodies)
    missing = [name for name in scope_names if name not in delivered]
    assert missing == [], (
        f"{len(missing)} of {len(scope_names)} matched items never reached Telegram "
        f"across {len(bodies)} message(s): {missing}"
    )
    # Split, not repeated: an item belongs to exactly one message.
    assert [delivered.count(name) for name in scope_names] == [1] * len(scope_names)
    assert [_telegram_units(body) for body in bodies if _telegram_units(body) > 4096] == []
    assert len(bodies) > 1
    assert "not shown" not in delivered
    assert result["status"] == "sent"

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        assert persisted.payload_snapshot is not None
        assert persisted.payload_snapshot["telegram_message_parts"] == len(bodies)

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_never_re_renders_telegram_at_a_bigger_budget(
    tmp_path,
    monkeypatch,
) -> None:
    """A too-long message must not be answered with an equally long one.

    The retry this replaces recomputed the item budget from whether an AI note
    was actually produced, while the render that had just been refused reserved
    room for one because the RULE has notes enabled. A rule whose note came back
    empty therefore retried at 3696 characters after failing at 2496 — a budget
    that fits MORE items, so the second body was longer than the first and was
    refused identically, and the delivery failed. The long custom preamble here
    is what pushes the first render over the ceiling in the first place.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_no_bigger_retry.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_payloads: list[dict[str, object]] = []

    preamble = "\n".join(
        f"Runbook step {step}: check the release dashboard, then the deploy log, "
        "then page the on-call engineer if the drop holds for two buckets."
        for step in range(15)
    )
    delivery_id, scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=12,
        message_template=f"[tripl] ${{matched_count}} alerts\n{preamble}\n${{items_text}}",
        ai_explanation_enabled=True,
    )

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        sent_payloads.append(body)
        if _telegram_units(str(body["text"])) > 4096:
            raise ValueError(
                "HTTP 400 from https://api.telegram.org/bot***/sendMessage: "
                "Bad Request: message is too long"
            )

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )
    # AI is enabled on the rule but the provider returns nothing — the exact
    # asymmetry the old retry arithmetic keyed on.
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_build_ai_explanation",
        lambda *args, **kwargs: None,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    lengths = [_telegram_units(str(payload["text"])) for payload in sent_payloads]
    refused = [length for length in lengths if length > 4096]
    assert refused == [], (
        f"{len(refused)} of {len(lengths)} attempts were over Telegram's 4096-unit "
        f"ceiling; the attempts measured {lengths}"
    )
    delivered = "\n".join(str(payload["text"]) for payload in sent_payloads)
    missing = [name for name in scope_names if name not in delivered]
    assert missing == [], (
        f"{len(missing)} of {len(scope_names)} matched items never reached Telegram: {missing}"
    )
    assert result["status"] == "sent"

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value

    Base.metadata.drop_all(engine)
    engine.dispose()


def _retry_from_inbox(sync_session_factory, delivery_id: str) -> None:
    """What ``retry_delivery`` does to the row before re-enqueueing the task.

    Kept in the same shape as ``services/_alerting_deliveries.retry_delivery``
    (failed -> pending, error and sent_at cleared, attempt budget reset) so the
    resume below is exercised through the state the Inbox button really leaves.
    """
    with sync_session_factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        assert delivery.status == AlertDeliveryStatus.failed.value
        delivery.status = AlertDeliveryStatus.pending.value
        delivery.error_message = None
        delivery.sent_at = None
        delivery.dispatch_attempts = 0
        session.commit()


def test_send_alert_delivery_resumes_a_partly_sent_telegram_split(
    tmp_path,
    monkeypatch,
) -> None:
    """A retry must not re-send the messages that already reached the reader.

    A long delivery goes out as several Telegram messages posted back to back.
    If the chat's rate limit answers 429 (or the connection times out, or the
    API 5xxes) on the second one, the first is already with the reader, but the
    delivery is recorded failed — and the retry, manual from the Inbox or from
    the stale-pending reaper, used to re-render and re-send EVERY message.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_resume.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    accepted: list[str] = []
    attempts: list[list[str]] = []
    fail_after_first = True

    delivery_id, scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=14,
        message_template="[tripl] ${matched_count} alerts\n${items_text}",
    )

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        text = str(body["text"])
        attempts[-1].append(text)
        if fail_after_first and len(attempts[-1]) > 1:
            # Neither a MarkdownV2 parse error nor an over-4096 rejection: the
            # ordinary transport failure that used to propagate untracked.
            raise ValueError(
                "HTTP 429 from https://api.telegram.org/bot***/sendMessage: "
                "Too Many Requests: retry after 27"
            )
        accepted.append(text)

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    attempts.append([])
    first = metrics.send_alert_delivery.run(delivery_id)
    assert first["status"] == "failed"
    assert len(attempts[0]) > 1, (
        "the delivery did not split into several messages, so this test is not "
        f"exercising a partial send: {[len(body) for body in attempts[0]]}"
    )
    first_message_names = [name for name in scope_names if name in accepted[0]]
    assert first_message_names, "the first message carried no items"

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value

    fail_after_first = False
    _retry_from_inbox(sync_session_factory, delivery_id)
    attempts.append([])
    second = metrics.send_alert_delivery.run(delivery_id)
    assert second["status"] == "sent"

    resent = [name for name in first_message_names if any(name in body for body in attempts[1])]
    assert resent == [], (
        f"the retry re-sent {len(resent)} of {len(first_message_names)} items the reader "
        f"had already received in the first message: {resent}"
    )
    delivered = "\n".join(accepted)
    assert [delivered.count(name) for name in scope_names] == [1] * len(scope_names), (
        "every matched item must reach the reader exactly once across the failed "
        f"attempt and the retry; counts were "
        f"{ {name: delivered.count(name) for name in scope_names} }"
    )

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_retry_of_a_fully_sent_telegram_split_sends_nothing(
    tmp_path,
    monkeypatch,
) -> None:
    """Retry after every part landed completes the record, it does not re-send.

    The send loop can finish and the delivery still be recorded failed — the
    worker is SIGKILLed before the status commit and ``task_acks_late`` re-runs
    the task, or the bookkeeping after the loop raises. Retry then has nothing
    left to tell the reader, so it only finishes the row.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_resume_done.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    attempts: list[list[str]] = []
    fail_after_all = True

    delivery_id, scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=14,
        message_template="[tripl] ${matched_count} alerts\n${items_text}",
    )

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        attempts[-1].append(str(body["text"]))

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    def stamp_rule_state(*args: object, **kwargs: object) -> None:
        # Stands in for a worker death between the last accepted message and
        # the status=sent commit: everything is out, nothing is recorded sent.
        if fail_after_all:
            raise RuntimeError("worker died before the status commit")

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_stamp_rule_state",
        stamp_rule_state,
    )

    attempts.append([])
    first = metrics.send_alert_delivery.run(delivery_id)
    assert first["status"] == "failed"
    assert len(attempts[0]) > 1

    fail_after_all = False
    _retry_from_inbox(sync_session_factory, delivery_id)
    attempts.append([])
    second = metrics.send_alert_delivery.run(delivery_id)

    assert attempts[1] == [], (
        f"the retry re-sent {len(attempts[1])} message(s) the reader already had"
    )
    assert second["status"] == "sent"
    delivered = "\n".join(attempts[0])
    assert [delivered.count(name) for name in scope_names] == [1] * len(scope_names)

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_posts_generic_webhook(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_webhook_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent: dict[str, object] = {}

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name="Alert Runtime",
            slug="alert-runtime",
            description="",
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        # Secrets are stored encrypted; in dev/test the crypto layer is a
        # passthrough, so plaintext here round-trips through _decrypt_secret.
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="webhook",
            name="Ops Webhook",
            enabled=True,
            target_url_encrypted="https://example.com/hook",
            webhook_header_name="X-Webhook-Token",
            webhook_header_value_encrypted="secret-abc",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_format="plain",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="webhook",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def capture_post_json(url: str, body: dict[str, object], headers: dict[str, str] | None = None):
        sent["url"] = url
        sent["body"] = body
        sent["headers"] = headers

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        capture_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent"
    assert sent["url"] == "https://example.com/hook"
    assert sent["headers"] == {"X-Webhook-Token": "secret-abc"}
    body = sent["body"]
    assert isinstance(body, dict)
    assert body["matched_count"] == 1
    assert body["rule"]["name"] == "Main Rule"
    assert body["scan"]["name"] == "Runtime Scan"
    assert body["project"]["slug"] == "alert-runtime"
    assert len(body["items"]) == 1
    assert body["items"][0]["scope_name"] == "purchase:success"
    assert body["items"][0]["direction"] == "drop"
    assert isinstance(body["message"], str)
    assert "purchase:success" in body["message"]

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        assert persisted.error_message is None

    Base.metadata.drop_all(engine)
    engine.dispose()


def _build_rule(**overrides: object) -> AlertRule:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "destination_id": uuid.uuid4(),
        "name": "Test rule",
        "enabled": True,
        "include_project_total": True,
        "include_event_types": True,
        "include_events": True,
        "include_schema_drifts": False,
        "include_distribution_drifts": False,
        "include_release_regressions": False,
        "notify_on_spike": True,
        "notify_on_drop": True,
        "min_percent_delta": 0.0,
        "min_absolute_delta": 0,
        "min_expected_count": 0,
        "cooldown_minutes": 60,
        "message_template": None,
        "items_template": None,
        "message_format": "plain",
    }
    defaults.update(overrides)
    rule = AlertRule(**defaults)
    rule.filters = []
    return rule


# Sentinel meaning "some scan, I don't care which": ``_build_anomaly`` swaps it
# for a fresh id. Deliberately distinct from an explicit ``None``, which is what
# a project-global (``metric``-scope) anomaly really carries on its row.
#
# A COOLDOWN TEST MUST NOT USE IT. The replay's cooldown key carries the scan
# partition, mirroring ``uq_alert_rule_state_scope``, so a fresh
# id per anomaly puts each one on its own clock and nothing is ever suppressed.
# Pass one explicit ``scan_config_id`` to every anomaly that shares a clock.
_ANY_SCAN_CONFIG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")


def _build_anomaly(
    bucket: datetime,
    *,
    scope_type: str = "event",
    scope_ref: str | None = None,
    direction: str = "spike",
    actual_count: int = 100,
    expected_count: float = 10.0,
    scan_config_id: uuid.UUID | None = _ANY_SCAN_CONFIG,
) -> object:
    from tripl.models.metric_anomaly import MetricAnomaly

    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=(uuid.uuid4() if scan_config_id == _ANY_SCAN_CONFIG else scan_config_id),
        scope_type=scope_type,
        scope_ref=scope_ref or str(uuid.uuid4()),
        event_id=None,
        event_type_id=None,
        bucket=bucket,
        actual_count=actual_count,
        expected_count=expected_count,
        stddev=1.0,
        z_score=5.0,
        direction=direction,
    )


def test_simulate_rule_firings_applies_cooldown_per_scope() -> None:
    from tripl.alerting_matching import simulate_rule_firings

    rule = _build_rule(cooldown_minutes=60)
    scope_a = str(uuid.uuid4())
    scope_b = str(uuid.uuid4())
    base = datetime(2026, 5, 1, 12, tzinfo=UTC)
    # One scan for every anomaly, stated explicitly: the cooldown partition
    # includes the scan, so the ``_ANY_SCAN_CONFIG`` default
    # would give each of these its own clock and admit all four.
    scan = uuid.uuid4()

    anomalies = [
        # Scope A: 3 anomalies at 0, 30min, 90min → cooldown=60min admits 1st and 3rd.
        _build_anomaly(base, scope_ref=scope_a, scan_config_id=scan),
        _build_anomaly(base.replace(hour=12, minute=30), scope_ref=scope_a, scan_config_id=scan),
        _build_anomaly(base.replace(hour=14), scope_ref=scope_a, scan_config_id=scan),
        # Scope B: independent cooldown — 1 anomaly admitted.
        _build_anomaly(base.replace(hour=12, minute=15), scope_ref=scope_b, scan_config_id=scan),
    ]

    fired = simulate_rule_firings(rule, anomalies)
    fired_keys = [(a.scope_ref, a.bucket) for a in fired]
    assert fired_keys == [
        (scope_a, base),
        (scope_b, base.replace(hour=12, minute=15)),
        (scope_a, base.replace(hour=14)),
    ]


def test_simulate_rule_firings_skips_scope_disabled_by_rule() -> None:
    from tripl.alerting_matching import simulate_rule_firings

    rule = _build_rule(include_events=False, cooldown_minutes=0)
    base = datetime(2026, 5, 1, 12, tzinfo=UTC)

    anomalies = [
        _build_anomaly(base, scope_type="event"),
        _build_anomaly(base, scope_type="event_type"),
    ]
    fired = simulate_rule_firings(rule, anomalies)
    assert [a.scope_type for a in fired] == ["event_type"]


def test_a_spike_from_a_zero_baseline_clears_a_percent_threshold() -> None:
    """A relative threshold has nothing to divide by at a zero baseline.

    The old fallback scored it 0% — the largest possible relative move reported
    as the smallest. Harmless while the default threshold was 0, silencing once
    it became 100: a scope resuming after an outage, or an event firing for the
    first time, matched no rule with a percent threshold. The mirror case makes
    the asymmetry plain, so it is asserted alongside.
    """
    from tripl.alerting_matching import rule_matches_anomaly

    base = datetime(2026, 5, 1, 12, tzinfo=UTC)
    rule = _build_rule(min_percent_delta=100)

    appeared = _build_anomaly(base, actual_count=40, expected_count=0.0)
    assert rule_matches_anomaly(rule, appeared) is True

    # The mirror: gone to zero from a real baseline is exactly 100% and alerts.
    went_dark = _build_anomaly(base, direction="drop", actual_count=0, expected_count=40.0)
    assert rule_matches_anomaly(rule, went_dark) is True

    # No baseline and no movement is not an event.
    nothing = _build_anomaly(base, actual_count=0, expected_count=0.0)
    assert rule_matches_anomaly(rule, nothing) is False


def test_a_fractional_baseline_below_one_keeps_its_full_percent() -> None:
    """Why the zero case is not fixed with a divisor floor of 1.

    Catalog metrics reach the numeric thresholds with fractional values, gated
    only at 1e-6. Scoring them against ``max(expected, 1)`` — the divisor the
    UI's relative effect uses, which assumes counts — would turn this 350% into
    70% and drop it under the very threshold the fix is about.
    """
    from tripl.alerting_matching import rule_matches_anomaly

    ratio = _build_anomaly(
        datetime(2026, 5, 1, 12, tzinfo=UTC),
        scope_type="metric",
        actual_count=0.9,
        expected_count=0.2,
    )

    assert rule_matches_anomaly(_build_rule(include_metrics=True, min_percent_delta=300), ratio)
    assert not rule_matches_anomaly(_build_rule(include_metrics=True, min_percent_delta=400), ratio)


def test_schema_drift_rule_matching_uses_scope_gate_not_metric_thresholds() -> None:
    from tripl.alerting_matching import SchemaDriftAlertCandidate, rule_matches_anomaly

    candidate = SchemaDriftAlertCandidate(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type="schema",
        scope_ref=str(uuid.uuid4()),
        event_id=None,
        event_type_id=uuid.uuid4(),
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="spike",
        actual_count=1,
        expected_count=0,
        drift_field="payload.extra",
        drift_type="new_field",
        sample_value="TASK-123",
    )

    disabled_rule = _build_rule(include_schema_drifts=False)
    assert rule_matches_anomaly(disabled_rule, candidate) is False

    enabled_rule = _build_rule(
        include_schema_drifts=True,
        min_percent_delta=999,
        min_absolute_delta=999,
        min_expected_count=999,
    )
    assert rule_matches_anomaly(enabled_rule, candidate) is True


def test_distribution_drift_rule_matching_uses_scope_gate_not_metric_thresholds() -> None:
    from tripl.alerting_matching import (
        SCOPE_DISTRIBUTION_DRIFT,
        DistributionDriftAlertCandidate,
        rule_matches_anomaly,
    )

    candidate = DistributionDriftAlertCandidate(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type=SCOPE_DISTRIBUTION_DRIFT,
        scope_ref="distribution-scope",
        event_id=None,
        event_type_id=uuid.uuid4(),
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="spike",
        actual_count=1000,
        expected_count=1000,
        drift_field="platform",
        drift_type="distribution_shift",
        sample_value="psi=0.400; ios 50.0%->90.0%",
    )

    disabled_rule = _build_rule(include_distribution_drifts=False)
    assert rule_matches_anomaly(disabled_rule, candidate) is False

    enabled_rule = _build_rule(
        include_distribution_drifts=True,
        min_percent_delta=999,
        min_absolute_delta=999,
        min_expected_count=999,
    )
    assert rule_matches_anomaly(enabled_rule, candidate) is True


def test_release_regression_rule_matching_uses_scope_gate_not_metric_thresholds() -> None:
    from tripl.alerting_matching import (
        SCOPE_RELEASE_REGRESSION,
        DriftAlertCandidate,
        rule_matches_anomaly,
    )

    candidate = DriftAlertCandidate(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type=SCOPE_RELEASE_REGRESSION,
        scope_ref=str(uuid.uuid4()),
        event_id=uuid.uuid4(),
        event_type_id=None,
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="drop",
        actual_count=0,
        expected_count=200.0,
        drift_field="2.1.0",
        drift_type="missing",
        sample_value="2.0.0",
    )

    # Gated by the dedicated include_release_regressions opt-in (not the generic
    # event toggles), and the analyzer's own gates mean numeric thresholds are
    # skipped.
    assert rule_matches_anomaly(_build_rule(include_release_regressions=False), candidate) is False
    # Generic event rules must NOT pick up regressions on their own.
    assert (
        rule_matches_anomaly(
            _build_rule(include_events=True, include_release_regressions=False), candidate
        )
        is False
    )

    enabled_rule = _build_rule(
        include_release_regressions=True,
        notify_on_drop=True,
        min_percent_delta=999,
        min_absolute_delta=999,
        min_expected_count=999,
    )
    assert rule_matches_anomaly(enabled_rule, candidate) is True

    # Direction gate still applies: a regression is a drop.
    no_drop_rule = _build_rule(include_release_regressions=True, notify_on_drop=False)
    assert rule_matches_anomaly(no_drop_rule, candidate) is False


def test_a_scan_bound_rule_ignores_every_other_scan() -> None:
    """A rule narrowed to one scan must not fire on a sibling scan's anomaly.

    Rules hang off a destination, destinations off a project, and dispatch runs
    once per scan config — so before ``AlertRule.scan_config_id`` existed, one
    rule fired for every scan in the project and there was no filter field able
    to say otherwise (``AlertRuleFilterField`` is event_type/event/direction and
    ``filter_matches_anomaly`` passes anything else through).
    """
    from tripl.alerting_matching import rule_matches_anomaly

    watched_scan = uuid.uuid4()
    other_scan = uuid.uuid4()
    bucket = datetime(2026, 5, 1, 12, tzinfo=UTC)
    rule = _build_rule(scan_config_id=watched_scan)

    mine = _build_anomaly(bucket, scan_config_id=watched_scan)
    theirs = _build_anomaly(bucket, scan_config_id=other_scan)

    assert rule_matches_anomaly(rule, mine) is True
    assert rule_matches_anomaly(rule, theirs) is False

    # NULL is the migration's no-op: an unbound rule keeps watching the project.
    project_rule = _build_rule()
    assert rule_matches_anomaly(project_rule, mine) is True
    assert rule_matches_anomaly(project_rule, theirs) is True


def test_a_scan_bound_rule_has_nothing_to_say_about_catalog_metrics() -> None:
    """``include_metrics`` goes inert once a rule is bound to a scan.

    Catalog metric anomalies are project-global — their row carries a NULL
    ``scan_config_id`` — so "this scan only" and "this project-wide series"
    cannot both be true. The scan gate wins; only an unbound rule delivers them.
    """
    from tripl.alerting_matching import rule_matches_anomaly

    metric_anomaly = _build_anomaly(
        datetime(2026, 5, 1, 12, tzinfo=UTC),
        scope_type="metric",
        scan_config_id=None,
    )

    bound = _build_rule(include_metrics=True, scan_config_id=uuid.uuid4())
    assert rule_matches_anomaly(bound, metric_anomaly) is False

    unbound = _build_rule(include_metrics=True)
    assert rule_matches_anomaly(unbound, metric_anomaly) is True


def test_a_scan_bound_rule_ignores_another_scans_drift() -> None:
    """The gate has to reach the dataclass candidates too, not only anomaly rows.

    Schema / distribution / variable-value drift and release regressions arrive
    as ``DriftAlertCandidate`` dataclasses rather than ORM rows. If they did not
    carry the scan they came from, a scan-bound rule would silently keep firing
    on every scan's drift.
    """
    from tripl.alerting_matching import DriftAlertCandidate, rule_matches_anomaly

    watched_scan = uuid.uuid4()

    def _drift(scan_config_id: uuid.UUID) -> DriftAlertCandidate:
        return DriftAlertCandidate(
            id=uuid.uuid4(),
            scan_config_id=scan_config_id,
            scope_type="schema",
            scope_ref=str(uuid.uuid4()),
            event_id=None,
            event_type_id=uuid.uuid4(),
            bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
            direction="spike",
            actual_count=1,
            expected_count=0,
            drift_field="payload.extra",
            drift_type="new_field",
            sample_value="TASK-123",
        )

    rule = _build_rule(include_schema_drifts=True, scan_config_id=watched_scan)
    assert rule_matches_anomaly(rule, _drift(watched_scan)) is True
    assert rule_matches_anomaly(rule, _drift(uuid.uuid4())) is False


def test_release_regression_item_renders_readable_release_line() -> None:
    from tripl.alert_templates import get_default_items_template, render_alert_template
    from tripl.models.alert_delivery_item import AlertDeliveryItem
    from tripl.worker.tasks.alerts_messages import _build_item_template_context

    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type="release_regression",
        scope_ref=str(uuid.uuid4()),
        scope_name="Login",
        event_id=uuid.uuid4(),
        event_type_id=None,
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="drop",
        actual_count=0,
        expected_count=200,
        absolute_delta=200,
        percent_delta=100.0,
        drift_type="missing",
        drift_field="2.1.0",
        sample_value="2.0.0",
    )
    context = _build_item_template_context(item, message_format="plain")
    rendered = render_alert_template(get_default_items_template("plain"), context)
    assert "Release regression Login" in rendered
    # "vs 2.0.0", not "(was 2.0.0)": the line now continues into the basis
    # clause, and a parenthetical mid-sentence read as an aside. What the
    # clause says, and why, is asserted in
    # test_release_regression_alert_link.py.
    assert "release: disappeared in 2.1.0 vs 2.0.0" in rendered
    assert "actual=0" in rendered
    assert "expected=200 (adoption-adjusted)" in rendered


def test_an_event_item_does_not_offer_the_same_link_twice() -> None:
    """`details` and `monitoring` resolve to one page for an event scope.

    `/events/detail/<id>` redirects to `/monitoring/event/<id>`, and that view
    carries the field values, meta fields AND the charts — so both builders
    produce the same URL and printing it under two labels advertised a choice
    that does not exist.
    """
    from tripl.alert_templates import get_default_items_template, render_alert_template
    from tripl.models.alert_delivery_item import AlertDeliveryItem
    from tripl.worker.tasks.alerts_messages import _build_item_template_context

    event_id = uuid.uuid4()
    same = f"https://tripl.example/p/demo/monitoring/event/{event_id}"
    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type="event",
        scope_ref=str(event_id),
        scope_name="Login",
        event_id=event_id,
        event_type_id=None,
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="drop",
        actual_count=10,
        expected_count=100,
        absolute_delta=90,
        percent_delta=90.0,
        details_path=same,
        monitoring_path=same,
    )
    rendered = render_alert_template(
        get_default_items_template("plain"),
        _build_item_template_context(item, message_format="plain"),
    )
    assert rendered.count(same) == 1, rendered
    assert "monitoring:" not in rendered


def test_a_scope_whose_two_links_differ_still_offers_both() -> None:
    """An event-type row points at the event type AND at the underlying event,
    which are different pages, so collapsing must not reach that case."""
    from tripl.alert_templates import get_default_items_template, render_alert_template
    from tripl.models.alert_delivery_item import AlertDeliveryItem
    from tripl.worker.tasks.alerts_messages import _build_item_template_context

    event_id, type_id = uuid.uuid4(), uuid.uuid4()
    details = f"https://tripl.example/p/demo/monitoring/event/{event_id}"
    monitoring = f"https://tripl.example/p/demo/monitoring/event-type/{type_id}"
    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type="event_type",
        scope_ref=str(type_id),
        scope_name="Purchases",
        event_id=event_id,
        event_type_id=type_id,
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="drop",
        actual_count=10,
        expected_count=100,
        absolute_delta=90,
        percent_delta=90.0,
        details_path=details,
        monitoring_path=monitoring,
    )
    rendered = render_alert_template(
        get_default_items_template("plain"),
        _build_item_template_context(item, message_format="plain"),
    )
    assert details in rendered
    assert monitoring in rendered


def test_format_metric_alert_value_percent_rules() -> None:
    from tripl.alert_templates import format_metric_alert_value

    # Percent metrics store fractions: scale ×100, integral drops the decimal.
    assert format_metric_alert_value(0.08, "%") == "8%"
    assert format_metric_alert_value(0.083, "%") == "8.3%"
    assert format_metric_alert_value(1.0, "%") == "100%"
    assert format_metric_alert_value(0.0, "%") == "0%"
    # Binary float noise must not force a decimal: 0.08 - 0.04 != exact 0.04.
    assert format_metric_alert_value(0.08 - 0.04, "%") == "4%"
    # Any other unit passes through unchanged for the shared stringifier.
    assert format_metric_alert_value(0.08, None) == 0.08
    assert format_metric_alert_value(0.08, "ms") == 0.08
    assert format_metric_alert_value(12.0, "users") == 12.0


def _metric_scope_delivery_item(
    *,
    scope_ref: str | None = None,
    actual: float,
    expected: float,
    delta: float,
) -> AlertDeliveryItem:
    return AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type="metric",
        scope_ref=scope_ref or str(uuid.uuid4()),
        scope_name="Signup CR",
        event_id=None,
        event_type_id=None,
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="spike",
        actual_count=actual,
        expected_count=expected,
        absolute_delta=delta,
        percent_delta=100.0,
    )


def test_metric_scope_percent_item_renders_scaled_values() -> None:
    from tripl.alert_templates import get_default_items_template, render_alert_template
    from tripl.worker.tasks.alerts_messages import _build_item_template_context

    item = _metric_scope_delivery_item(actual=0.08, expected=0.04, delta=0.04)
    context = _build_item_template_context(item, message_format="plain", metric_unit="%")
    rendered = render_alert_template(get_default_items_template("plain"), context)
    assert "actual=8%" in rendered
    assert "expected=4%" in rendered
    assert "delta=4% (100.0%)" in rendered

    fractional = _metric_scope_delivery_item(actual=0.083, expected=0.04, delta=0.043)
    context = _build_item_template_context(fractional, message_format="plain", metric_unit="%")
    rendered = render_alert_template(get_default_items_template("plain"), context)
    assert "actual=8.3%" in rendered
    assert "delta=4.3% (100.0%)" in rendered


def test_metric_scope_non_percent_and_event_items_render_unchanged() -> None:
    """Regression: only unit='%' opts in — other units and scopes stay raw."""
    from tripl.alert_templates import get_default_items_template, render_alert_template
    from tripl.worker.tasks.alerts_messages import _build_item_template_context

    template = get_default_items_template("plain")
    item = _metric_scope_delivery_item(actual=12.0, expected=4.0, delta=8.0)
    for unit in (None, "users", "ms"):
        rendered = render_alert_template(
            template,
            _build_item_template_context(item, message_format="plain", metric_unit=unit),
        )
        assert "actual=12" in rendered
        assert "expected=4," in rendered
        assert "delta=8 (100.0%)" in rendered

    event_item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type="event",
        scope_ref=str(uuid.uuid4()),
        scope_name="Login",
        event_id=uuid.uuid4(),
        event_type_id=None,
        bucket=datetime(2026, 5, 1, 12, tzinfo=UTC),
        direction="spike",
        actual_count=200,
        expected_count=20.5,
        absolute_delta=179.5,
        percent_delta=875.6,
    )
    rendered = render_alert_template(
        template,
        _build_item_template_context(event_item, message_format="plain"),
    )
    assert "actual=200" in rendered
    assert "expected=20.5" in rendered
    assert "delta=179.5 (875.6%)" in rendered


def test_simulator_renders_same_percent_values_as_worker() -> None:
    """Shared-predicates parity: preview and live send must show identical
    numbers for the same percent metric."""
    from tripl.alert_templates import get_default_items_template, render_alert_template
    from tripl.schemas.alerting import SimulatedRuleFiring
    from tripl.services.alerting_rendering import render_firing_item
    from tripl.worker.tasks.alerts_messages import _build_item_template_context

    metric_id = str(uuid.uuid4())
    bucket = datetime(2026, 5, 1, 12, tzinfo=UTC)
    template = get_default_items_template("plain")

    firing = SimulatedRuleFiring(
        anomaly_id=uuid.uuid4(),
        scope_type="metric",
        scope_ref=metric_id,
        scope_name="Signup CR",
        event_type_id=None,
        event_id=None,
        bucket=bucket,
        direction="spike",
        actual_count=0.083,
        expected_count=0.04,
        absolute_delta=0.043,
        percent_delta=100.0,
    )
    simulated = render_firing_item(
        firing,
        message_format="plain",
        items_template=template,
        metric_unit="%",
    )

    item = _metric_scope_delivery_item(
        scope_ref=metric_id, actual=0.083, expected=0.04, delta=0.043
    )
    item.bucket = bucket
    live = render_alert_template(
        template,
        _build_item_template_context(item, message_format="plain", metric_unit="%"),
    ).rstrip()

    assert "actual=8.3%" in simulated
    assert "expected=4%" in simulated
    assert "delta=4.3%" in simulated
    assert simulated == live


@pytest.mark.asyncio
async def test_alert_rule_simulate_endpoint(client: AsyncClient) -> None:
    from datetime import timedelta

    from tripl.models.metric_anomaly import MetricAnomaly

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Sim", "slug": "alert-sim"},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    destination_resp = await client.post(
        "/api/v1/projects/alert-sim/alert-destinations",
        json={
            "type": "slack",
            "name": "Sim Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/sim",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/alert-sim/alert-destinations/{destination_id}/rules",
        json={
            "name": "Sim Rule",
            "enabled": True,
            "include_project_total": True,
            "include_event_types": True,
            "include_events": True,
            "notify_on_spike": True,
            "notify_on_drop": True,
            "min_percent_delta": 0,
            "min_absolute_delta": 0,
            "min_expected_count": 0,
            "cooldown_minutes": 60,
            "filters": [],
        },
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    now = datetime.now(UTC)
    scope_ref = str(uuid.uuid4())
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name="ds",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(scan)
        await session.flush()
        # 4 anomalies, 0/15/30/60 min apart. Cooldown=60min admits #0 and #3.
        for offset_min in (0, 15, 30, 60):
            session.add(
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan.id,
                    scope_type="event",
                    scope_ref=scope_ref,
                    event_id=None,
                    event_type_id=None,
                    bucket=now - timedelta(days=1) + timedelta(minutes=offset_min),
                    actual_count=200,
                    expected_count=20.0,
                    stddev=1.0,
                    z_score=10.0,
                    direction="spike",
                )
            )

    resp = await client.post(
        f"/api/v1/projects/alert-sim/alert-destinations/"
        f"{destination_id}/rules/{rule_id}/simulate?days=7"
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["anomalies_considered"] == 4
    assert payload["matched_before_cooldown"] == 4
    assert len(payload["firings"]) == 2
    assert payload["noisy"] is False
    assert payload["rule_id"] == rule_id
    # Preview attached: rendered_message exists and per-firing rendered_item too.
    assert payload["cooldown_minutes_used"] == 60
    assert payload["cooldown_minutes_saved"] == 60
    assert isinstance(payload["rendered_message"], str)
    assert payload["rendered_message"]
    assert all(
        isinstance(f["rendered_item"], str) and f["rendered_item"] for f in payload["firings"]
    )

    # Cooldown override = 0 disables grouping, so every anomaly fires.
    resp_zero = await client.post(
        f"/api/v1/projects/alert-sim/alert-destinations/"
        f"{destination_id}/rules/{rule_id}/simulate"
        f"?days=7&cooldown_minutes_override=0"
    )
    assert resp_zero.status_code == 200
    payload_zero = resp_zero.json()
    assert payload_zero["cooldown_minutes_used"] == 0
    assert payload_zero["cooldown_minutes_saved"] == 60
    assert len(payload_zero["firings"]) == 4

    # Cooldown override well above the spacing collapses everything to one firing.
    resp_long = await client.post(
        f"/api/v1/projects/alert-sim/alert-destinations/"
        f"{destination_id}/rules/{rule_id}/simulate"
        f"?days=7&cooldown_minutes_override=600"
    )
    assert resp_long.status_code == 200
    payload_long = resp_long.json()
    assert payload_long["cooldown_minutes_used"] == 600
    assert len(payload_long["firings"]) == 1


@pytest.mark.asyncio
async def test_alert_rule_simulate_renders_percent_metric_values(client: AsyncClient) -> None:
    """Simulate resolves the metric's unit ('%') with one batched query and
    previews the same ×100 values the live worker would send."""
    from tripl.models.metric_anomaly import MetricAnomaly
    from tripl.models.metric_definition import MetricDefinition

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Sim Pct", "slug": "alert-sim-pct"},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    destination_resp = await client.post(
        "/api/v1/projects/alert-sim-pct/alert-destinations",
        json={
            "type": "slack",
            "name": "Sim Pct Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/simpct",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/alert-sim-pct/alert-destinations/{destination_id}/rules",
        json={
            "name": "Percent Metric Rule",
            "enabled": True,
            "include_project_total": False,
            "include_event_types": False,
            "include_events": False,
            "include_metrics": True,
            "notify_on_spike": True,
            "notify_on_drop": True,
            "min_percent_delta": 0,
            "min_absolute_delta": 0,
            "min_expected_count": 0,
            "cooldown_minutes": 60,
            "filters": [],
        },
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    now = datetime.now(UTC)
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name="ds-pct",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc-pct",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project_id,
            name="conversion_rate",
            display_name="Conversion Rate",
            kind="sql",
            config={},
            data_source_id=data_source.id,
            interval="1h",
            status="active",
            unit="%",
        )
        session.add_all([scan, metric])
        await session.flush()
        # NOTE: live metric-scope anomalies are project-global (NULL
        # scan_config_id); the simulator window query joins scan_configs, so
        # anchor on the scan to surface this anomaly in the window.
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                scope_type="metric",
                scope_ref=str(metric.id),
                event_id=None,
                event_type_id=None,
                bucket=now - timedelta(days=1),
                actual_count=0.08,
                expected_count=0.04,
                stddev=0.01,
                z_score=4.0,
                direction="spike",
            )
        )

    resp = await client.post(
        f"/api/v1/projects/alert-sim-pct/alert-destinations/"
        f"{destination_id}/rules/{rule_id}/simulate?days=7"
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["firings"]) == 1
    rendered_item = payload["firings"][0]["rendered_item"]
    assert "actual=8%" in rendered_item
    assert "expected=4%" in rendered_item
    assert "delta=4% (100.0%)" in rendered_item


@pytest.mark.asyncio
async def test_alert_rule_simulate_includes_project_global_metric_anomaly(
    client: AsyncClient,
) -> None:
    """Regression: catalog metric anomalies are project-global,
    stored with scope_type='metric' and a NULL scan_config_id. The simulator's
    old scan_config inner join silently dropped them, so metric-including rules
    never fired. The simulator must now load them (scoped via MetricDefinition)
    and resolve their scope_name to the metric's display name — while non-metric
    (event) loading stays byte-identical.
    """
    from tripl.models.metric_anomaly import MetricAnomaly
    from tripl.models.metric_definition import MetricDefinition

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Sim Global", "slug": "alert-sim-global"},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    destination_resp = await client.post(
        "/api/v1/projects/alert-sim-global/alert-destinations",
        json={
            "type": "slack",
            "name": "Sim Global Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/simglobal",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/alert-sim-global/alert-destinations/{destination_id}/rules",
        json={
            "name": "Metric + Event Rule",
            "enabled": True,
            "include_project_total": True,
            "include_event_types": True,
            "include_events": True,
            "include_metrics": True,
            "notify_on_spike": True,
            "notify_on_drop": True,
            "min_percent_delta": 0,
            "min_absolute_delta": 0,
            "min_expected_count": 0,
            "cooldown_minutes": 60,
            "filters": [],
        },
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    now = datetime.now(UTC)
    metric_id = uuid.uuid4()
    event_scope_ref = str(uuid.uuid4())
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name="ds-global",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc-global",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        metric = MetricDefinition(
            id=metric_id,
            project_id=project_id,
            name="signups",
            display_name="Signups",
            kind="sql",
            config={},
            data_source_id=data_source.id,
            interval="1h",
            status="active",
            unit=None,
        )
        session.add_all([scan, metric])
        await session.flush()
        # Project-global catalog metric anomaly: NULL scan_config_id. The old
        # simulator inner-joined scan_configs and silently dropped these rows.
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=None,
                scope_type="metric",
                scope_ref=str(metric_id),
                event_id=None,
                event_type_id=None,
                bucket=now - timedelta(days=1),
                actual_count=200.0,
                expected_count=20.0,
                stddev=1.0,
                z_score=10.0,
                direction="spike",
            )
        )
        # Non-metric (event) anomaly stays anchored to a scan config and must
        # still load exactly as before (unchanged behavior).
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                scope_type="event",
                scope_ref=event_scope_ref,
                event_id=None,
                event_type_id=None,
                bucket=now - timedelta(days=1, minutes=5),
                actual_count=150.0,
                expected_count=15.0,
                stddev=1.0,
                z_score=9.0,
                direction="spike",
            )
        )

    resp = await client.post(
        f"/api/v1/projects/alert-sim-global/alert-destinations/"
        f"{destination_id}/rules/{rule_id}/simulate?days=7"
    )
    assert resp.status_code == 200
    payload = resp.json()

    # Both the project-global metric anomaly and the event anomaly are loaded.
    assert payload["anomalies_considered"] == 2
    assert payload["matched_before_cooldown"] == 2

    firings_by_scope = {f["scope_type"]: f for f in payload["firings"]}
    # Regression: the event anomaly still fires (non-metric loading unchanged),
    # and the previously-dropped project-global metric anomaly now fires too.
    assert set(firings_by_scope) == {"metric", "event"}

    # Core defect fix: the metric anomaly's scope_name resolves to the metric's
    # display name, not the raw UUID scope_ref.
    metric_firing = firings_by_scope["metric"]
    assert metric_firing["scope_ref"] == str(metric_id)
    assert metric_firing["scope_name"] == "Signups"
    assert metric_firing["scope_name"] != metric_firing["scope_ref"]


def test_simulate_rule_firings_respects_cooldown_override() -> None:
    from tripl.alerting_matching import simulate_rule_firings

    rule = _build_rule(cooldown_minutes=60)
    scope = str(uuid.uuid4())
    base = datetime(2026, 5, 1, 12, tzinfo=UTC)
    # One scan for all three: the cooldown partition includes the scan,
    # and three separate scans would be three separate clocks.
    scan = uuid.uuid4()
    anomalies = [
        _build_anomaly(base, scope_ref=scope, scan_config_id=scan),
        _build_anomaly(base.replace(hour=12, minute=15), scope_ref=scope, scan_config_id=scan),
        _build_anomaly(base.replace(hour=12, minute=30), scope_ref=scope, scan_config_id=scan),
    ]

    # Without override: saved cooldown=60 → only the first fires.
    assert len(simulate_rule_firings(rule, anomalies)) == 1

    # Override to 0: every anomaly fires.
    assert len(simulate_rule_firings(rule, anomalies, cooldown_minutes_override=0)) == 3

    # Override to 10: 15-min and 30-min anomalies each clear the gate.
    assert len(simulate_rule_firings(rule, anomalies, cooldown_minutes_override=10)) == 3

    # Override to 20: only first and third clear (second is 15 min after first).
    fired_20 = simulate_rule_firings(rule, anomalies, cooldown_minutes_override=20)
    assert [a.bucket for a in fired_20] == [anomalies[0].bucket, anomalies[2].bucket]


def test_build_sparkline_handles_empty_flat_and_varied_inputs() -> None:
    from tripl.anomaly_context import build_sparkline

    assert build_sparkline([]) == ""

    # All-identical → mid block, length preserved.
    flat = build_sparkline([5, 5, 5, 5])
    assert len(flat) == 4
    assert flat == flat[0] * 4

    # Ascending series → strictly non-decreasing block heights.
    ascending = build_sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    blocks = "▁▂▃▄▅▆▇█"
    assert ascending[0] == blocks[0]
    assert ascending[-1] == blocks[-1]
    levels = [blocks.index(ch) for ch in ascending]
    assert levels == sorted(levels)

    # Width cap: trim to last N when input longer.
    long_series = list(range(40))
    capped = build_sparkline(long_series, width=10)
    assert len(capped) == 10


def test_format_top_movers_renders_signed_percent_and_truncates_value() -> None:
    from types import SimpleNamespace

    from tripl.anomaly_context import format_top_movers

    movers = [
        SimpleNamespace(
            breakdown_column="country",
            breakdown_value="RU",
            actual_count=42,
            expected_count=10.0,
        ),
        SimpleNamespace(
            breakdown_column="device",
            breakdown_value="extremely-long-device-identifier-string",
            actual_count=2,
            expected_count=10.0,
        ),
        # Zero baseline → "+inf%" label so we never divide by zero.
        SimpleNamespace(
            breakdown_column="referrer",
            breakdown_value="new_one",
            actual_count=5,
            expected_count=0.0,
        ),
    ]
    rendered = format_top_movers(movers)  # type: ignore[arg-type]
    # Country: (42-10)/10*100 = +320%
    assert "country=RU +320%" in rendered
    # Device value truncated with ellipsis at MAX_MOVER_VALUE_LEN-1 chars.
    assert "device=extremely-long-device-i…" in rendered
    # Zero baseline labeled +inf%.
    assert "referrer=new_one +inf%" in rendered
    # Separator: middle dot with spaces.
    assert rendered.count(" · ") == 2


@pytest.mark.asyncio
async def test_send_alert_delivery_attaches_top_movers_and_sparkline(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: a real EventMetric + MetricBreakdownAnomaly history
    surface as ``trend:`` / ``movers:`` lines in the rendered Slack message.
    """
    from datetime import timedelta

    from tripl.models.event_metric import EventMetric
    from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
    from tripl.worker.tasks import metrics

    engine = create_engine(f"sqlite:///{tmp_path / 'explain.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    bucket = datetime(2026, 4, 11, 12, tzinfo=UTC)

    with sync_session_factory() as session:
        project = Project(id=uuid.uuid4(), name="Explain", slug="explain", description="")
        ds = DataSource(
            id=uuid.uuid4(),
            name="ds",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=ds.id,
            project_id=project.id,
            name="sc",
            base_query="SELECT t, app_version FROM events",
            time_column="t",
            app_version_column="app_version",
            cardinality_threshold=100,
            interval="1h",
        )
        event_id = uuid.uuid4()
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="slack",
            name="dst",
            enabled=True,
            webhook_url_encrypted="https://hooks.slack.com/services/T/B/sim",
            chat_id=None,
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="rule",
            enabled=True,
            message_format="plain",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="slack",
            status="pending",
            matched_count=1,
            payload_snapshot={},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref=str(event_id),
            scope_name="purchase",
            event_id=event_id,
            event_type_id=None,
            bucket=bucket,
            direction="drop",
            actual_count=10,
            expected_count=100,
            absolute_delta=90,
            percent_delta=90.0,
            details_path=None,
            monitoring_path=None,
        )
        # 6 historical buckets with rising counts → non-flat sparkline.
        for i, count in enumerate([10, 12, 15, 50, 80, 10]):
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan.id,
                    event_id=event_id,
                    event_type_id=None,
                    bucket=bucket - timedelta(hours=5 - i),
                    count=count,
                )
            )
        # One outsized breakdown anomaly at the same bucket → top mover.
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                scope_type="event",
                scope_ref=str(event_id),
                event_id=event_id,
                event_type_id=None,
                bucket=bucket,
                breakdown_column="country",
                breakdown_value="RU",
                is_other=False,
                actual_count=1,
                expected_count=50.0,
                stddev=1.0,
                z_score=-12.0,
                direction="drop",
            )
        )
        # Legacy version markers must not leak into movers after this behavior
        # changes; version lifecycle is handled by release regressions instead.
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                scope_type="event",
                scope_ref=str(event_id),
                event_id=event_id,
                event_type_id=None,
                bucket=bucket,
                breakdown_column="app_version",
                breakdown_value="2.10.0",
                is_other=False,
                actual_count=1,
                expected_count=100.0,
                stddev=1.0,
                z_score=-30.0,
                direction="drop",
            )
        )
        session.add_all([project, ds, scan, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    sent_bodies: list[dict[str, object]] = []

    def capture_post_json(url: str, body: dict[str, object]) -> None:
        sent_bodies.append(body)

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        capture_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)
    assert result["status"] == "sent"
    assert len(sent_bodies) == 1

    text = sent_bodies[0]["text"]
    assert isinstance(text, str)
    assert "movers: country=RU" in text
    assert "app_version" not in text
    assert "trend: " in text  # Sparkline rendered after `trend: ` label.

    Base.metadata.drop_all(engine)
    engine.dispose()


# --- Email channel ----------------------------------------------------------


@pytest.mark.asyncio
async def test_alerting_email_destination_crud_and_validation(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Email Alerts", "slug": "email-alerts", "description": ""},
    )
    assert project_resp.status_code == 201

    # Empty / malformed recipient list rejected.
    empty_resp = await client.post(
        "/api/v1/projects/email-alerts/alert-destinations",
        json={"type": "email", "name": "Bad", "email_recipients": "   ,  "},
    )
    assert empty_resp.status_code == 422

    bad_addr_resp = await client.post(
        "/api/v1/projects/email-alerts/alert-destinations",
        json={"type": "email", "name": "Bad", "email_recipients": "not-an-email"},
    )
    assert bad_addr_resp.status_code == 422

    # Newline in subject template is header injection → reject.
    bad_subject_resp = await client.post(
        "/api/v1/projects/email-alerts/alert-destinations",
        json={
            "type": "email",
            "name": "Bad Subject",
            "email_recipients": "alice@example.com",
            "email_subject_template": "spike\nBcc: attacker@example.com",
        },
    )
    assert bad_subject_resp.status_code == 422

    create_resp = await client.post(
        "/api/v1/projects/email-alerts/alert-destinations",
        json={
            "type": "email",
            "name": "Ops Email",
            "email_recipients": "alice@example.com, bob@example.com , alice@example.com",
            "email_from_address": "alerts@tripl-app.io",
            "email_subject_template": "[{project_name}] {rule_name}",
        },
    )
    assert create_resp.status_code == 201
    destination = create_resp.json()
    assert destination["type"] == "email"
    # Dedup + normalized whitespace in the recipient CSV.
    assert destination["email_recipients"] == "alice@example.com, bob@example.com"
    assert destination["email_from_address"] == "alerts@tripl-app.io"
    assert destination["email_subject_template"] == "[{project_name}] {rule_name}"
    destination_id = destination["id"]

    # Update validation: bad address in the new list is rejected; good update sticks.
    bad_update = await client.patch(
        f"/api/v1/projects/email-alerts/alert-destinations/{destination_id}",
        json={"email_recipients": "not-an-email"},
    )
    assert bad_update.status_code == 422

    good_update = await client.patch(
        f"/api/v1/projects/email-alerts/alert-destinations/{destination_id}",
        json={"email_recipients": "carol@example.com", "name": "Renamed Email"},
    )
    assert good_update.status_code == 200
    body = good_update.json()
    assert body["name"] == "Renamed Email"
    assert body["email_recipients"] == "carol@example.com"


def _email_sender_with(refused: object):
    """``_send_email_message`` bound to a fake SMTP returning ``refused``."""
    from tripl.worker.tasks.alerts_channels import _send_email_message

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int = 10) -> None:
            pass

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def starttls(self) -> None:
            pass

        def login(self, username: str, password: str) -> None:
            pass

        def send_message(self, msg) -> object:  # type: ignore[no-untyped-def]
            return refused

    class FakeSmtplib:
        SMTP = FakeSMTP
        SMTP_SSL = FakeSMTP

    def send() -> None:
        _send_email_message(
            smtp_module=FakeSmtplib,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_security="none",
            from_address="alerts@example.com",
            recipients=["alice@example.com", "bob@example.com", "carol@example.com"],
            subject="Subject",
            body="Body",
        )

    return send


def test_email_partial_recipient_refusal_is_not_a_successful_send() -> None:
    """smtplib only raises when EVERY recipient is refused.

    The partial-refusal dict used to be discarded, so a mail that reached one of
    three people was stored and shown as "sent".
    """
    send = _email_sender_with(
        {
            "bob@example.com": (550, b"No such user"),
            "carol@example.com": (552, b"Mailbox full"),
        }
    )

    with pytest.raises(ValueError, match="refused 2 of 3") as error:
        send()

    # The Audit view shows this string, so it has to name who missed out.
    assert "bob@example.com" in str(error.value)
    assert "carol@example.com" in str(error.value)
    assert "alice@example.com" not in str(error.value)


@pytest.mark.parametrize("refused", [{}, None])
def test_email_send_succeeds_when_nothing_is_refused(refused: object) -> None:
    # ``None`` covers SMTP stand-ins whose send_message returns nothing.
    _email_sender_with(refused)()


def test_send_alert_delivery_sends_email(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_email_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_messages: list[dict[str, object]] = []

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(), name="Alert Runtime", slug="alert-runtime", description=""
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="email",
            name="Ops Email",
            enabled=True,
            email_recipients="alice@example.com, bob@example.com",
            email_from_address=None,
            email_subject_template=None,
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_format="plain",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="email",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    # Fake SMTP client — captures the EmailMessage that send_message would ship.
    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int = 10) -> None:
            sent_messages.append({"connect_host": host, "connect_port": port})

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def starttls(self) -> None:
            sent_messages.append({"event": "starttls"})

        def login(self, username: str, password: str) -> None:
            sent_messages.append({"event": "login", "username": username})

        def send_message(self, msg) -> None:  # type: ignore[no-untyped-def]
            sent_messages.append(
                {
                    "event": "send",
                    "From": msg["From"],
                    "To": msg["To"],
                    "Subject": msg["Subject"],
                    "body": msg.get_content(),
                }
            )

    from tripl.config import settings as live_settings

    alerts_globals = metrics.send_alert_delivery.run.__globals__
    fake_smtplib = type("FakeSmtplibModule", (), {"SMTP": FakeSMTP})
    monkeypatch.setitem(alerts_globals, "_get_sync_session", sync_session_factory)
    monkeypatch.setitem(alerts_globals, "smtplib", fake_smtplib)
    monkeypatch.setattr(live_settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(live_settings, "smtp_port", 587)
    monkeypatch.setattr(live_settings, "smtp_username", "tripl-bot")
    monkeypatch.setattr(live_settings, "smtp_password", "hunter2")
    monkeypatch.setattr(live_settings, "smtp_use_tls", True)
    monkeypatch.setattr(live_settings, "smtp_from_address", "alerts@tripl-app.io")

    result = metrics.send_alert_delivery.run(delivery_id)
    assert result["status"] == "sent"

    # Connect → TLS → login → send.
    events = [m.get("event") for m in sent_messages if "event" in m]
    assert events == ["starttls", "login", "send"]
    send_event = next(m for m in sent_messages if m.get("event") == "send")
    assert send_event["From"] == "alerts@tripl-app.io"
    assert send_event["To"] == "alice@example.com, bob@example.com"
    # Default subject: "[<project>] <rule> — N alert(s)"
    assert send_event["Subject"] == "[Alert Runtime] Main Rule — 1 alert(s)"
    body = send_event["body"]
    assert isinstance(body, str)
    assert "purchase:success" in body

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_email_fails_without_smtp(monkeypatch, tmp_path) -> None:
    """Destination exists but SMTP is unconfigured → delivery is marked failed
    with an actionable error message (rather than crashing the worker)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_email_no_smtp.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    with sync_session_factory() as session:
        project = Project(id=uuid.uuid4(), name="P", slug="p", description="")
        data_source = DataSource(
            id=uuid.uuid4(),
            name="DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="S",
            base_query="SELECT 1",
            time_column="ts",
            cardinality_threshold=10,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="email",
            name="Email",
            enabled=True,
            email_recipients="x@example.com",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="R",
            enabled=True,
            message_format="plain",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="email",
            status="pending",
            matched_count=0,
            payload_snapshot=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery])
        session.commit()
        delivery_id = str(delivery.id)

    from tripl.config import settings as live_settings

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setattr(live_settings, "smtp_host", "")

    result = metrics.send_alert_delivery.run(delivery_id)
    assert result["status"] == "failed"
    assert "SMTP" in result["error"]

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.error_message is not None
        assert "SMTP" in persisted.error_message

    Base.metadata.drop_all(engine)
    engine.dispose()


# --- Jira channel -----------------------------------------------------------


@pytest.mark.asyncio
async def test_alerting_jira_destination_crud_and_validation(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Jira Alerts", "slug": "jira-alerts", "description": ""},
    )
    assert project_resp.status_code == 201

    # Non-https base_url rejected.
    bad_url_resp = await client.post(
        "/api/v1/projects/jira-alerts/alert-destinations",
        json={
            "type": "jira",
            "name": "Bad",
            "jira_base_url": "http://example.atlassian.net",
            "jira_auth_email": "alice@example.com",
            "jira_api_token": "secret",
            "jira_project_key": "ENG",
        },
    )
    assert bad_url_resp.status_code == 422

    # Lowercase / invalid project key rejected.
    bad_key_resp = await client.post(
        "/api/v1/projects/jira-alerts/alert-destinations",
        json={
            "type": "jira",
            "name": "Bad",
            "jira_base_url": "https://example.atlassian.net",
            "jira_auth_email": "alice@example.com",
            "jira_api_token": "secret",
            "jira_project_key": "engineering team",
        },
    )
    assert bad_key_resp.status_code == 422

    create_resp = await client.post(
        "/api/v1/projects/jira-alerts/alert-destinations",
        json={
            "type": "jira",
            "name": "Ops Jira",
            "jira_base_url": "https://example.atlassian.net/",
            "jira_auth_email": "alice@example.com",
            "jira_api_token": "secret-token",
            "jira_project_key": "eng",
            "jira_issue_type": "Bug",
        },
    )
    assert create_resp.status_code == 201
    destination = create_resp.json()
    assert destination["type"] == "jira"
    # base_url has trailing slash stripped, project key uppercased.
    assert destination["jira_base_url"] == "https://example.atlassian.net"
    assert destination["jira_project_key"] == "ENG"
    assert destination["jira_issue_type"] == "Bug"
    assert destination["jira_api_token_set"] is True
    # Secrets are never echoed back.
    assert "jira_api_token" not in destination
    destination_id = destination["id"]

    update_resp = await client.patch(
        f"/api/v1/projects/jira-alerts/alert-destinations/{destination_id}",
        json={"jira_issue_type": "Task", "name": "Renamed Jira"},
    )
    assert update_resp.status_code == 200
    body = update_resp.json()
    assert body["name"] == "Renamed Jira"
    assert body["jira_issue_type"] == "Task"


def test_send_alert_delivery_creates_jira_issue(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_jira_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent: dict[str, object] = {}

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(), name="Alert Runtime", slug="alert-runtime", description=""
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="jira",
            name="Ops Jira",
            enabled=True,
            jira_base_url="https://example.atlassian.net",
            jira_auth_email="alice@example.com",
            jira_api_token_encrypted="api-token-1",
            jira_project_key="ENG",
            jira_issue_type="Task",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_format="plain",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="jira",
            status="pending",
            matched_count=2,
            payload_snapshot={"preview": "two alerts"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def capture_post_json(url: str, body: dict[str, object], headers: dict[str, str] | None = None):
        sent["url"] = url
        sent["body"] = body
        sent["headers"] = headers

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        capture_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)
    assert result["status"] == "sent"

    assert sent["url"] == "https://example.atlassian.net/rest/api/3/issue"
    headers = sent["headers"]
    assert isinstance(headers, dict)
    # Basic auth header is base64("email:token").
    assert headers["Authorization"].startswith("Basic ")
    expected_creds = base64.b64encode(b"alice@example.com:api-token-1").decode()
    assert headers["Authorization"] == f"Basic {expected_creds}"
    body = sent["body"]
    assert isinstance(body, dict)
    fields = body["fields"]
    assert isinstance(fields, dict)
    assert fields["project"] == {"key": "ENG"}
    assert fields["issuetype"] == {"name": "Task"}
    assert "Main Rule" in fields["summary"]
    description = fields["description"]
    assert isinstance(description, dict)
    assert description["type"] == "doc"
    # Body has paragraphs; render text contains the scope name.
    rendered = json.dumps(description)
    assert "purchase:success" in rendered

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value

    Base.metadata.drop_all(engine)
    engine.dispose()


# --- Linear channel ---------------------------------------------------------


@pytest.mark.asyncio
async def test_alerting_linear_destination_crud_and_validation(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Linear Alerts", "slug": "linear-alerts", "description": ""},
    )
    assert project_resp.status_code == 201

    # Team id with whitespace rejected.
    bad_team_resp = await client.post(
        "/api/v1/projects/linear-alerts/alert-destinations",
        json={
            "type": "linear",
            "name": "Bad",
            "linear_api_key": "lin_api_xyz",
            "linear_team_id": "team with space",
        },
    )
    assert bad_team_resp.status_code == 422

    create_resp = await client.post(
        "/api/v1/projects/linear-alerts/alert-destinations",
        json={
            "type": "linear",
            "name": "Ops Linear",
            "linear_api_key": "lin_api_xyz",
            "linear_team_id": "team-eng-1",
            "linear_state_id": "state-backlog",
            "linear_label_ids": "label-1, label-2, label-1, ",
        },
    )
    assert create_resp.status_code == 201
    destination = create_resp.json()
    assert destination["type"] == "linear"
    assert destination["linear_team_id"] == "team-eng-1"
    assert destination["linear_state_id"] == "state-backlog"
    # Dedup + normalize: "label-1,label-2".
    assert destination["linear_label_ids"] == "label-1,label-2"
    assert destination["linear_api_key_set"] is True
    assert "linear_api_key" not in destination
    destination_id = destination["id"]

    update_resp = await client.patch(
        f"/api/v1/projects/linear-alerts/alert-destinations/{destination_id}",
        json={"linear_label_ids": None, "name": "Renamed Linear"},
    )
    assert update_resp.status_code == 200
    body = update_resp.json()
    assert body["name"] == "Renamed Linear"
    assert body["linear_label_ids"] is None


def test_send_alert_delivery_creates_linear_issue(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_linear_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent: dict[str, object] = {}

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(), name="Alert Runtime", slug="alert-runtime", description=""
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="linear",
            name="Ops Linear",
            enabled=True,
            linear_api_key_encrypted="lin_api_xyz",
            linear_team_id="team-eng-1",
            linear_state_id="state-backlog",
            linear_label_ids="label-1,label-2",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_format="plain",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="linear",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def capture_post_json(url: str, body: dict[str, object], headers: dict[str, str] | None = None):
        sent["url"] = url
        sent["body"] = body
        sent["headers"] = headers

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        capture_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)
    assert result["status"] == "sent"

    assert sent["url"] == "https://api.linear.app/graphql"
    headers = sent["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "lin_api_xyz"
    body = sent["body"]
    assert isinstance(body, dict)
    assert "mutation IssueCreate" in body["query"]
    variables = body["variables"]
    assert isinstance(variables, dict)
    input_payload = variables["input"]
    assert isinstance(input_payload, dict)
    assert input_payload["teamId"] == "team-eng-1"
    assert input_payload["stateId"] == "state-backlog"
    assert input_payload["labelIds"] == ["label-1", "label-2"]
    assert "Main Rule" in input_payload["title"]
    assert "purchase:success" in input_payload["description"]

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value

    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_alert_delivery(
    session,
    *,
    status: str,
    created_at: datetime,
    dispatch_attempts: int = 0,
    error_message: str | None = None,
    updated_at: datetime | None = None,
    channel: str = "webhook",
    destination_enabled: bool = True,
    claimed_at: datetime | None = None,
    payload_snapshot: dict[str, object] | None = None,
) -> uuid.UUID:
    """Create the minimal Project/ScanConfig/Destination/Rule graph plus one
    AlertDelivery, returning the delivery id. Used by the reaper tests.

    ``error_message`` and ``updated_at`` are what the reaper's failed arm
    selects on; when ``updated_at`` is omitted the column keeps its server
    default rather than being inserted as an explicit NULL.

    ``claimed_at`` is the send task's single-flight LEASE
    (``alerts._claim_delivery``), seeded the same optional way for the same
    reason. A row holding one is a row no send task can pick up until the lease
    ages out ``STRANDED_DELIVERY_MINUTES``, so which rows the reaper clears it
    on — and which it leaves alone — decides whether the delivery it just
    enqueued actually goes anywhere."""
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(), name=f"Reaper Project {suffix}", slug=f"reaper-{suffix}", description=""
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"Reaper DS {suffix}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    scan_config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Reaper Scan",
        base_query="SELECT * FROM events",
        time_column="created_at",
        cardinality_threshold=100,
        interval="1h",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=channel,
        name="Reaper Hook",
        enabled=destination_enabled,
        target_url_encrypted="https://example.com/hook",
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Reaper Rule",
        enabled=True,
    )
    optional_fields: dict[str, object] = {}
    if error_message is not None:
        optional_fields["error_message"] = error_message
    if updated_at is not None:
        optional_fields["updated_at"] = updated_at
    if claimed_at is not None:
        optional_fields["claimed_at"] = claimed_at
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=project.id,
        scan_config_id=scan_config.id,
        destination_id=destination.id,
        rule_id=rule.id,
        channel=channel,
        status=status,
        matched_count=1,
        dispatch_attempts=dispatch_attempts,
        created_at=created_at,
        payload_snapshot=payload_snapshot,
        **optional_fields,
    )
    session.add_all([project, data_source, scan_config, destination, rule, delivery])
    session.commit()
    return delivery.id


def test_reaper_keeps_combinable_digest_members_on_the_digest_sender(tmp_path, monkeypatch) -> None:
    from tripl.worker.tasks import maintenance
    from tripl.worker.tasks.alert_digest_send import send_alert_digest
    from tripl.worker.tasks.alerts import send_alert_delivery

    engine = create_engine(f"sqlite:///{tmp_path / 'digest-reaper.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    stale = datetime.now(UTC) - timedelta(minutes=maintenance.STRANDED_DELIVERY_MINUTES + 5)
    with factory() as session:
        slack_id = _seed_alert_delivery(
            session,
            status="pending",
            created_at=stale,
            updated_at=stale,
            channel="slack",
            payload_snapshot={"digest": True},
        )
        telegram_id = _seed_alert_delivery(
            session,
            status="pending",
            created_at=stale,
            updated_at=stale,
            channel="telegram",
            payload_snapshot={"digest": True},
        )

    monkeypatch.setattr(maintenance, "_get_sync_session", factory)
    grouped: list[list[str]] = []
    single: list[str] = []
    monkeypatch.setattr(send_alert_digest, "delay", lambda ids: grouped.append(ids))
    monkeypatch.setattr(
        send_alert_delivery, "delay", lambda delivery_id: single.append(delivery_id)
    )

    result = maintenance.requeue_stranded_alert_deliveries.run()

    assert result["requeued"] == 2
    assert grouped == [[str(slack_id)]]
    assert single == [str(telegram_id)]


def test_requeue_stranded_alert_deliveries_redispatches_and_bounds_attempts(
    tmp_path,
    monkeypatch,
) -> None:
    from tripl.worker.tasks import maintenance

    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    stale = timedelta(minutes=maintenance.STRANDED_DELIVERY_MINUTES + 5)
    with sync_session_factory() as session:
        # Stranded: pending, old and untouched, attempts left → re-enqueued.
        # ``updated_at`` is seeded old too: a genuinely stranded row has not
        # been touched since creation, and the arm now checks both so it never
        # races a row a retry path just flipped back to pending.
        stranded_id = _seed_alert_delivery(
            session,
            status="pending",
            created_at=now - stale,
            dispatch_attempts=1,
            updated_at=now - stale,
        )
        # Fresh pending (within horizon) → likely still in flight, skip.
        _seed_alert_delivery(session, status="pending", created_at=now)
        # Old row RECENTLY flipped back to pending (a manual Retry or the
        # failed arm's auto-retry) → its send is queued or in flight, skip.
        _seed_alert_delivery(session, status="pending", created_at=now - stale)
        # Already sent → ignore.
        _seed_alert_delivery(session, status="sent", created_at=now - stale)
        # Exhausted: pending, old, attempts maxed → mark failed, do not requeue.
        exhausted_id = _seed_alert_delivery(
            session,
            status="pending",
            created_at=now - stale,
            dispatch_attempts=maintenance.MAX_DISPATCH_ATTEMPTS,
            updated_at=now - stale,
        )

    monkeypatch.setattr(maintenance, "_get_sync_session", sync_session_factory)

    enqueued: list[str] = []
    from tripl.worker.tasks import alerts as alerts_module

    monkeypatch.setattr(
        alerts_module.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )

    result = maintenance.requeue_stranded_alert_deliveries.run()

    assert result["requeued"] == 1
    assert result["exhausted"] == 1
    assert enqueued == [str(stranded_id)]

    with sync_session_factory() as session:
        stranded = session.get(AlertDelivery, stranded_id)
        assert stranded is not None
        assert stranded.status == AlertDeliveryStatus.pending.value
        assert stranded.dispatch_attempts == 2

        exhausted = session.get(AlertDelivery, exhausted_id)
        assert exhausted is not None
        assert exhausted.status == AlertDeliveryStatus.failed.value
        assert exhausted.error_message is not None
        assert "exhausted" in exhausted.error_message

    Base.metadata.drop_all(engine)
    engine.dispose()


# The exact error text of the production failure this arm exists for
# (2026-08-31: one egress blip lost the only alert of the run).
_TRANSIENT_SEND_ERROR = "urlopen error [Errno 101] Network is unreachable"


def test_requeue_auto_retries_recent_transient_failed_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    from tripl.worker.tasks import maintenance

    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    with sync_session_factory() as session:
        failed_id = _seed_alert_delivery(
            session,
            status="failed",
            created_at=now - timedelta(hours=1),
            dispatch_attempts=0,
            error_message=_TRANSIENT_SEND_ERROR,
            updated_at=now,
            # A lease still inside its horizon, i.e. one no send task would be
            # able to take. The send task's own failure handler normally hands
            # the lease back with the attempt, so this is the state that
            # handler did NOT produce: a worker killed between its claim and
            # its terminal write, a release whose commit raised, a row written
            # before that release existed. The reaper clears it unconditionally
            # rather than trusting the release to have run, and this is the
            # only row in the suite that can tell whether it still does.
            claimed_at=now - timedelta(minutes=1),
        )

    monkeypatch.setattr(maintenance, "_get_sync_session", sync_session_factory)

    enqueued: list[str] = []
    from tripl.worker.tasks import alerts as alerts_module

    monkeypatch.setattr(
        alerts_module.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )

    result = maintenance.requeue_stranded_alert_deliveries.run()

    assert result["auto_retried"] == 1
    assert enqueued == [str(failed_id)]

    with sync_session_factory() as session:
        delivery = session.get(AlertDelivery, failed_id)
        assert delivery is not None
        # The attempt is recorded and the row is handed to the pending arm
        # (single-flight: this arm's WHERE no longer matches it, and the
        # manual Retry endpoint 409s on a non-failed row while the attempt is
        # queued). The error text is KEPT until the queued send resolves.
        assert delivery.dispatch_attempts == 1
        assert delivery.status == AlertDeliveryStatus.pending.value
        assert delivery.error_message == _TRANSIENT_SEND_ERROR
        # ...and the row can actually be SENT when the task it was just handed
        # to picks it up. ``_claim_delivery`` is a compare-and-set that refuses
        # a row whose ``claimed_at`` is newer than the stranded horizon, so a
        # lease left on this row would make the ``.delay()`` above a silent
        # no-op for fifteen minutes — the flip to `pending` and the enqueue
        # would both be visible and nothing would go out. Delete
        # ``delivery.claimed_at = None`` from the ``recent_failed`` loop in
        # worker/tasks/maintenance.py and this is the assertion that reddens.
        assert delivery.claimed_at is None, (
            "the auto-retried row kept a live lease, so the send it was just "
            "enqueued for cannot claim it"
        )

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_requeue_auto_retry_leaves_ticket_channels_and_disabled_destinations_alone(
    tmp_path,
    monkeypatch,
) -> None:
    """Two rows the failed arm must never touch, however transient the error.

    A Jira/Linear create is not idempotent — an after-send timeout leaves no
    external id in the snapshot, so an automatic re-run would mint a duplicate
    ticket per attempt; only a human who can check the tracker may retry. And
    a destination the operator disabled mid-incident has to stay silent, or
    the reaper re-sends the very rows they watched fail and switched off.
    """
    from tripl.worker.tasks import maintenance

    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    with sync_session_factory() as session:
        jira_id = _seed_alert_delivery(
            session,
            status="failed",
            created_at=now - timedelta(hours=1),
            dispatch_attempts=0,
            error_message=_TRANSIENT_SEND_ERROR,
            updated_at=now,
            channel="jira",
            claimed_at=now - timedelta(minutes=1),
        )
        disabled_id = _seed_alert_delivery(
            session,
            status="failed",
            created_at=now - timedelta(hours=1),
            dispatch_attempts=0,
            error_message=_TRANSIENT_SEND_ERROR,
            updated_at=now,
            destination_enabled=False,
            claimed_at=now - timedelta(minutes=1),
        )

    monkeypatch.setattr(maintenance, "_get_sync_session", sync_session_factory)

    enqueued: list[str] = []
    from tripl.worker.tasks import alerts as alerts_module

    monkeypatch.setattr(
        alerts_module.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )

    result = maintenance.requeue_stranded_alert_deliveries.run()

    assert result["auto_retried"] == 0
    assert enqueued == []

    with sync_session_factory() as session:
        for delivery_id in (jira_id, disabled_id):
            delivery = session.get(AlertDelivery, delivery_id)
            assert delivery is not None
            assert delivery.status == AlertDeliveryStatus.failed.value
            assert delivery.dispatch_attempts == 0
            assert delivery.error_message == _TRANSIENT_SEND_ERROR
            # Untouched means untouched, lease included. This is the scope
            # control for the release the sibling test asserts: the clear
            # belongs to the failed->pending FLIP, on the rows this arm's WHERE
            # actually selected, and not to a blanket sweep over every failed
            # row. A sweep would pass the sibling test and redden this one.
            assert delivery.claimed_at is not None

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_requeue_auto_retry_skips_non_transient_stale_and_exhausted_failures(
    tmp_path,
    monkeypatch,
) -> None:
    from tripl.worker.tasks import maintenance

    engine = create_engine(f"sqlite:///{tmp_path / 'reaper.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    beyond_horizon = maintenance.AUTO_RETRY_FAILED_HORIZON + timedelta(minutes=5)
    with sync_session_factory() as session:
        # Not a network blip: the destination rejected the message.
        non_transient_id = _seed_alert_delivery(
            session,
            status="failed",
            created_at=now - timedelta(hours=1),
            error_message="chat not found",
            updated_at=now,
        )
        # Transient, but past the horizon: belongs to the human Retry button.
        stale_id = _seed_alert_delivery(
            session,
            status="failed",
            created_at=now - timedelta(days=1),
            error_message=_TRANSIENT_SEND_ERROR,
            updated_at=now - beyond_horizon,
        )
        # Transient and recent, but out of attempts.
        maxed_id = _seed_alert_delivery(
            session,
            status="failed",
            created_at=now - timedelta(hours=1),
            dispatch_attempts=maintenance.MAX_DISPATCH_ATTEMPTS,
            error_message=_TRANSIENT_SEND_ERROR,
            updated_at=now,
        )

    monkeypatch.setattr(maintenance, "_get_sync_session", sync_session_factory)

    enqueued: list[str] = []
    from tripl.worker.tasks import alerts as alerts_module

    monkeypatch.setattr(
        alerts_module.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )

    result = maintenance.requeue_stranded_alert_deliveries.run()

    assert result["auto_retried"] == 0
    assert enqueued == []

    with sync_session_factory() as session:
        non_transient = session.get(AlertDelivery, non_transient_id)
        assert non_transient is not None
        assert non_transient.dispatch_attempts == 0
        assert non_transient.error_message == "chat not found"

        stale = session.get(AlertDelivery, stale_id)
        assert stale is not None
        assert stale.dispatch_attempts == 0
        assert stale.error_message == _TRANSIENT_SEND_ERROR

        maxed = session.get(AlertDelivery, maxed_id)
        assert maxed is not None
        assert maxed.dispatch_attempts == maintenance.MAX_DISPATCH_ATTEMPTS
        assert maxed.status == AlertDeliveryStatus.failed.value
        # Its real error is kept — the "exhausted redispatch attempts" relabel
        # belongs to the pending arm and would lie about why this one failed.
        assert maxed.error_message == _TRANSIENT_SEND_ERROR

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_is_transient_send_error_classifies_persisted_error_text() -> None:
    from tripl.worker.tasks._errors import is_transient_send_error

    assert is_transient_send_error(_TRANSIENT_SEND_ERROR)
    # Case-insensitive: the hint tuples are lowercase, urllib is not.
    assert is_transient_send_error("Connection Refused by peer")
    assert is_transient_send_error("read timed out")
    assert not is_transient_send_error("chat not found")
    assert not is_transient_send_error(None)


# --- SSRF guard on destination URLs ------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://127.0.0.1/hook",  # loopback
        "https://10.0.0.5/hook",  # RFC1918
        "https://192.168.1.10/hook",  # RFC1918
        "https://172.16.0.1/hook",  # RFC1918
        "https://[::1]/hook",  # IPv6 loopback
    ],
)
def test_validate_webhook_target_url_rejects_internal_literal_ips(url: str) -> None:
    from tripl.alerting_validation import validate_webhook_target_url

    with pytest.raises(ValueError, match="private or internal"):
        validate_webhook_target_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/rest/api/3",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://[::1]",
    ],
)
def test_validate_jira_base_url_rejects_internal_literal_ips(url: str) -> None:
    from tripl.alerting_validation import validate_jira_base_url

    with pytest.raises(ValueError, match="private or internal"):
        validate_jira_base_url(url)


def test_validate_webhook_target_url_rejects_hostname_resolving_to_private(monkeypatch) -> None:
    import tripl.alerting_validation as av

    def fake_getaddrinfo(host, *args, **kwargs):
        # Simulate an attacker-controlled DNS name pointing at the metadata IP.
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(av.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="private or internal"):
        av.validate_webhook_target_url("https://evil.example.com/hook")


def test_validate_webhook_target_url_fails_closed_on_dns_error(monkeypatch) -> None:
    import socket as socket_module

    import tripl.alerting_validation as av

    def fake_getaddrinfo(host, *args, **kwargs):
        raise socket_module.gaierror("name does not resolve")

    monkeypatch.setattr(av.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="could not be resolved"):
        av.validate_webhook_target_url("https://nope.invalid/hook")


def test_validate_webhook_target_url_allows_public_host(monkeypatch) -> None:
    import tripl.alerting_validation as av

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]  # example.com (public)

    monkeypatch.setattr(av.socket, "getaddrinfo", fake_getaddrinfo)
    assert (
        av.validate_webhook_target_url("https://hooks.example.com/abc")
        == "https://hooks.example.com/abc"
    )


# --- idempotent re-delivery under acks_late ----------------------


def test_send_alert_delivery_is_idempotent_on_resend(monkeypatch, tmp_path) -> None:
    """With task_acks_late a sent-then-crashed task gets re-queued. The second
    run must be a no-op: no second outbound call, status stays sent."""
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_idempotent_send.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    call_count = {"n": 0}

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(), name="Alert Runtime", slug="alert-runtime", description=""
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="jira",
            name="Ops Jira",
            enabled=True,
            jira_base_url="https://example.atlassian.net",
            jira_auth_email="alice@example.com",
            jira_api_token_encrypted="api-token-1",
            jira_project_key="ENG",
            jira_issue_type="Task",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_format="plain",
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="jira",
            status="pending",
            matched_count=1,
            payload_snapshot={"preview": "one alert"},
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def counting_post_json(
        url: str, body: dict[str, object], headers: dict[str, str] | None = None
    ):
        call_count["n"] += 1
        return {"id": "10001", "key": "ENG-1"}

    # No-op the SSRF send-time re-check so the test doesn't hit real DNS.
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_reject_private_target",
        lambda url, *, field: None,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        counting_post_json,
    )

    first = metrics.send_alert_delivery.run(delivery_id)
    assert first["status"] == "sent"
    assert call_count["n"] == 1

    # Re-run (simulating an acks_late re-queue) — must be a no-op.
    second = metrics.send_alert_delivery.run(delivery_id)
    assert second["status"] == "already_sent"
    assert call_count["n"] == 1  # no second outbound HTTP call

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        assert persisted.payload_snapshot["external_issue_key"] == "ENG-1"
        assert persisted.payload_snapshot["external_issue_id"] == "10001"

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_send_alert_delivery_skips_ticket_creation_when_external_id_present(
    monkeypatch, tmp_path
) -> None:
    """Crash window: a prior attempt created the Jira ticket and committed its
    external id into payload_snapshot, but was killed before status=sent. On
    re-run the delivery is still `pending`, so the status guard does not fire —
    the external-id guard must, skipping creation to avoid a duplicate ticket."""
    engine = create_engine(f"sqlite:///{tmp_path / 'alerting_ticket_guard.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    call_count = {"n": 0}

    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(), name="Alert Runtime", slug="alert-runtime", description=""
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Runtime DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="Runtime Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="jira",
            name="Ops Jira",
            enabled=True,
            jira_base_url="https://example.atlassian.net",
            jira_auth_email="alice@example.com",
            jira_api_token_encrypted="api-token-1",
            jira_project_key="ENG",
            jira_issue_type="Task",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            message_format="plain",
        )
        # Still pending, but external id already recorded from the prior attempt.
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="jira",
            status="pending",
            matched_count=1,
            payload_snapshot={
                "preview": "one alert",
                "external_issue_id": "10001",
                "external_issue_key": "ENG-1",
            },
        )
        item = AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref="event-1",
            scope_name="purchase:success",
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=10,
            expected_count=20,
            absolute_delta=10,
            percent_delta=50,
            details_path=None,
            monitoring_path=None,
        )
        session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
        session.commit()
        delivery_id = str(delivery.id)

    def counting_post_json(
        url: str, body: dict[str, object], headers: dict[str, str] | None = None
    ):
        call_count["n"] += 1
        return {"id": "20002", "key": "ENG-2"}

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_reject_private_target",
        lambda url, *, field: None,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        counting_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent"
    assert call_count["n"] == 0  # no ticket creation — guard skipped it

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        # Original id preserved, not overwritten by a new creation.
        assert persisted.payload_snapshot["external_issue_key"] == "ENG-1"

    Base.metadata.drop_all(engine)
    engine.dispose()


# --- Deprecated sunset alert ------------------------------------------------


def _make_sunset_project(
    session,  # type: ignore[no-untyped-def]
) -> tuple:
    """Return (project, destination) seeded in *session*."""
    project = Project(id=uuid.uuid4(), name="Sunset Project", slug="sunset-proj", description="")
    data_source = DataSource(
        id=uuid.uuid4(),
        name="DS",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="slack",
        name="Ops Slack",
        enabled=True,
        webhook_url_encrypted="fake-secret",
    )
    session.add_all([project, data_source, destination])
    session.commit()
    return project, destination


def test_check_deprecated_sunset_events_fires_when_event_alive_past_sunset(
    monkeypatch, tmp_path
) -> None:
    """A deprecated event with sunset_at in the past and last_seen_at > sunset_at
    triggers a Slack message."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sunset_alert_fires.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_messages: list[str] = []

    with sync_session_factory() as session:
        project, _destination = _make_sunset_project(session)
        sunset = datetime(2026, 1, 1, tzinfo=UTC)
        last_seen = datetime(2026, 3, 1, tzinfo=UTC)
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=uuid.uuid4(),  # FK not enforced in SQLite
            name="app:old_purchase",
            description="",
            status=EventStatus.deprecated,
            sunset_at=sunset,
            last_seen_at=last_seen,
        )
        session.add(event)
        session.commit()

    def fake_send_digest(
        *,
        destination,  # type: ignore[no-untyped-def]
        message: str,
        project,  # type: ignore[no-untyped-def]
        email_config,  # type: ignore[no-untyped-def]
        subject_title: str = "Weekly tripl digest",
    ) -> None:
        sent_messages.append(message)

    monkeypatch.setitem(
        check_deprecated_sunset_events.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        check_deprecated_sunset_events.run.__globals__,
        "_send_digest_to_destination",
        fake_send_digest,
    )

    result = check_deprecated_sunset_events.run()

    assert result["sent"] == 1
    assert len(sent_messages) == 1
    assert "app:old_purchase" in sent_messages[0]
    assert "deprecated" in sent_messages[0].lower() or "sunset" in sent_messages[0].lower()

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_check_deprecated_sunset_events_silent_when_no_recent_data(monkeypatch, tmp_path) -> None:
    """A deprecated event whose last_seen_at is before sunset_at does NOT fire."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sunset_alert_silent.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)
    sent_messages: list[str] = []

    with sync_session_factory() as session:
        project, _destination = _make_sunset_project(session)
        sunset = datetime(2026, 3, 1, tzinfo=UTC)
        # last_seen before sunset — not overdue
        last_seen = datetime(2026, 2, 1, tzinfo=UTC)
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=uuid.uuid4(),
            name="app:retired_event",
            description="",
            status=EventStatus.deprecated,
            sunset_at=sunset,
            last_seen_at=last_seen,
        )
        session.add(event)
        session.commit()

    def fake_send_digest(
        *,
        destination,  # type: ignore[no-untyped-def]
        message: str,
        project,  # type: ignore[no-untyped-def]
        email_config,  # type: ignore[no-untyped-def]
        subject_title: str = "Weekly tripl digest",
    ) -> None:
        sent_messages.append(message)

    monkeypatch.setitem(
        check_deprecated_sunset_events.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        check_deprecated_sunset_events.run.__globals__,
        "_send_digest_to_destination",
        fake_send_digest,
    )

    result = check_deprecated_sunset_events.run()

    assert result["sent"] == 0
    assert sent_messages == []

    Base.metadata.drop_all(engine)
    engine.dispose()


async def _seed_destination_rule_delivery(
    project_id: uuid.UUID,
    *,
    status: str,
    error_message: str | None = None,
    dispatch_attempts: int = 0,
) -> tuple[str, str, str]:
    """Insert a data source, scan, destination, rule and one delivery.

    Returns ``(delivery_id, destination_id, rule_id)`` as strings.
    """
    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Retry DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="Retry Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project_id,
            type="slack",
            name="Retry Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Retry Rule",
            enabled=True,
        )
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project_id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="slack",
            status=status,
            matched_count=1,
            error_message=error_message,
            dispatch_attempts=dispatch_attempts,
        )
        session.add_all([data_source, scan_config, destination, rule])
        # Flush parents before the delivery child: the unit of work has no ORM
        # relationship ordering the delivery after its scan_config, so a single
        # flush inserts it first and trips the FK under SQLite (matches Postgres).
        await session.flush()
        session.add(delivery)
        await session.commit()
        return str(delivery.id), str(destination.id), str(rule.id)


@pytest.mark.asyncio
async def test_retry_failed_delivery_resets_and_re_enqueues(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.worker.tasks.alerts import send_alert_delivery

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Retry", "slug": "alert-retry", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    delivery_id, _destination_id, _rule_id = await _seed_destination_rule_delivery(
        project_id,
        status="failed",
        error_message="boom",
        dispatch_attempts=3,
    )

    enqueued: list[str] = []
    monkeypatch.setattr(send_alert_delivery, "delay", lambda did: enqueued.append(did))

    resp = await client.post(f"/api/v1/projects/alert-retry/alert-deliveries/{delivery_id}/retry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["error_message"] is None
    assert enqueued == [delivery_id]

    async with TestSessionLocal() as session:
        persisted = await session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.pending.value
        assert persisted.error_message is None
        assert persisted.sent_at is None
        # Manual retry hands the reaper a clean attempt budget.
        assert persisted.dispatch_attempts == 0


@pytest.mark.asyncio
async def test_retry_non_failed_delivery_conflicts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.worker.tasks.alerts import send_alert_delivery

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Retry Sent", "slug": "alert-retry-sent", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    delivery_id, _destination_id, _rule_id = await _seed_destination_rule_delivery(
        project_id,
        status="sent",
    )

    enqueued: list[str] = []
    monkeypatch.setattr(send_alert_delivery, "delay", lambda did: enqueued.append(did))

    resp = await client.post(
        f"/api/v1/projects/alert-retry-sent/alert-deliveries/{delivery_id}/retry"
    )
    assert resp.status_code == 409
    # A sent delivery must never be re-dispatched.
    assert enqueued == []


@pytest.mark.asyncio
async def test_retry_unknown_delivery_returns_404(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Retry 404", "slug": "alert-retry-404", "description": ""},
    )
    assert project_resp.status_code == 201

    resp = await client.post(
        f"/api/v1/projects/alert-retry-404/alert-deliveries/{uuid.uuid4()}/retry"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_monitor_detail_mute_and_unmute(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Monitor Detail", "slug": "monitor-detail", "description": ""},
    )
    assert project_resp.status_code == 201

    destination_resp = await client.post(
        "/api/v1/projects/monitor-detail/alert-destinations",
        json={
            "type": "slack",
            "name": "Mon Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/mon",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/monitor-detail/alert-destinations/{destination_id}/rules",
        json={"name": "Mon Rule", "enabled": True, "filters": []},
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    detail_resp = await client.get(f"/api/v1/projects/monitor-detail/monitors/{rule_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["rule_id"] == rule_id
    assert detail["status"] == "healthy"
    assert detail["muted"] is False
    assert detail["muted_until"] is None
    assert detail["rule_enabled"] is True
    assert detail["destination_enabled"] is True
    assert detail["total_deliveries"] == 0
    assert detail["last_delivery_at"] is None
    # The project-wide default, which is what every rule is created with and
    # every rule predating the column carries. The scan join is an OUTER one for
    # exactly this row; an inner join would 404 here.
    assert detail["scan_config_id"] is None
    assert detail["scan_name"] is None

    # A mute in the past is rejected.
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    bad_mute = await client.post(
        f"/api/v1/projects/monitor-detail/monitors/{rule_id}/mute",
        json={"muted_until": past},
    )
    assert bad_mute.status_code == 422

    muted_until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    mute_resp = await client.post(
        f"/api/v1/projects/monitor-detail/monitors/{rule_id}/mute",
        json={"muted_until": muted_until},
    )
    assert mute_resp.status_code == 200
    assert mute_resp.json()["muted"] is True
    assert mute_resp.json()["muted_until"] is not None
    # All three endpoints return MonitorDetailResponse through one builder, and
    # all three had to be rewired for the scan join — a GET-only assertion would
    # miss a POST that stopped compiling the pair.
    assert mute_resp.json()["scan_config_id"] is None
    assert mute_resp.json()["scan_name"] is None

    # The summary list reflects the mute too.
    summary_resp = await client.get("/api/v1/projects/monitor-detail/monitors-summary")
    assert summary_resp.status_code == 200
    summary_monitor = next(
        monitor for monitor in summary_resp.json()["monitors"] if monitor["rule_id"] == rule_id
    )
    assert summary_monitor["muted"] is True

    unmute_resp = await client.post(f"/api/v1/projects/monitor-detail/monitors/{rule_id}/unmute")
    assert unmute_resp.status_code == 200
    assert unmute_resp.json()["muted"] is False
    assert unmute_resp.json()["muted_until"] is None
    assert unmute_resp.json()["scan_config_id"] is None
    assert unmute_resp.json()["scan_name"] is None

    missing_resp = await client.get(f"/api/v1/projects/monitor-detail/monitors/{uuid.uuid4()}")
    assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_monitor_detail_names_the_scan_a_rule_is_narrowed_to(client: AsyncClient) -> None:
    """A scan-bound monitor says WHICH scan, on every response that builds it.

    The detail screen never named the binding, while the docs told the reader to
    go and check that scan's own distribution-drift list before trusting
    ``scope_readiness`` — a project verdict that a sibling scan can satisfy on a
    bound rule's behalf. Naming the scan does not fix that verdict; it makes the
    documented workaround reachable.

    ``scope_readiness`` is asserted UNCHANGED here on purpose: it is still the
    project's answer, and re-pointing it at the named scan on this response only
    would give one field name two meanings across two responses — tripl-oxkt.18.
    """
    from tripl.services.scan_service import delete_scan_config

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Monitor Scan", "slug": "monitor-scan", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_id = await _seed_scan_config(project_id, "Legacy iOS scan")
    # A SIBLING scan that does watch a column. This is the shape the issue
    # describes: the bound scan feeds distribution drift nothing, the project
    # reads ready because this one does, and the reader is left to work out
    # which scan to look at.
    sibling_id = await _seed_scan_config(project_id, "Web scan")
    async with TestSessionLocal() as session:
        sibling = await session.get(ScanConfig, sibling_id)
        assert sibling is not None
        sibling.distribution_drift_fields = ["country"]
        await session.commit()

    destination_resp = await client.post(
        "/api/v1/projects/monitor-scan/alert-destinations",
        json={
            "type": "slack",
            "name": "Mon Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/scan",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/monitor-scan/alert-destinations/{destination_id}/rules",
        json={
            "name": "iOS only",
            "scan_config_id": str(scan_id),
            "include_distribution_drifts": True,
            "filters": [],
        },
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    detail = (await client.get(f"/api/v1/projects/monitor-scan/monitors/{rule_id}")).json()
    assert detail["scan_config_id"] == str(scan_id)
    assert detail["scan_name"] == "Legacy iOS scan"
    # The binding is named BESIDE the readiness block, never folded into it. The
    # verdict is still True — the SIBLING scan watches a column, this rule's own
    # scan does not — which is the shipped limitation this task mitigates rather
    # than fixes. It reads identically on the monitors list, which is what stops
    # one field name meaning two things on two responses.
    assert detail["scope_readiness"]["distribution_drift"] is True
    summary = (await client.get("/api/v1/projects/monitor-scan/monitors-summary")).json()
    assert detail["scope_readiness"] == summary["scope_readiness"]

    # Mute and unmute return the same model through the same builder.
    muted_until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    mute_resp = await client.post(
        f"/api/v1/projects/monitor-scan/monitors/{rule_id}/mute",
        json={"muted_until": muted_until},
    )
    assert mute_resp.status_code == 200
    assert mute_resp.json()["scan_config_id"] == str(scan_id)
    assert mute_resp.json()["scan_name"] == "Legacy iOS scan"

    unmute_resp = await client.post(f"/api/v1/projects/monitor-scan/monitors/{rule_id}/unmute")
    assert unmute_resp.status_code == 200
    assert unmute_resp.json()["scan_name"] == "Legacy iOS scan"

    # Deleting the scan unbinds AND disables the rule
    # (``disable_rules_bound_to_scan``), so the monitor must come back nameless
    # and off rather than 404 — the row the OUTER join exists for, produced by
    # the path that actually creates it in production.
    async with TestSessionLocal() as session:
        await delete_scan_config(session, "monitor-scan", scan_id)

    orphaned = await client.get(f"/api/v1/projects/monitor-scan/monitors/{rule_id}")
    assert orphaned.status_code == 200
    assert orphaned.json()["scan_config_id"] is None
    assert orphaned.json()["scan_name"] is None
    assert orphaned.json()["rule_enabled"] is False


# --- enum-shaped query params reject garbage at the edge ---------
#
# ``status`` and ``channel`` on /alert-deliveries bind against native Postgres
# enums, so while they were declared ``str`` a typo travelled to the driver and
# came back as a 500 ("not among the defined enum values"). /alert-inbox filters
# in Python instead, so a typo there silently reported "no incidents" — a wrong
# answer, which is worse. Each test pins BOTH halves of the contract: garbage
# 422s, and the values that worked before still 200 with the same rows.


@pytest.mark.asyncio
async def test_alert_deliveries_reject_unknown_status_and_channel(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Delivery Enums", "slug": "delivery-enums", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    await _seed_destination_rule_delivery(project_id, status=AlertDeliveryStatus.sent.value)

    base = "/api/v1/projects/delivery-enums/alert-deliveries"
    for params in ({"status": "BOGUS"}, {"channel": "BOGUS"}):
        rejected = await client.get(base, params=params)
        assert rejected.status_code == 422, f"{params} was accepted: {rejected.text}"

    # Every member that worked before still selects its rows.
    for status in AlertDeliveryStatus:
        accepted = await client.get(base, params={"status": status.value})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["total"] == (1 if status is AlertDeliveryStatus.sent else 0)

    by_channel = await client.get(base, params={"channel": "slack"})
    assert by_channel.status_code == 200
    assert by_channel.json()["total"] == 1

    other_channel = await client.get(base, params={"channel": "telegram"})
    assert other_channel.status_code == 200
    assert other_channel.json()["total"] == 0


@pytest.mark.asyncio
async def test_alert_inbox_rejects_unknown_status_instead_of_reporting_empty(
    client: AsyncClient,
) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Inbox Enums", "slug": "inbox-enums", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    delivery_id, _destination_id, _rule_id = await _seed_destination_rule_delivery(
        project_id, status=AlertDeliveryStatus.sent.value
    )
    # A correlated item is what promotes a delivery into an inbox group; with no
    # AlertCorrelationState the group's effective status is `open`.
    group_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            AlertDeliveryItem(
                delivery_id=uuid.UUID(delivery_id),
                scope_type="project_total",
                scope_ref=str(uuid.uuid4()),
                scope_name="Project total",
                bucket=datetime.now(UTC) - timedelta(hours=1),
                direction="spike",
                actual_count=20,
                expected_count=10,
                absolute_delta=10,
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        )
        await session.commit()

    base = "/api/v1/projects/inbox-enums/alert-inbox"
    rejected = await client.get(base, params={"status": "BOGUS"})
    assert rejected.status_code == 422, rejected.text

    accepted = await client.get(base, params={"status": "open"})
    assert accepted.status_code == 200
    assert accepted.json()["total"] == 1

    # Filtering still narrows: a real-but-non-matching member returns no groups.
    other = await client.get(base, params={"status": "resolved"})
    assert other.status_code == 200
    assert other.json()["total"] == 0


# ---------------------------------------------------------------------------
# Inbox as a triage surface (epic tripl-oxkt): the card has to say WHAT fired
# and how big it was, an incident a human handled must stay reachable, and an
# action must report what it actually did.
# ---------------------------------------------------------------------------


async def _seed_inbox_fixture(
    project_id: uuid.UUID,
    *,
    rule_names: tuple[str, ...] = ("Rule",),
) -> tuple[uuid.UUID, list[uuid.UUID], uuid.UUID]:
    """Insert the infra an inbox group hangs off.

    Returns ``(scan_config_id, rule_ids, destination_id)``.
    """
    async with TestSessionLocal() as session:
        data_source = DataSource(
            name="Inbox DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan_config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            name="Inbox Scan",
            base_query="SELECT * FROM events",
            time_column="time",
            interval="1h",
            sigma_threshold=3.0,
            min_expected_count=10,
        )
        destination = AlertDestination(
            project_id=project_id,
            type="slack",
            name="Inbox Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        settings = ProjectAnomalySettings(
            project_id=project_id,
            anomaly_detection_enabled=True,
            sigma_threshold=3.0,
            min_expected_count=10,
        )
        session.add_all([scan_config, destination, settings])
        await session.flush()
        rules = [
            AlertRule(destination_id=destination.id, name=name, enabled=True) for name in rule_names
        ]
        session.add_all(rules)
        await session.commit()
        return scan_config.id, [rule.id for rule in rules], destination.id


async def _seed_inbox_delivery(
    project_id: uuid.UUID,
    *,
    scan_config_id: uuid.UUID,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
    created_at: datetime,
    items: list[dict[str, object]],
) -> uuid.UUID:
    """Insert one delivery with its items. ``created_at`` is set explicitly so a
    test can place a group inside or outside the inbox's lookback window."""
    async with TestSessionLocal() as session:
        delivery = AlertDelivery(
            project_id=project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_id,
            status="sent",
            channel="slack",
            matched_count=len(items),
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(delivery)
        await session.flush()
        for item in items:
            session.add(AlertDeliveryItem(delivery_id=delivery.id, **item))
        await session.commit()
        return delivery.id


def _drop_tz(value: datetime) -> datetime:
    """Compare timestamps regardless of tzinfo.

    ``TimestampMixin`` uses a plain ``DateTime(timezone=True)`` and SQLite hands
    those back NAIVE, so a seeded aware timestamp and the one the API echoes back
    after the round trip cannot be compared directly. Everything is UTC.
    """
    return value.replace(tzinfo=None)


def _inbox_item(
    *,
    scope_type: str,
    bucket: datetime,
    percent_delta: float,
    correlation_group_id: uuid.UUID,
    actual_count: float = 20,
    expected_count: float = 10,
    scope_name: str | None = None,
    scope_ref: str | None = None,
) -> dict[str, object]:
    return {
        "scope_type": scope_type,
        "scope_ref": scope_ref if scope_ref is not None else str(uuid.uuid4()),
        "scope_name": scope_name if scope_name is not None else f"{scope_type} scope",
        "bucket": bucket,
        "direction": "spike" if percent_delta >= 0 else "drop",
        "actual_count": actual_count,
        "expected_count": expected_count,
        "absolute_delta": actual_count - expected_count,
        "percent_delta": percent_delta,
        "correlation_group_id": correlation_group_id,
    }


@pytest.mark.asyncio
async def test_inbox_group_reports_magnitude_scope_types_and_rules(client: AsyncClient) -> None:
    """Two firings of one scope were near-identical cards: status, item count and
    names, but never what fired or how big it was."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Inbox Magnitude", "slug": "inbox-magnitude", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(
        project_id, rule_names=("Volume rule", "Release rule")
    )
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    first_at = now - timedelta(days=3)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=first_at,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(days=3),
                percent_delta=120.0,
                correlation_group_id=group_id,
            )
        ],
    )
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[1],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="release_regression",
                bucket=now - timedelta(hours=1),
                percent_delta=-42.5,
                actual_count=3,
                expected_count=8,
                correlation_group_id=group_id,
            )
        ],
    )

    resp = await client.get("/api/v1/projects/inbox-magnitude/alert-inbox")
    assert resp.status_code == 200
    group = resp.json()["items"][0]

    # Magnitude of the NEWEST item, so the card reports the firing it headlines.
    assert group["actual_count"] == 3
    assert group["expected_count"] == 8
    assert group["percent_delta"] == -42.5
    # ...while the worst deviation anywhere in the group survives for ordering.
    assert group["max_abs_percent_delta"] == 120.0
    # `scope_type` alone cannot label a group that mixes types.
    assert group["scope_type"] == "release_regression"
    assert group["scope_types"] == ["event", "release_regression"]
    # Rule id and rule name travel as ONE pair, sorted by name: the parallel
    # arrays sorted by different keys could not be zipped, so the card linked a
    # name to whichever monitor happened to sort first.
    assert group["rules"] == [
        {"id": str(rule_ids[1]), "name": "Release rule"},
        {"id": str(rule_ids[0]), "name": "Volume rule"},
    ]
    assert group["rule_names"] == ["Release rule", "Volume rule"]
    assert "rule_ids" not in group
    # How long this has been going, not just when it last spoke.
    assert _drop_tz(datetime.fromisoformat(group["first_delivery_at"])) == _drop_tz(first_at)
    assert _drop_tz(datetime.fromisoformat(group["latest_delivery_at"])) > _drop_tz(first_at)
    # Nobody has acted, so there is no name to show.
    assert group["acted_by_name"] is None


@pytest.mark.asyncio
async def test_lapsed_mute_stops_reporting_muted_until(client: AsyncClient) -> None:
    """A mute that has expired reported status `open` AND `muted_until` in the
    past, so the card rendered two contradictory claims."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Lapsed Mute", "slug": "lapsed-mute", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=2),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=2),
                percent_delta=90.0,
                correlation_group_id=group_id,
            )
        ],
    )

    # Mute already lifted.
    async with TestSessionLocal() as session:
        session.add(
            AlertCorrelationState(
                project_id=project_id,
                correlation_group_id=group_id,
                status="muted",
                muted_until=now - timedelta(days=1),
            )
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/lapsed-mute/alert-inbox")
    assert resp.status_code == 200
    group = resp.json()["items"][0]
    assert group["status"] == "open"
    assert group["muted_until"] is None

    # A mute still in force keeps reporting when it lifts.
    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        assert state is not None
        state.muted_until = now + timedelta(days=1)
        await session.commit()

    resp = await client.get("/api/v1/projects/lapsed-mute/alert-inbox")
    group = resp.json()["items"][0]
    assert group["status"] == "muted"
    assert group["muted_until"] is not None


@pytest.mark.asyncio
async def test_handled_group_never_outranks_an_untouched_open_one(client: AsyncClient) -> None:
    """Open work sorts above handled work, full stop.

    Ranking on "newest of last-spoke and last-acted-on" stamped `acted_at` for
    acknowledge, resolve, mute, reopen and false_positive alike, so the last N
    incidents a human TRIAGED took the top N ranks and pushed every untouched one
    off page one — a worse failure than the sinking it was meant to fix, and it
    did not fix that either (a mute freezes the key just the same, minutes later)
    — tripl-oxkt.2.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Handled Sort", "slug": "handled-sort", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    handled_group = uuid.uuid4()
    fresh_group = uuid.uuid4()

    async def seed(group_id: uuid.UUID, created_at: datetime) -> None:
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=created_at,
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=created_at,
                    percent_delta=100.0,
                    correlation_group_id=group_id,
                )
            ],
        )

    # The one a human will handle is the NEWER of the two, so activity alone
    # would put it on top after the action.
    await seed(fresh_group, now - timedelta(days=5))
    await seed(handled_group, now - timedelta(minutes=5))

    # Before anyone acts, the newest delivery leads.
    resp = await client.get("/api/v1/projects/handled-sort/alert-inbox")
    assert [item["correlation_group_id"] for item in resp.json()["items"]] == [
        str(handled_group),
        str(fresh_group),
    ]

    resolve_resp = await client.post(
        f"/api/v1/projects/handled-sort/alert-inbox/{handled_group}/actions",
        json={"action": "resolve"},
    )
    assert resolve_resp.status_code == 200

    # (a) A resolved group must NOT outrank an untouched open one, however much
    # more recently it spoke or was acted on.
    resp = await client.get("/api/v1/projects/handled-sort/alert-inbox")
    assert [item["correlation_group_id"] for item in resp.json()["items"]] == [
        str(fresh_group),
        str(handled_group),
    ]


@pytest.mark.asyncio
async def test_muted_group_stays_findable_behind_the_status_filter(client: AsyncClient) -> None:
    """Reaching a muted incident is the status filter's job, not the sort's.

    A muted group is suppressed, so it records no further deliveries and its
    activity key is frozen: no ordering rule can keep it on page one while open
    incidents keep firing. Sinking it is therefore expected — what must NOT
    happen is it becoming unreachable (.2).
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Muted Reach", "slug": "muted-reach", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    muted_group = uuid.uuid4()

    async def seed(group_id: uuid.UUID, created_at: datetime) -> None:
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=created_at,
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=created_at,
                    percent_delta=100.0,
                    correlation_group_id=group_id,
                )
            ],
        )

    await seed(muted_group, now - timedelta(hours=6))
    mute_resp = await client.post(
        f"/api/v1/projects/muted-reach/alert-inbox/{muted_group}/actions",
        json={"action": "mute", "muted_until": (now + timedelta(days=7)).isoformat()},
    )
    assert mute_resp.status_code == 200
    # The mute is IN FORCE, so both readings of "is it muted?" agree.
    assert mute_resp.json()["group"]["status"] == "muted"
    assert mute_resp.json()["group"]["muted"] is True
    assert mute_resp.json()["group"]["muted_until"] is not None

    # Five newer incidents across distinct groups bury it in the default view.
    for index in range(5):
        await seed(uuid.uuid4(), now - timedelta(minutes=index + 1))

    default_view = await client.get("/api/v1/projects/muted-reach/alert-inbox")
    listed = [item["correlation_group_id"] for item in default_view.json()["items"]]
    assert listed[-1] == str(muted_group)

    # (b) ...and the status filter still hands it straight back.
    filtered = await client.get(
        "/api/v1/projects/muted-reach/alert-inbox", params={"status": "muted"}
    )
    assert filtered.status_code == 200
    assert [item["correlation_group_id"] for item in filtered.json()["items"]] == [str(muted_group)]


@pytest.mark.asyncio
async def test_false_positive_reports_how_many_scopes_it_tightened(client: AsyncClient) -> None:
    """`release_regression` is not in RATCHETABLE_SCOPE_TYPES, so the button
    wrote nothing on 10 of 57 production groups while promising a permanent
    detection change. The count says which happened."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Override Count", "slug": "override-count", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    regression_group = uuid.uuid4()
    event_group = uuid.uuid4()
    for group_id, scope_type in ((regression_group, "release_regression"), (event_group, "event")):
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=now - timedelta(hours=1),
            items=[
                _inbox_item(
                    scope_type=scope_type,
                    bucket=now - timedelta(hours=1),
                    percent_delta=100.0,
                    correlation_group_id=group_id,
                )
            ],
        )

    regression_resp = await client.post(
        f"/api/v1/projects/override-count/alert-inbox/{regression_group}/actions",
        json={"action": "false_positive"},
    )
    assert regression_resp.status_code == 200
    # The incident is still dismissed — only the ratchet is a no-op.
    assert regression_resp.json()["group"]["status"] == "false_positive"
    assert regression_resp.json()["overrides_written"] == 0

    event_resp = await client.post(
        f"/api/v1/projects/override-count/alert-inbox/{event_group}/actions",
        json={"action": "false_positive"},
    )
    assert event_resp.status_code == 200
    assert event_resp.json()["overrides_written"] == 1

    async with TestSessionLocal() as session:
        overrides = list(
            (
                await session.execute(
                    select(AnomalyScopeOverride).where(
                        AnomalyScopeOverride.project_id == project_id
                    )
                )
            ).scalars()
        )
        assert [override.scope_type for override in overrides] == ["event"]


@pytest.mark.asyncio
async def test_note_action_records_the_note_and_nothing_else(client: AsyncClient) -> None:
    """Documenting an incident used to require taking an action, and the stamp at
    the end of apply_alert_inbox_action was unconditional — so a note-only save
    forged the "already handled by X" line the card derives from acted_at
    ."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Note Only", "slug": "note-only", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )

    # A note on a never-touched incident leaves it open and unstamped.
    note_resp = await client.post(
        f"/api/v1/projects/note-only/alert-inbox/{group_id}/actions",
        json={"action": "note", "note": "Watching the rollout"},
    )
    assert note_resp.status_code == 200
    group = note_resp.json()["group"]
    assert group["status"] == "open"
    assert group["note"] == "Watching the rollout"
    assert group["acted_at"] is None
    assert group["acted_by"] is None
    assert group["acted_by_name"] is None
    # `None`, not 0: a note cannot tighten anything, and reporting 0 let the UI
    # render "no scopes tightened" — a detection verdict — after an action that
    # never went near detection.
    assert note_resp.json()["overrides_written"] is None

    # A real decision stamps it, and names the human who took it.
    ack_resp = await client.post(
        f"/api/v1/projects/note-only/alert-inbox/{group_id}/actions",
        json={"action": "acknowledge"},
    )
    assert ack_resp.status_code == 200
    assert ack_resp.json()["group"]["acted_by_name"] == "Test User"
    acted_at = ack_resp.json()["group"]["acted_at"]
    assert acted_at is not None

    # A later note replaces the text without re-stamping the decision.
    second_note = await client.post(
        f"/api/v1/projects/note-only/alert-inbox/{group_id}/actions",
        json={"action": "note", "note": "Still watching"},
    )
    assert second_note.status_code == 200
    assert second_note.json()["group"]["status"] == "acknowledged"
    assert second_note.json()["group"]["note"] == "Still watching"
    assert _drop_tz(datetime.fromisoformat(second_note.json()["group"]["acted_at"])) == _drop_tz(
        datetime.fromisoformat(acted_at)
    )


@pytest.mark.asyncio
async def test_inbox_group_route_resolves_a_group_outside_the_lookback_window(
    client: AsyncClient,
) -> None:
    """An alert message deep-links its incident and the reader opens it late, so
    this route must ignore INBOX_LOOKBACK_DAYS — the links that most need to land
    are the old ones."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Deep Link", "slug": "deep-link", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    aged_at = now - timedelta(days=60)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=aged_at,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=aged_at,
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )

    # The list window has long since dropped it.
    listed = await client.get("/api/v1/projects/deep-link/alert-inbox")
    assert listed.status_code == 200
    assert listed.json()["total"] == 0

    resolved = await client.get(f"/api/v1/projects/deep-link/alert-inbox/{group_id}")
    assert resolved.status_code == 200
    group = resolved.json()
    assert group["correlation_group_id"] == str(group_id)
    assert group["status"] == "open"
    assert group["item_count"] == 1
    assert group["percent_delta"] == 100.0

    unknown = await client.get(f"/api/v1/projects/deep-link/alert-inbox/{uuid.uuid4()}")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_inbox_action_succeeds_on_a_group_outside_the_lookback_window(
    client: AsyncClient,
) -> None:
    """The action committed and THEN rebuilt the whole inbox to find the group it
    had just written, so an aged incident 404'd after a successful write and the
    UI reported an error for a change that landed."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Aged Action", "slug": "aged-action", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    aged_at = datetime.now(UTC) - timedelta(days=45)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=aged_at,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=aged_at,
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )

    resp = await client.post(
        f"/api/v1/projects/aged-action/alert-inbox/{group_id}/actions",
        json={"action": "resolve", "note": "Fixed in the next release"},
    )
    assert resp.status_code == 200
    assert resp.json()["group"]["status"] == "resolved"
    assert resp.json()["group"]["note"] == "Fixed in the next release"
    # An acknowledge/resolve cannot tighten a scope, so it reports "not
    # applicable" rather than a count of zero.
    assert resp.json()["overrides_written"] is None


@pytest.mark.asyncio
async def test_an_indefinite_mute_stays_reachable_after_its_deliveries_age_out(
    client: AsyncClient,
) -> None:
    """Muting is the act of stopping deliveries, and the list only sees
    deliveries — so 30 days later the incident dropped out of every filter while
    its suppression went on being enforced forever, taking the only Unmute
    control with it."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Silenced Window", "slug": "silenced-window", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    aged_at = datetime.now(UTC) - timedelta(days=45)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=aged_at,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=aged_at,
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )

    # `muted_until: null` IS the indefinite mute, and it is the one status
    # nothing ever releases: `_reopen_closed_incidents` skips it on purpose
    # and `_suppressed_correlation_group_ids` has no time bound.
    mute = await client.post(
        f"/api/v1/projects/silenced-window/alert-inbox/{group_id}/actions",
        json={"action": "mute", "muted_until": None},
    )
    assert mute.status_code == 200
    assert mute.json()["group"]["muted"] is True

    listing = await client.get("/api/v1/projects/silenced-window/alert-inbox")
    assert listing.status_code == 200
    assert [item["correlation_group_id"] for item in listing.json()["items"]] == [str(group_id)]

    # The filter the docs name as the only route back to Unmute. Before the fix
    # this was empty, because the status filter runs over groups already built
    # from windowed rows — it can subtract, never add.
    muted_only = await client.get(
        "/api/v1/projects/silenced-window/alert-inbox", params={"status": "muted"}
    )
    assert muted_only.status_code == 200
    assert [item["correlation_group_id"] for item in muted_only.json()["items"]] == [str(group_id)]
    assert muted_only.json()["total"] == 1

    # …and once the decision is lifted the incident is an aged incident like any
    # other again, i.e. OUT of the list. The rescue is gated on the suppression
    # still being in force, not on a state row merely existing.
    reopen = await client.post(
        f"/api/v1/projects/silenced-window/alert-inbox/{group_id}/actions",
        json={"action": "reopen"},
    )
    assert reopen.status_code == 200
    after = await client.get("/api/v1/projects/silenced-window/alert-inbox")
    assert after.json()["items"] == []
    assert after.json()["total"] == 0


@pytest.mark.asyncio
async def test_a_lapsed_mute_on_an_aged_incident_is_not_rescued(
    client: AsyncClient,
) -> None:
    """The rescue reads `_effective_inbox_status`, not `state.status`.

    A mute whose expiry has passed is OPEN again — the whole of tripl-oxkt.20 —
    and an open incident that stopped delivering is exactly the resolved-by-time
    case the 30-day window exists to forget. Keying the rescue on the stored
    string instead would turn the inbox into an unbounded archive of everything
    that ever fired and was once muted.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Lapsed Window", "slug": "lapsed-window", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    aged_at = datetime.now(UTC) - timedelta(days=45)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=aged_at,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=aged_at,
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )
    # The state, not the request, is this test's subject: what it needs is a
    # row carrying `muted` with an expiry that has passed. It used to POST the
    # lapsed instant directly, which the route now refuses with a 422 — a mute
    # whose end has already gone by is a silence no reader can honour, so it is
    # rejected at the door (the refusal itself is pinned in
    # test_batch4_services.py). So the mute is made the way an operator makes
    # one, with a real future expiry, and then TIME is what passes — written
    # onto the row directly, the same way ``test_lapsed_mute_stops_reporting_
    # muted_until`` above ages its own state. Nothing sweeps an expired mute,
    # which is exactly the point: the lapse only ever exists as a stored
    # instant that a reader compares against now.
    mute = await client.post(
        f"/api/v1/projects/lapsed-window/alert-inbox/{group_id}/actions",
        json={
            "action": "mute",
            "muted_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert mute.status_code == 200

    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        assert state is not None
        assert state.status == "muted"
        state.muted_until = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    listing = await client.get("/api/v1/projects/lapsed-window/alert-inbox")
    assert listing.json()["items"] == []
    assert listing.json()["total"] == 0


@pytest.mark.asyncio
async def test_inbox_says_where_its_window_really_starts_when_the_cap_shortens_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page claims "last 30 days"; the cap can make that untrue in silence.

    ``INBOX_MAX_SOURCE_ITEMS`` is applied to DELIVERY-ordered item rows before
    grouping, so a project loud enough to exceed it gets a window shorter than
    the documented one, with the oldest incidents simply absent — and absent
    looks exactly like handled. The response now names the instant the visible
    window really starts, and names nothing when the documented window held.

    Both branches are pinned, because "exactly at the cap" and "cut short by the
    cap" are the two the limit+1 probe exists to tell apart: fetching only the
    cap makes them identical and the honest case would report truncation too.
    """
    from tripl.services import _alerting_deliveries

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Capped Window", "slug": "capped-window", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    # Three incidents, each one item, each a day apart and all well inside the
    # 30-day window: the only bound that can drop one here is the cap.
    sent_at = [now - timedelta(days=days) for days in (1, 2, 3)]
    group_ids = [uuid.uuid4() for _ in sent_at]
    for created_at, group_id in zip(sent_at, group_ids, strict=True):
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=created_at,
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=created_at,
                    percent_delta=100.0,
                    correlation_group_id=group_id,
                )
            ],
        )

    monkeypatch.setattr(_alerting_deliveries, "INBOX_MAX_SOURCE_ITEMS", 2)
    truncated = await client.get("/api/v1/projects/capped-window/alert-inbox")
    assert truncated.status_code == 200
    body = truncated.json()
    # The two newest survive; the third is gone with no other explanation.
    assert [item["correlation_group_id"] for item in body["items"]] == [
        str(group_ids[0]),
        str(group_ids[1]),
    ]
    assert body["total"] == 2
    # …and the start reported is the OLDEST ADMITTED row's, not the cutoff and
    # not the first rejected row's.
    assert body["window_truncated_at"] is not None
    assert datetime.fromisoformat(body["window_truncated_at"]) == sent_at[1]

    # Exactly at the cap is NOT truncation: nothing was dropped, so the page is
    # entitled to go on saying "last 30 days".
    monkeypatch.setattr(_alerting_deliveries, "INBOX_MAX_SOURCE_ITEMS", 3)
    exact = await client.get("/api/v1/projects/capped-window/alert-inbox")
    assert exact.status_code == 200
    assert exact.json()["total"] == 3
    assert exact.json()["window_truncated_at"] is None


@pytest.mark.asyncio
async def test_inbox_group_reports_no_baseline_as_null_not_zero(client: AsyncClient) -> None:
    """A scope firing from a ZERO baseline is the loudest class there is, and the
    stored 0.0 is a placeholder, not a measurement.

    Copied off the column, it rendered "0%" on the card and sorted as the
    SMALLEST deviation in the group, while the delivery the card expands to
    correctly reported null — one payload family answering the same question two
    ways (.27).
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "No Baseline", "slug": "no-baseline", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    barren_group = uuid.uuid4()
    mixed_group = uuid.uuid4()

    # Nothing in this group ever had a baseline.
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=0.0,
                actual_count=37,
                expected_count=0,
                correlation_group_id=barren_group,
            )
        ],
    )
    # This one's NEWEST item has no baseline, but an older one did — so the
    # headline number is null while the group still has a worst measured
    # deviation to be ordered by.
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=4),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=4),
                percent_delta=-64.0,
                actual_count=9,
                expected_count=25,
                correlation_group_id=mixed_group,
            )
        ],
    )
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=2),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=2),
                percent_delta=0.0,
                actual_count=12,
                expected_count=0,
                correlation_group_id=mixed_group,
            )
        ],
    )

    resp = await client.get("/api/v1/projects/no-baseline/alert-inbox")
    assert resp.status_code == 200
    groups = {group["correlation_group_id"]: group for group in resp.json()["items"]}

    barren = groups[str(barren_group)]
    assert barren["expected_count"] == 0
    assert barren["percent_delta"] is None
    # No row in the group has a baseline, so there is no measured deviation to be
    # the largest — emphatically not 0.0, which sorted it last.
    assert barren["max_abs_percent_delta"] is None

    mixed = groups[str(mixed_group)]
    assert mixed["percent_delta"] is None
    assert mixed["max_abs_percent_delta"] == 64.0

    # The deep link reads the same way.
    detail = await client.get(f"/api/v1/projects/no-baseline/alert-inbox/{barren_group}")
    assert detail.status_code == 200
    assert detail.json()["percent_delta"] is None
    assert detail.json()["max_abs_percent_delta"] is None


@pytest.mark.asyncio
async def test_inbox_action_returns_what_the_list_returned(client: AsyncClient) -> None:
    """Acknowledge must not redraw the card with different numbers.

    The action rebuilt the group from ALL of its rows while the list built it
    from rows inside INBOX_LOOKBACK_DAYS, so an incident with deliveries on both
    sides of the cutoff re-rendered with a different item_count, delivery_count,
    first_delivery_at, latest_bucket, max_abs_percent_delta, scope_names, rules
    and scan_names — with no state change that explained any of it.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Straddle", "slug": "straddle", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    group_id = uuid.uuid4()

    # Outside the window: bigger, older, and with its own scope name.
    outside_at = now - timedelta(days=45)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=outside_at,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=outside_at,
                percent_delta=900.0,
                correlation_group_id=group_id,
            )
        ],
    )
    # Inside the window.
    inside_at = now - timedelta(hours=3)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=inside_at,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=inside_at,
                percent_delta=110.0,
                correlation_group_id=group_id,
            )
        ],
    )

    listed = await client.get("/api/v1/projects/straddle/alert-inbox")
    assert listed.status_code == 200
    before = listed.json()["items"][0]
    assert before["item_count"] == 1
    assert before["max_abs_percent_delta"] == 110.0

    acted = await client.post(
        f"/api/v1/projects/straddle/alert-inbox/{group_id}/actions",
        json={"action": "acknowledge"},
    )
    assert acted.status_code == 200
    after = acted.json()["group"]

    # Everything the window decides is unchanged by the click.
    windowed_fields = (
        "item_count",
        "delivery_count",
        "first_delivery_at",
        "latest_bucket",
        "latest_delivery_at",
        "percent_delta",
        "max_abs_percent_delta",
        "scope_names",
        "scope_types",
        "rules",
        "rule_names",
        "scan_names",
    )
    assert {field: after[field] for field in windowed_fields} == {
        field: before[field] for field in windowed_fields
    }
    # ...and the list agrees with itself afterwards.
    relisted = (await client.get("/api/v1/projects/straddle/alert-inbox")).json()["items"][0]
    assert {field: relisted[field] for field in windowed_fields} == {
        field: before[field] for field in windowed_fields
    }
    # Only what the action actually decided moved.
    assert before["status"] == "open"
    assert after["status"] == "acknowledged"

    # The deep link is the one reading that DOES see the whole history — that is
    # its purpose, and it is why the action's window had to be explicit.
    deep = await client.get(f"/api/v1/projects/straddle/alert-inbox/{group_id}")
    assert deep.status_code == 200
    assert deep.json()["item_count"] == 2
    assert deep.json()["max_abs_percent_delta"] == 900.0


@pytest.mark.asyncio
async def test_inbox_never_reports_an_operators_email(client: AsyncClient) -> None:
    """`acted_by_name` used to fall back to the acting operator's EMAIL.

    Every project member can read the inbox, and this endpoint previously exposed
    only an opaque UUID, so the fallback turned incident cards into a roster of
    colleagues' addresses. An unnamed operator gets no name.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Nameless", "slug": "nameless", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )

    # `name` is nullable on User, which is the only way acted_by fails to
    # resolve: the column is an FK, so it cannot point at a row that is not there.
    async with TestSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        user.name = None
        await session.commit()

    acted = await client.post(
        f"/api/v1/projects/nameless/alert-inbox/{group_id}/actions",
        json={"action": "acknowledge"},
    )
    assert acted.status_code == 200
    group = acted.json()["group"]
    # The decision is still attributed — by id, which is not a personal detail.
    assert group["acted_by"] is not None
    assert group["acted_by_name"] is None
    assert "test@example.com" not in acted.text


@pytest.mark.asyncio
async def test_inbox_group_of_another_project_is_not_reachable(client: AsyncClient) -> None:
    """A correlation group is only ever addressed under its own project's slug.

    Both single-group routes filter on ``AlertDelivery.project_id``, so asking
    for somebody else's incident is a 404 rather than a cross-project read.
    """
    owner_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Group Owner", "slug": "group-owner", "description": ""},
    )
    await client.post(
        "/api/v1/projects",
        json={"name": "Group Stranger", "slug": "group-stranger", "description": ""},
    )
    owner_id = uuid.UUID(owner_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(owner_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _seed_inbox_delivery(
        owner_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )

    assert (
        await client.get(f"/api/v1/projects/group-owner/alert-inbox/{group_id}")
    ).status_code == 200

    stranger_read = await client.get(f"/api/v1/projects/group-stranger/alert-inbox/{group_id}")
    assert stranger_read.status_code == 404
    stranger_write = await client.post(
        f"/api/v1/projects/group-stranger/alert-inbox/{group_id}/actions",
        json={"action": "resolve"},
    )
    assert stranger_write.status_code == 404
    # ...and the write really did not land under the other project either.
    async with TestSessionLocal() as session:
        assert (
            await session.scalar(
                select(AlertCorrelationState).where(
                    AlertCorrelationState.correlation_group_id == group_id
                )
            )
        ) is None


@pytest.mark.asyncio
async def test_note_action_without_a_note_is_rejected(client: AsyncClient) -> None:
    """`{"action": "note"}` with no note was a silent 200 no-op.

    The note write is conditional on ``note is not None``, so the request looked
    accepted, changed nothing, and still inserted a correlation-state row. It
    used to mirror a guard on mute/muted_until; that one is gone, because a null
    ``muted_until`` is now the indefinite mute rather than a missing field.
    This guard stands alone and is still needed.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Empty Note", "slug": "empty-note", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )

    missing = await client.post(
        f"/api/v1/projects/empty-note/alert-inbox/{group_id}/actions",
        json={"action": "note"},
    )
    assert missing.status_code == 422
    # Nothing was written on the way to rejecting it.
    async with TestSessionLocal() as session:
        assert (
            await session.scalar(
                select(AlertCorrelationState).where(
                    AlertCorrelationState.correlation_group_id == group_id
                )
            )
        ) is None

    # An EMPTY note is still valid — it is the documented way to clear one.
    await client.post(
        f"/api/v1/projects/empty-note/alert-inbox/{group_id}/actions",
        json={"action": "note", "note": "Was investigating"},
    )
    cleared = await client.post(
        f"/api/v1/projects/empty-note/alert-inbox/{group_id}/actions",
        json={"action": "note", "note": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["group"]["note"] is None


async def _seed_inbox_group(client: AsyncClient, slug: str, name: str) -> uuid.UUID:
    """One project, one seeded delivery, one correlation group to act on.

    The indefinite-mute tests below both need exactly this and nothing else, and
    the eight-line seed is the bulk of either one.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": name, "slug": slug, "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=100.0,
                correlation_group_id=group_id,
            )
        ],
    )
    return group_id


@pytest.mark.asyncio
async def test_inbox_mute_without_an_expiry_is_an_indefinite_mute(client: AsyncClient) -> None:
    """`{"action": "mute"}` with no expiry means "muted until I unmute".

    The validator used to reject it with a 422, so an operator watching a scope
    they already KNEW was broken had to invent an end date, and got paged again
    the moment they guessed too short. A null ``muted_until`` on a
    muted row is the encoding, and it has to survive the whole round trip: the
    column stays NULL, the group still reads ``muted``, and the card gets
    ``muted: true`` with ``muted_until: null`` — which is the pair the frontend
    renders as "muted indefinitely" instead of "muted until <date>".
    """
    group_id = await _seed_inbox_group(client, "mute-forever", "Mute Forever")

    mute_resp = await client.post(
        f"/api/v1/projects/mute-forever/alert-inbox/{group_id}/actions",
        json={"action": "mute"},
    )
    assert mute_resp.status_code == 200
    group = mute_resp.json()["group"]
    assert group["status"] == "muted"
    assert group["muted"] is True
    # NOT coerced into some default expiry on the way in.
    assert group["muted_until"] is None

    # The stored row carries the same NULL, so nothing downstream can mistake it
    # for a mute that ran out.
    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        assert state is not None
        assert state.status == "muted"
        assert state.muted_until is None

    # An indefinitely muted group sinks in the default view forever — it records
    # no further deliveries — so `?status=muted` is the ONLY practical way back
    # to it, and it must not read as "open" to the filter.
    filtered = await client.get(
        "/api/v1/projects/mute-forever/alert-inbox", params={"status": "muted"}
    )
    assert filtered.status_code == 200
    assert [item["correlation_group_id"] for item in filtered.json()["items"]] == [str(group_id)]

    detail = await client.get(f"/api/v1/projects/mute-forever/alert-inbox/{group_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "muted"
    assert detail.json()["muted"] is True
    assert detail.json()["muted_until"] is None


@pytest.mark.asyncio
async def test_reopen_lifts_an_indefinite_inbox_mute(client: AsyncClient) -> None:
    """Reopen is the ONLY exit from a mute with no expiry — nothing else can end it.

    A timed mute is released by the passage of time in
    ``_suppressed_correlation_group_ids``; an indefinite one is released by a
    human and by nobody else, so if ``reopen`` ever stopped nulling the column or
    stopped resetting the status, the operator would hold an unbreakable mute
    with no way out through the API. That is a worse failure than the one
    tripl-a50u fixed, and it is what this test stands guard over.
    """
    group_id = await _seed_inbox_group(client, "unmute-forever", "Unmute Forever")

    mute_resp = await client.post(
        f"/api/v1/projects/unmute-forever/alert-inbox/{group_id}/actions",
        json={"action": "mute"},
    )
    assert mute_resp.status_code == 200
    assert mute_resp.json()["group"]["muted"] is True

    reopen_resp = await client.post(
        f"/api/v1/projects/unmute-forever/alert-inbox/{group_id}/actions",
        json={"action": "reopen"},
    )
    assert reopen_resp.status_code == 200
    group = reopen_resp.json()["group"]
    assert group["status"] == "open"
    assert group["muted"] is False
    assert group["muted_until"] is None

    async with TestSessionLocal() as session:
        state = await session.scalar(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        )
        assert state is not None
        assert state.status == "open"
        assert state.muted_until is None


async def _seed_inbox_groups(
    client: AsyncClient, slug: str, name: str, *, count: int
) -> list[uuid.UUID]:
    """One project holding ``count`` distinct, never-acted-on correlation groups.

    Never-acted-on ON PURPOSE: none of them has an ``AlertCorrelationState`` row
    until something acts on it, and that is what a real bulk selection is mostly
    made of. It is also the case the bulk route's id validation has to get right
    — validating on the presence of a state row would 404 every one of these.

    Deliveries are staggered an hour apart so the inbox ordering is deterministic
    and the returned list is newest-first, matching what the list endpoint hands
    the operator to tick.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": name, "slug": slug, "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    group_ids: list[uuid.UUID] = []
    for index in range(count):
        group_id = uuid.uuid4()
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=now - timedelta(hours=index + 1),
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=now - timedelta(hours=index + 1),
                    percent_delta=100.0 + index,
                    correlation_group_id=group_id,
                )
            ],
        )
        group_ids.append(group_id)
    return group_ids


@pytest.mark.asyncio
async def test_bulk_action_copies_the_decision_into_every_selected_incident(
    client: AsyncClient,
) -> None:
    """The batch is a SHORTCUT for N clicks, so every selected row ends up identical.

    tripl-gpfr deliberately built no group object and no new table: the note,
    ``acted_at`` and ``acted_by`` are COPIED into each incident's own state, so
    afterwards nothing distinguishes a bulk-acknowledged incident from a
    hand-clicked one. That is the whole contract, and these three assertions are
    it — same status, same note, same stamp, on every selected row.

    The single shared ``acted_at`` matters on its own: the service takes ONE
    ``now`` before the loop, because the batch was one human decision and the
    inbox has to be able to show it as one rather than as N decisions milliseconds
    apart.

    Reaching this route at all also proves the registration order: "bulk-actions"
    sits above ``/alert-inbox/{correlation_group_id}``, and if it did not, FastAPI
    would try to parse the literal segment as a UUID and 422 a correctly spelled
    request.
    """
    group_ids = await _seed_inbox_groups(client, "bulk-triage", "Bulk Triage", count=3)

    resp = await client.post(
        "/api/v1/projects/bulk-triage/alert-inbox/bulk-actions",
        json={
            "correlation_group_ids": [str(group_id) for group_id in group_ids],
            "action": "acknowledge",
            "note": "Known deploy window",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    # Cards come back in the order they were asked for, so the client can pair
    # request to response without matching on id.
    assert [group["correlation_group_id"] for group in body["groups"]] == [
        str(group_id) for group_id in group_ids
    ]
    # A body, not the house 204 — these cards replace the ones the operator is
    # looking at, and re-listing the whole inbox to redraw three rows it just
    # changed is what the body exists to avoid.
    for group in body["groups"]:
        assert group["status"] == "acknowledged"
        assert group["note"] == "Known deploy window"
        assert group["acted_by_name"] == "Test User"
    # ``false_positive`` is refused on this route, so nothing here can ever ratchet
    # a threshold and the count is structurally null — never 0, which a shared
    # client handler would render as "no scopes tightened".
    assert body["overrides_written"] is None
    assert body["batch_id"] is not None

    async with TestSessionLocal() as session:
        states = (
            (
                await session.execute(
                    select(AlertCorrelationState).where(
                        AlertCorrelationState.correlation_group_id.in_(group_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        # A state row is created lazily for each — none of these groups had one.
        assert len(states) == 3
        assert {state.status for state in states} == {"acknowledged"}
        assert {state.note for state in states} == {"Known deploy window"}
        # ONE decision, so ONE timestamp and one actor across the whole batch.
        assert len({state.acted_at for state in states}) == 1
        assert states[0].acted_at is not None
        assert len({state.acted_by for state in states}) == 1
        assert states[0].acted_by is not None


@pytest.mark.asyncio
async def test_bulk_action_404s_the_whole_request_and_mutates_nothing(
    client: AsyncClient,
) -> None:
    """One unknown id fails the WHOLE batch, before anything has been written.

    The single-incident route validates inside a call that has already done work,
    which is harmless at N=1. At N groups that ordering would leave a half-applied
    batch sitting behind a 404, so the bulk route validates every id in one query
    up front and mutates only after all of them pass. There is no
    partial success and no per-item error array — nothing in this repo has one.

    The second half of this test is the half that matters: a 404 that had already
    acknowledged two of the three incidents would be far worse than the error it
    reports, because the operator has no way to see it happened.
    """
    group_ids = await _seed_inbox_groups(client, "bulk-partial", "Bulk Partial", count=2)
    unknown_id = uuid.uuid4()

    resp = await client.post(
        "/api/v1/projects/bulk-partial/alert-inbox/bulk-actions",
        json={
            "correlation_group_ids": [
                str(group_ids[0]),
                str(unknown_id),
                str(group_ids[1]),
            ],
            "action": "resolve",
        },
    )

    assert resp.status_code == 404
    # Does not name the offending id — matching ``bulk_update_events``, and so the
    # error cannot confirm whether that group exists in some other project.
    assert resp.json()["detail"] == "One or more alert correlation groups were not found"

    async with TestSessionLocal() as session:
        states = (
            (
                await session.execute(
                    select(AlertCorrelationState).where(
                        AlertCorrelationState.correlation_group_id.in_(group_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        # Not merely "not resolved" — not even CREATED. The valid ids never
        # reached the get-or-create, so the failed batch left no trace at all.
        assert states == []

    # The incidents are still open and still actionable afterwards.
    listing = await client.get("/api/v1/projects/bulk-partial/alert-inbox")
    assert listing.status_code == 200
    assert {item["status"] for item in listing.json()["items"]} == {"open"}


@pytest.mark.asyncio
async def test_bulk_action_refuses_false_positive_with_the_reason(client: AsyncClient) -> None:
    """``false_positive`` is the ONE action this route will not take, and it says why.

    Direction is part of the correlation key (worker/tasks/metrics/dispatch.py),
    so one scope's spike and one scope's drop are two separate incidents sitting
    side by side in the list — exactly what an operator sweeping a noisy scope
    would select together. ``_tune_false_positive_thresholds`` dedupes only
    within a single call and each step compounds off the scope's own current
    value, so marking both would take two ratchet steps on one scope for one
    human decision, permanently desensitising detection there with nothing in the
    record to say it was one click.

    A refusal rather than a silent dedupe: deduping would have to guess which of
    the two incidents the operator meant, and the ratchet is not undoable from
    the inbox. The message therefore has to carry the reason AND the way to get
    the job done, or the operator just retries the same click.
    """
    group_ids = await _seed_inbox_groups(client, "bulk-fp", "Bulk FP", count=2)

    resp = await client.post(
        "/api/v1/projects/bulk-fp/alert-inbox/bulk-actions",
        json={
            "correlation_group_ids": [str(group_id) for group_id in group_ids],
            "action": "false_positive",
        },
    )

    assert resp.status_code == 422
    message = " ".join(item["msg"] for item in resp.json()["detail"])
    assert "false_positive cannot be applied in bulk" in message
    # Names the REASON, not just the refusal.
    assert "direction" in message
    assert "correlation key" in message
    # And points at the action that still works.
    assert "one incident at a time" in message

    async with TestSessionLocal() as session:
        # Refused at the schema, so nothing downstream ran: no state rows, and
        # above all no scope override — the thing that cannot be undone here.
        assert (
            (
                await session.execute(
                    select(AlertCorrelationState).where(
                        AlertCorrelationState.correlation_group_id.in_(group_ids)
                    )
                )
            )
            .scalars()
            .all()
        ) == []
        assert (await session.execute(select(AnomalyScopeOverride))).scalars().all() == []

    # The single-incident route the message points at still accepts it.
    single = await client.post(
        f"/api/v1/projects/bulk-fp/alert-inbox/{group_ids[0]}/actions",
        json={"action": "false_positive"},
    )
    assert single.status_code == 200
    assert single.json()["group"]["status"] == "false_positive"


@pytest.mark.asyncio
async def test_bulk_action_caps_the_selection_and_rejects_an_empty_one(
    client: AsyncClient,
) -> None:
    """The id list is bounded, and this route is the first in the repo to bound one.

    The cap is pinned to ``list_alert_inbox``'s own page ceiling (``limit`` is
    ``le=200``), because the selection is made by ticking rows on ONE page: any
    lower and "select all" on a full page would 422, any higher and it would admit
    a list no page of the UI can produce. Unlike the other bulk
    routes, which mutate every named row in a single UPDATE, this one does
    per-group work — a state row, a rebuilt card and an audit row EACH — so the
    length of the list is a real cost here.

    Rejected before any database work, so an over-long list is cheap to refuse.
    """
    from tripl.schemas.alerting import MAX_BULK_INBOX_ACTION_GROUPS

    await _seed_inbox_groups(client, "bulk-cap", "Bulk Cap", count=1)

    too_many = await client.post(
        "/api/v1/projects/bulk-cap/alert-inbox/bulk-actions",
        json={
            "correlation_group_ids": [
                str(uuid.uuid4()) for _ in range(MAX_BULK_INBOX_ACTION_GROUPS + 1)
            ],
            "action": "acknowledge",
        },
    )
    assert too_many.status_code == 422
    assert any(item["type"] == "too_long" for item in too_many.json()["detail"])

    # An empty selection is refused too: it would otherwise be a 200 that changed
    # nothing, and a "0 incidents acknowledged" toast reads as a failure anyway.
    empty = await client.post(
        "/api/v1/projects/bulk-cap/alert-inbox/bulk-actions",
        json={"correlation_group_ids": [], "action": "acknowledge"},
    )
    assert empty.status_code == 422
    assert any(item["type"] == "too_short" for item in empty.json()["detail"])

    async with TestSessionLocal() as session:
        assert (await session.execute(select(AlertCorrelationState))).scalars().all() == []


@pytest.mark.asyncio
async def test_bulk_action_writes_one_audit_row_per_group_sharing_a_batch_id(
    client: AsyncClient,
) -> None:
    """The trail has to name WHICH incident was muted, so it is one row per group.

    ``audit_log.target_id`` holds a single UUID. One batch row with
    ``target_id=None`` would be an audit trail that records that somebody muted
    "some incidents" — worse than none, because it looks like coverage. So the
    route writes a row per group under the SAME ``alert_inbox.{action}`` name and
    the same ``alert_correlation_group`` target type the single route uses, which
    keeps a bulk mute searchable by exactly the query that finds a hand-clicked
    one, and a shared ``batch_id`` in the payload re-joins them into the one click
    that wrote them.
    """
    from tripl.models.audit_log import AuditLog

    group_ids = await _seed_inbox_groups(client, "bulk-audit", "Bulk Audit", count=3)

    resp = await client.post(
        "/api/v1/projects/bulk-audit/alert-inbox/bulk-actions",
        json={
            "correlation_group_ids": [str(group_id) for group_id in group_ids],
            "action": "mute",
            "note": "Vendor outage",
        },
    )
    assert resp.status_code == 200
    batch_id = resp.json()["batch_id"]

    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.target_type == "alert_correlation_group")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3
        # Same action name as the single route — not a separate "bulk_mute" verb
        # that an existing audit query would silently miss.
        assert {row.action for row in rows} == {"alert_inbox.mute"}
        # Each row names its OWN incident. ``target_id`` is the identity; the name
        # is the readable form of it (see the scope-name test below),
        # which for these fixtures is the same scope on every group.
        assert {row.target_id for row in rows} == set(group_ids)
        assert {row.target_name for row in rows} == {"event · event scope"}
        # And every row carries the batch id the response advertised, so the three
        # rows can be recognised as one decision after the fact.
        assert {row.payload["batch_id"] for row in rows} == {batch_id}
        assert {row.payload["batch_size"] for row in rows} == {3}
        assert {row.payload["action"] for row in rows} == {"mute"}
        # The id LIST is not repeated into every row: target_id already says which
        # incident this row is about.
        assert all("correlation_group_ids" not in row.payload for row in rows)


@pytest.mark.asyncio
async def test_inbox_audit_rows_name_the_incident_rather_than_its_uuid(
    client: AsyncClient,
) -> None:
    """An ``alert_inbox.*`` row has to say WHICH incident, in words.

    Both routes recorded ``str(correlation_group_id)`` as the target name, so the
    project Audit log — the page whose stated job is a compliance trail — was a
    wall of 8-character hex sitting next to rows that named their target
    ("scan_config.update  Old events (Android)"). Neither affordance on the row
    rescued it: the title attribute reveals the FULL UUID, and expanding the row
    shows only ``{action, note, muted_until}``.

    The name comes from the fields the incident card already renders — the newest
    item's ``scope_type`` and the group's ``scope_names`` — so the trail reads
    like the thing the operator clicked. ``target_id`` still carries the UUID, so
    nothing that looks an incident up by id changes.
    """
    from tripl.models.audit_log import AuditLog

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Inbox Named", "slug": "inbox-named", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    # Distinct scope names, so a row proves it named ITS OWN incident rather than
    # picking up whatever the fixture happens to share.
    named_scopes = {"checkout_started": uuid.uuid4(), "settings/choose_model": uuid.uuid4()}
    for index, (scope_name, group_id) in enumerate(named_scopes.items()):
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=now - timedelta(hours=index + 1),
            items=[
                {
                    **_inbox_item(
                        scope_type="event",
                        bucket=now - timedelta(hours=index + 1),
                        percent_delta=100.0,
                        correlation_group_id=group_id,
                    ),
                    "scope_name": scope_name,
                }
            ],
        )

    single_target = named_scopes["checkout_started"]
    single = await client.post(
        f"/api/v1/projects/inbox-named/alert-inbox/{single_target}/actions",
        json={"action": "mute", "note": "Vendor outage"},
    )
    assert single.status_code == 200

    bulk_target = named_scopes["settings/choose_model"]
    bulk = await client.post(
        "/api/v1/projects/inbox-named/alert-inbox/bulk-actions",
        json={"correlation_group_ids": [str(bulk_target)], "action": "acknowledge"},
    )
    assert bulk.status_code == 200

    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.target_type == "alert_correlation_group")
                )
            )
            .scalars()
            .all()
        )
    named = {row.target_id: row.target_name for row in rows}
    # Both routes, because the hex came from both of them.
    assert named == {
        single_target: "event · checkout_started",
        bulk_target: "event · settings/choose_model",
    }


@pytest.mark.asyncio
async def test_bulk_action_treats_a_repeated_id_as_one_incident(client: AsyncClient) -> None:
    """``[A, A, B]`` is TWO incidents — two cards and two audit rows, not three.

    ``dedupe_correlation_group_ids`` is the one definition of "what this batch
    acted on", and it is read twice: by the service, which mutates and then
    rebuilds a card per entry, and by the route, which writes an audit row per
    entry. Both readings go wrong on a repeat, and the audit one
    goes wrong silently — two rows under one ``batch_id``, each claiming its own
    decision on the same incident, which is a trail that reports two mutes where
    an operator made one. Nothing about the response would say so.

    A repeat is not hypothetical from the client: the inbox is an accumulating
    infinite list, a deep-linked incident is pinned ABOVE the pages it also
    appears in, and any caller assembling ids from both sources can send the same
    one twice.

    First-seen order is asserted, not merely the count: order is part of the
    response contract (the cards come back in request order so the client can
    pair them up without matching on id), so a dedupe through a ``set`` would
    pass a length check and still hand back the two cards in an arbitrary order.
    """
    from tripl.models.audit_log import AuditLog

    group_ids = await _seed_inbox_groups(client, "bulk-dupe", "Bulk Dupe", count=2)
    first, second = group_ids

    resp = await client.post(
        "/api/v1/projects/bulk-dupe/alert-inbox/bulk-actions",
        json={
            "correlation_group_ids": [str(first), str(first), str(second)],
            "action": "acknowledge",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    # Two cards, in the order the ids were first seen — the repeat is dropped
    # where it sits, not moved to the end and not rendered twice.
    assert [group["correlation_group_id"] for group in body["groups"]] == [
        str(first),
        str(second),
    ]

    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.target_type == "alert_correlation_group")
                )
            )
            .scalars()
            .all()
        )
        # Two rows, one per incident. Sorted rather than set-compared on purpose:
        # a set would hide the very failure this test exists for, which is the
        # SAME target_id appearing twice.
        assert sorted(str(row.target_id) for row in rows) == sorted([str(first), str(second)])
        # …and every row agrees the click was two incidents wide. A row reading
        # ``batch_size: 3`` beside two rows is a trail that contradicts itself.
        assert {row.payload["batch_size"] for row in rows} == {2}
        assert {row.payload["batch_id"] for row in rows} == {body["batch_id"]}

    # The incident named twice was acted on ONCE and looks like every other
    # acknowledged incident afterwards — the batch is a shortcut for N clicks,
    # and a duplicate must not become a second click.
    async with TestSessionLocal() as session:
        states = (
            (
                await session.execute(
                    select(AlertCorrelationState).where(
                        AlertCorrelationState.correlation_group_id == first
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(states) == 1
        assert states[0].status == "acknowledged"


@pytest.mark.asyncio
async def test_bulk_mute_without_an_expiry_is_indefinite_here_too(client: AsyncClient) -> None:
    """A null ``muted_until`` means "muted until I unmute" on this route as well.

    The single route already allows it: an operator watching a scope
    they know is broken should not have to invent an end date and get paged again
    the moment they guess too short. Bulk-muting a screenful of incidents is the
    case that needs it MOST, so this route must not become the one place that
    demands an expiry — and the null has to survive the whole round trip, since a
    ``muted`` row with a null column is the encoding every reader agrees on.
    """
    group_ids = await _seed_inbox_groups(client, "bulk-mute", "Bulk Mute", count=2)

    resp = await client.post(
        "/api/v1/projects/bulk-mute/alert-inbox/bulk-actions",
        json={
            "correlation_group_ids": [str(group_id) for group_id in group_ids],
            "action": "mute",
        },
    )

    assert resp.status_code == 200
    for group in resp.json()["groups"]:
        assert group["status"] == "muted"
        assert group["muted"] is True
        # NOT coerced into some default expiry on the way in.
        assert group["muted_until"] is None

    async with TestSessionLocal() as session:
        states = (
            (
                await session.execute(
                    select(AlertCorrelationState).where(
                        AlertCorrelationState.correlation_group_id.in_(group_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(states) == 2
        assert {state.status for state in states} == {"muted"}
        assert {state.muted_until for state in states} == {None}

    # Indefinitely muted rows sink out of the default view, so ``?status=muted``
    # is the way back to them — and they must not read as "open" to that filter.
    filtered = await client.get(
        "/api/v1/projects/bulk-mute/alert-inbox", params={"status": "muted"}
    )
    assert filtered.status_code == 200
    assert {item["correlation_group_id"] for item in filtered.json()["items"]} == {
        str(group_id) for group_id in group_ids
    }


async def _seed_slack_destination_with_rule(
    client: AsyncClient,
    slug: str,
    *,
    name: str = "Ops Slack",
    rule_name: str = "Volume rule",
) -> tuple[str, str]:
    """Create a real slack destination + rule THROUGH THE API.

    Through the API rather than by INSERT because the webhook has to be a value
    ``decrypt_value`` can read back — a hand-written ``webhook_url_encrypted``
    is not, and a test send would then fail for a reason no operator could hit.
    Returns ``(destination_id, rule_id)``.
    """
    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": name,
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]
    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={"name": rule_name, "enabled": True},
    )
    assert rule_resp.status_code == 201
    return destination_id, rule_resp.json()["id"]


@pytest.mark.asyncio
async def test_rule_carries_the_same_mute_state_as_its_monitor(client: AsyncClient) -> None:
    """The destination card and the monitors screen render the SAME AlertRule and
    used to disagree about its mute, because AlertRuleResponse had no mute state
    at all — so the card could neither show nor set one."""
    await client.post(
        "/api/v1/projects",
        json={"name": "Rule Mute", "slug": "rule-mute", "description": ""},
    )
    destination_id, rule_id = await _seed_slack_destination_with_rule(client, "rule-mute")

    async def read_rule() -> dict[str, object]:
        resp = await client.get("/api/v1/projects/rule-mute/alert-destinations")
        assert resp.status_code == 200
        return resp.json()[0]["rules"][0]

    rule = await read_rule()
    assert rule["muted"] is False
    assert rule["muted_until"] is None

    muted_until = datetime.now(UTC) + timedelta(hours=2)
    mute_resp = await client.post(
        f"/api/v1/projects/rule-mute/monitors/{rule_id}/mute",
        json={"muted_until": muted_until.isoformat()},
    )
    assert mute_resp.status_code == 200
    assert mute_resp.json()["muted"] is True

    rule = await read_rule()
    monitor = (await client.get(f"/api/v1/projects/rule-mute/monitors/{rule_id}")).json()
    assert rule["muted"] is True
    assert rule["muted"] == monitor["muted"]
    assert rule["muted_until"] == monitor["muted_until"]

    # A mute that has run out. It can only arrive by the passage of time — the
    # mute route refuses a past instant — so it is written straight to the row.
    async with TestSessionLocal() as session:
        db_rule = await session.get(AlertRule, uuid.UUID(rule_id))
        assert db_rule is not None
        db_rule.muted_until = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()

    rule = await read_rule()
    monitor = (await client.get(f"/api/v1/projects/rule-mute/monitors/{rule_id}")).json()
    assert rule["muted"] is False
    assert monitor["muted"] is False
    # The stored instant survives, so the card can still offer "unmute" on a rule
    # whose mute has lapsed; `muted` is the claim about NOW.
    assert rule["muted_until"] is not None


@pytest.mark.asyncio
async def test_rule_mute_still_requires_an_expiry(client: AsyncClient) -> None:
    """A null ``muted_until`` means the OPPOSITE thing on a rule, and must stay 422.

    tripl-a50u made "null = muted forever" true on ``AlertCorrelationState``. On
    ``AlertRule`` null means NOT MUTED — ``is_rule_muted`` answers False for it,
    and null is the default on every rule ever created — so relaxing
    ``MonitorMuteRequest.muted_until`` to match the inbox payload would report
    every monitor in the fleet as muted at once, with no test objecting.

    This is cheap insurance directly proportional to the blast radius of the
    semantic split: one column name, one domain, two opposite readings. The
    rule's permanent lever is ``enabled``, not a null expiry.
    """
    await client.post(
        "/api/v1/projects",
        json={"name": "Rule Mute Guard", "slug": "rule-mute-guard", "description": ""},
    )
    _destination_id, rule_id = await _seed_slack_destination_with_rule(client, "rule-mute-guard")

    # Neither spelling of "no expiry" is a rule mute.
    omitted = await client.post(
        f"/api/v1/projects/rule-mute-guard/monitors/{rule_id}/mute",
        json={},
    )
    assert omitted.status_code == 422
    explicit_null = await client.post(
        f"/api/v1/projects/rule-mute-guard/monitors/{rule_id}/mute",
        json={"muted_until": None},
    )
    assert explicit_null.status_code == 422

    # ...and the rule it failed to mute still carries the null it was born with,
    # which every payload must keep reading as NOT muted.
    async with TestSessionLocal() as session:
        db_rule = await session.get(AlertRule, uuid.UUID(rule_id))
        assert db_rule is not None
        assert db_rule.muted_until is None

    monitor = (await client.get(f"/api/v1/projects/rule-mute-guard/monitors/{rule_id}")).json()
    assert monitor["muted"] is False
    assert monitor["muted_until"] is None

    destinations = (await client.get("/api/v1/projects/rule-mute-guard/alert-destinations")).json()
    assert destinations[0]["rules"][0]["muted"] is False

    summary = (await client.get("/api/v1/projects/rule-mute-guard/monitors-summary")).json()
    summary_monitor = next(item for item in summary["monitors"] if item["rule_id"] == rule_id)
    assert summary_monitor["muted"] is False


@pytest.mark.asyncio
async def test_delete_confirm_can_state_what_it_would_destroy(client: AsyncClient) -> None:
    """Both AlertDelivery FKs are ondelete=CASCADE and the Inbox INNER JOINs
    through them, so deleting a rule or a destination silently takes the delivery
    history and the incidents with it. The confirm needs numbers."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Delete Impact", "slug": "delete-impact", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(
        project_id, rule_names=("Rule A", "Rule B")
    )
    rule_a, rule_b = rule_ids
    shared_group = uuid.uuid4()
    only_a_group = uuid.uuid4()
    now = datetime.now(UTC)

    async def seed(rule_id: uuid.UUID, created_at: datetime, groups: list[uuid.UUID]) -> uuid.UUID:
        return await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_id,
            created_at=created_at,
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=created_at,
                    percent_delta=50.0,
                    correlation_group_id=group_id,
                )
                for group_id in groups
            ],
        )

    await seed(rule_a, now - timedelta(hours=3), [shared_group, only_a_group])
    newest_a = await seed(rule_a, now - timedelta(hours=1), [shared_group])
    await seed(rule_b, now - timedelta(hours=2), [shared_group])
    async with TestSessionLocal() as session:
        delivery = await session.get(AlertDelivery, newest_a)
        assert delivery is not None
        delivery.status = AlertDeliveryStatus.failed.value
        await session.commit()

    listed = (await client.get("/api/v1/projects/delete-impact/alert-destinations")).json()
    assert len(listed) == 1
    destination = listed[0]
    rules = {rule["id"]: rule for rule in destination["rules"]}

    # ``total_deliveries`` on a RULE, not ``delivery_count``: the monitor detail
    # already publishes this same all-time count under that name, and
    # ``delivery_count`` means the deliveries of one INCIDENT on the inbox group.
    assert rules[str(rule_a)]["total_deliveries"] == 2
    assert "delivery_count" not in rules[str(rule_a)]
    assert rules[str(rule_a)]["incident_count"] == 2
    assert rules[str(rule_b)]["total_deliveries"] == 1
    assert rules[str(rule_b)]["incident_count"] == 1

    # The DESTINATION keeps ``delivery_count``: it is a destination-wide total
    # with no monitor counterpart to disagree with.
    assert destination["delivery_count"] == 3
    # The point of querying the destination total separately: `shared_group` is
    # carried by BOTH rules, so summing the per-rule DISTINCT counts would claim
    # three incidents where there are two.
    assert destination["incident_count"] == 2

    # Delivery health of the newest delivery, per rule, not per destination.
    assert rules[str(rule_a)]["last_delivery_status"] == "failed"
    assert rules[str(rule_b)]["last_delivery_status"] == "sent"
    assert rules[str(rule_a)]["last_delivery_at"] > rules[str(rule_b)]["last_delivery_at"]

    # A rule that has never delivered says so rather than omitting the fields.
    fresh_resp = await client.post(
        f"/api/v1/projects/delete-impact/alert-destinations/{destination_id}/rules",
        json={"name": "Rule C", "enabled": True},
    )
    assert fresh_resp.status_code == 201
    fresh = fresh_resp.json()
    assert fresh["total_deliveries"] == 0
    assert fresh["incident_count"] == 0
    assert fresh["last_delivery_at"] is None
    assert fresh["last_delivery_status"] is None


@pytest.mark.asyncio
async def test_destination_test_send_reaches_the_channel_and_logs_no_delivery(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'webhook set' says a value is STORED, not that it arrives."""
    from tripl.models.audit_log import AuditLog
    from tripl.worker.tasks import alerts

    posted: list[tuple[str, dict[str, object]]] = []

    def capture_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        posted.append((url, body))
        return None

    monkeypatch.setattr(alerts, "_post_json", capture_post_json)
    await client.post(
        "/api/v1/projects",
        json={"name": "Test Send", "slug": "test-send", "description": ""},
    )
    destination_id, _rule_id = await _seed_slack_destination_with_rule(client, "test-send")

    resp = await client.post(f"/api/v1/projects/test-send/alert-destinations/{destination_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["sent_at"] is not None

    assert len(posted) == 1
    url, payload = posted[0]
    assert "hooks.slack.com" in url
    text = payload["text"]
    assert isinstance(text, str)
    # Unmistakably a test to whoever reads the channel: nobody in that channel
    # asked for this message.
    assert "Tripl test message" in text
    assert "No alert fired" in text

    async with TestSessionLocal() as session:
        # NOT in the Delivery log: a test row would have to borrow a real rule and
        # a real scan (both FKs are NOT NULL) and claim they fired.
        deliveries = (await session.execute(select(AlertDelivery))).scalars().all()
        assert deliveries == []
        # The operator action is recorded where operator actions belong.
        actions = [
            row.action
            for row in (await session.execute(select(AuditLog))).scalars().all()
            if row.action == "alert_destination.test"
        ]
        assert actions == ["alert_destination.test"]


@pytest.mark.asyncio
async def test_destination_test_send_reports_a_channel_refusal_as_an_answer(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoked token is the thing the probe exists to find, so it comes back as
    ok=false with the channel's own words — not as a 500 reading "Tripl broke"."""
    from tripl.worker.tasks import alerts

    def refuse(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        raise ValueError("HTTP 401 from https://hooks.slack.com: invalid_token")

    monkeypatch.setattr(alerts, "_post_json", refuse)
    await client.post(
        "/api/v1/projects",
        json={"name": "Test Refused", "slug": "test-refused", "description": ""},
    )
    destination_id, _rule_id = await _seed_slack_destination_with_rule(client, "test-refused")

    resp = await client.post(
        f"/api/v1/projects/test-refused/alert-destinations/{destination_id}/test"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "401" in body["error"]
    assert "invalid_token" in body["error"]
    assert body["sent_at"] is None


@pytest.mark.asyncio
async def test_destination_test_send_on_a_demo_project_never_leaves_the_box(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A demo is zero-egress: its local sink tests ok with no
    network, and its disabled Slack example is refused rather than sent."""
    from tripl.worker.tasks import alerts

    def explode(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        raise AssertionError("a demo project must make no outbound request")

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Demo Test Send", "slug": "demo-test-send", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    # Created while the project is still real: a demo may not gain an external
    # destination through the API, which is exactly the row a demo ships.
    slack_id, _rule_id = await _seed_slack_destination_with_rule(client, "demo-test-send")
    async with TestSessionLocal() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        project.is_demo = True
        await session.commit()

    sink_resp = await client.post(
        "/api/v1/projects/demo-test-send/alert-destinations",
        json={"type": "demo_sink", "name": "Local sink", "enabled": True},
    )
    assert sink_resp.status_code == 201
    sink_id = sink_resp.json()["id"]

    monkeypatch.setattr(alerts, "_post_json", explode)

    sink_test = await client.post(
        f"/api/v1/projects/demo-test-send/alert-destinations/{sink_id}/test"
    )
    assert sink_test.status_code == 200
    assert sink_test.json()["ok"] is True
    assert sink_test.json()["sent_at"] is not None

    slack_test = await client.post(
        f"/api/v1/projects/demo-test-send/alert-destinations/{slack_id}/test"
    )
    assert slack_test.status_code == 200
    assert slack_test.json()["ok"] is False
    assert "Demo projects cannot send external alerts" in slack_test.json()["error"]


@pytest.mark.asyncio
async def test_open_incident_count_agrees_with_the_inbox_it_badges(client: AsyncClient) -> None:
    """The sidebar badged `alert_destination_count`, so it read "Alerting 1" while
    52 incidents sat open. A badge that disagrees with the page it
    labels is worse than none, so it is asserted equal to the page's own total."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Open Badge", "slug": "open-badge", "description": ""},
    )
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)

    async def seed_group(created_at: datetime) -> uuid.UUID:
        group_id = uuid.uuid4()
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=created_at,
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=created_at,
                    percent_delta=80.0,
                    correlation_group_id=group_id,
                )
            ],
        )
        return group_id

    untouched = await seed_group(now - timedelta(hours=1))
    resolved = await seed_group(now - timedelta(hours=2))
    lapsed_mute = await seed_group(now - timedelta(hours=3))
    live_mute = await seed_group(now - timedelta(hours=4))
    # Older than INBOX_LOOKBACK_DAYS: the page cannot see it, so neither may the
    # badge, however open it looks.
    await seed_group(now - timedelta(days=40))

    async with TestSessionLocal() as session:
        session.add_all(
            [
                AlertCorrelationState(
                    project_id=project_id,
                    correlation_group_id=resolved,
                    status="resolved",
                ),
                AlertCorrelationState(
                    project_id=project_id,
                    correlation_group_id=lapsed_mute,
                    status="muted",
                    muted_until=now - timedelta(hours=1),
                ),
                AlertCorrelationState(
                    project_id=project_id,
                    correlation_group_id=live_mute,
                    status="muted",
                    muted_until=now + timedelta(days=2),
                ),
            ]
        )
        await session.commit()

    inbox = (
        await client.get("/api/v1/projects/open-badge/alert-inbox", params={"status": "open"})
    ).json()
    summary = (await client.get("/api/v1/projects/open-badge")).json()["summary"]

    # `untouched` plus `lapsed_mute` — a mute that has run out is open again.
    assert inbox["total"] == 2
    assert {group["correlation_group_id"] for group in inbox["items"]} == {
        str(untouched),
        str(lapsed_mute),
    }
    assert summary["open_incident_count"] == inbox["total"]
    # ...and it is emphatically not the old destination count.
    assert summary["alert_destination_count"] == 1


async def _seed_simulate_fixture(
    client: AsyncClient,
    slug: str,
) -> dict[str, object]:
    """A project with one destination, one wide-open rule and three scopes.

    The three scopes deliberately disagree on every knob the replay can override,
    so one seeding is enough for all of them:

    ==========  ========  ========  =======  =======
    scope       expected    actual  percent  z-score
    ==========  ========  ========  =======  =======
    loud            20.0     200.0     900%     10.0
    quiet          100.0     150.0      50%      4.2
    thin             5.0      20.0     300%      8.0
    ==========  ========  ========  =======  =======

    No ``ProjectAnomalySettings`` row is seeded, so every replay driven from this
    fixture quotes ``DEFAULT_SIGMA_THRESHOLD`` as its saved sigma — the value
    that row is born holding. Sigma is a PROJECT setting and the replay reads it
    from there alone; the ``ScanConfig.sigma_threshold`` column
    is a per-scan copy nothing scores against and no API writes. That is why the
    ``second_scan_sigma`` knob this fixture used to carry is gone: a second scan
    disagreeing on that column could not move the quoted number, so it had
    nothing left to control. Telling the two columns apart needs values that are
    the default on neither side, and those cases live in test_batch4_services.py.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": f"Sim {slug}", "slug": slug},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Sim Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/sim",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "Sim Rule",
            "enabled": True,
            "include_project_total": True,
            "include_event_types": True,
            "include_events": True,
            "notify_on_spike": True,
            "notify_on_drop": True,
            # Wide open, so every gate below is the override's doing and not the
            # rule's.
            "min_percent_delta": 0,
            "min_absolute_delta": 0,
            "min_expected_count": 0,
            "cooldown_minutes": 60,
            "filters": [],
        },
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    now = datetime.now(UTC)
    scopes = {
        "loud": (20.0, 200.0, 10.0),
        "quiet": (100.0, 150.0, 4.2),
        "thin": (5.0, 20.0, 8.0),
    }
    scope_refs = {name: str(uuid.uuid4()) for name in scopes}
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name="ds",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(scan)
        await session.flush()
        from tripl.models.metric_anomaly import MetricAnomaly

        for name, (expected, actual, z_score) in scopes.items():
            session.add(
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan.id,
                    scope_type="event",
                    scope_ref=scope_refs[name],
                    event_id=None,
                    event_type_id=None,
                    bucket=now - timedelta(days=1),
                    actual_count=actual,
                    expected_count=expected,
                    stddev=1.0,
                    z_score=z_score,
                    direction="spike",
                )
            )
        scan_id = scan.id

    return {
        "project_id": project_id,
        "destination_id": destination_id,
        "rule_id": rule_id,
        "scan_id": scan_id,
        "scope_refs": scope_refs,
    }


def _simulate_url(fixture: dict[str, object], slug: str) -> str:
    return (
        f"/api/v1/projects/{slug}/alert-destinations/"
        f"{fixture['destination_id']}/rules/{fixture['rule_id']}/simulate"
    )


@pytest.mark.asyncio
async def test_simulate_tries_a_percent_threshold_without_saving_it_on_a_live_rule(
    client: AsyncClient,
) -> None:
    """tripl-oxkt.17 part 3: replay accepted only ``days`` and a cooldown, so
    asking "would min % 300 cut these" meant editing a rule that is live-routing
    to a real channel and waiting to see what production did."""
    slug = "sim-pct-override"
    fixture = await _seed_simulate_fixture(client, slug)
    url = _simulate_url(fixture, slug)

    baseline = (await client.post(f"{url}?days=7")).json()
    assert len(baseline["firings"]) == 3
    assert baseline["min_percent_delta_used"] == 0.0
    assert baseline["min_percent_delta_saved"] == 0.0

    # 300 drops `quiet` (50%) and keeps `thin` — which is exactly 300%, and the
    # live gate is ``< threshold``, so the boundary belongs to the alert.
    tightened = (await client.post(f"{url}?days=7&min_percent_delta_override=300")).json()
    assert {firing["scope_ref"] for firing in tightened["firings"]} == {
        fixture["scope_refs"]["loud"],  # type: ignore[index]
        fixture["scope_refs"]["thin"],  # type: ignore[index]
    }
    assert tightened["matched_before_cooldown"] == 2
    # The candidates are all still THERE — a rule threshold does not un-detect
    # anything, it only declines to deliver it.
    assert tightened["anomalies_considered"] == 3
    assert tightened["min_percent_delta_used"] == 300.0
    assert tightened["min_percent_delta_saved"] == 0.0

    # The whole point: nothing was written. The rule still routes exactly as it
    # did before the question was asked.
    rule = (
        await client.get(f"/api/v1/projects/{slug}/alert-destinations/{fixture['destination_id']}")
    ).json()["rules"][0]
    assert rule["min_percent_delta"] == 0.0
    assert len((await client.post(f"{url}?days=7")).json()["firings"]) == 3


@pytest.mark.asyncio
async def test_simulate_tries_a_min_expected_count_without_saving_it(client: AsyncClient) -> None:
    slug = "sim-count-override"
    fixture = await _seed_simulate_fixture(client, slug)
    url = _simulate_url(fixture, slug)

    tightened = (await client.post(f"{url}?days=7&min_expected_count_override=50")).json()
    # Only `quiet` expects 100; `loud` (20) and `thin` (5) are below the floor.
    assert [firing["scope_ref"] for firing in tightened["firings"]] == [
        fixture["scope_refs"]["quiet"]  # type: ignore[index]
    ]
    assert tightened["min_expected_count_used"] == 50.0
    assert tightened["min_expected_count_saved"] == 0.0
    assert tightened["anomalies_considered"] == 3

    rule = (
        await client.get(f"/api/v1/projects/{slug}/alert-destinations/{fixture['destination_id']}")
    ).json()["rules"][0]
    assert rule["min_expected_count"] == 0.0


@pytest.mark.asyncio
async def test_simulate_sigma_override_re_reads_what_the_detector_recorded(
    client: AsyncClient,
) -> None:
    """A sigma what-if is about DETECTION, so it removes candidates outright.

    The rule thresholds decline to deliver a signal that exists; a stricter sigma
    says the signal was never recorded, which has to show in
    ``anomalies_considered`` as well or the two numbers would describe different
    worlds.
    """
    slug = "sim-sigma-override"
    fixture = await _seed_simulate_fixture(client, slug)
    url = _simulate_url(fixture, slug)

    baseline = (await client.post(f"{url}?days=7")).json()
    assert baseline["anomalies_considered"] == 3
    # No override: `used` mirrors the SAVED threshold, and saved is the
    # PROJECT's Detection setting, never the scan's column.
    # This fixture seeds no ``ProjectAnomalySettings`` row, so both numbers are
    # ``DEFAULT_SIGMA_THRESHOLD`` — the value that row would be born holding —
    # and this test cannot tell the two sources apart, because the scan column's
    # own default happens to be the same number. The ones that can are in
    # test_batch4_services.py, which seeds a project sigma and a scan column that
    # agree with neither each other nor the default.
    assert baseline["sigma_threshold_saved"] == DEFAULT_SIGMA_THRESHOLD
    assert baseline["sigma_threshold_used"] == DEFAULT_SIGMA_THRESHOLD

    strict = (await client.post(f"{url}?days=7&sigma_threshold_override=5")).json()
    # `quiet` was scored at z=4.2 and would not have been written at all.
    assert strict["anomalies_considered"] == 2
    assert strict["matched_before_cooldown"] == 2
    assert {firing["scope_ref"] for firing in strict["firings"]} == {
        fixture["scope_refs"]["loud"],  # type: ignore[index]
        fixture["scope_refs"]["thin"],  # type: ignore[index]
    }
    assert strict["sigma_threshold_used"] == 5.0
    assert strict["sigma_threshold_saved"] == 4.0

    # Exactly at a candidate's z-score still keeps it: the detector's own test is
    # ``|z| >= threshold``, and the replay must not disagree with it.
    boundary = (await client.post(f"{url}?days=7&sigma_threshold_override=8")).json()
    assert {firing["scope_ref"] for firing in boundary["firings"]} == {
        fixture["scope_refs"]["loud"],  # type: ignore[index]
        fixture["scope_refs"]["thin"],  # type: ignore[index]
    }

    # Lowering it cannot conjure anything: the rows below the scan's threshold
    # were never written, so there is nothing on disk to bring back.
    loosened = (await client.post(f"{url}?days=7&sigma_threshold_override=1")).json()
    assert loosened["anomalies_considered"] == 3
    assert loosened["sigma_threshold_used"] == 1.0


# ``test_simulate_reports_no_saved_sigma_when_the_scans_disagree`` stood here and
# is deliberately gone rather than repaired. Its subject was the
# premise, not an assertion: "``sigma_threshold`` is a SCAN setting, and a
# project-wide rule reads many", from which it followed that a project whose
# scans disagree has no saved number to quote and the replay answers null. That
# is the defect. Sigma is a PROJECT setting — ``ProjectAnomalySettings``, the
# only row ``detect._build_anomaly_settings`` reads — so the replay now always
# has exactly one number, whatever the per-scan columns hold, and the two
# ``is None`` assertions read ``DEFAULT_SIGMA_THRESHOLD``. Its other half
# (bind the rule to one scan, override still wins) survived only because the two
# columns' defaults are both 4.0, so it asserted the right value for the wrong
# reason and could not have caught this.
#
# Rewriting it here would have reproduced test_batch4_services.py's
# ``test_the_replay_has_one_sigma_to_quote_whatever_the_scans_hold``, which seeds
# the same two disagreeing scans plus a project sigma that is neither, and checks
# a wide and a bound rule in one pass — the assertion this test could not make.
# The default-when-no-settings-row arm it also covered is asserted above.


@pytest.mark.asyncio
async def test_simulate_rejects_overrides_that_cannot_mean_anything(client: AsyncClient) -> None:
    slug = "sim-bad-override"
    fixture = await _seed_simulate_fixture(client, slug)
    url = _simulate_url(fixture, slug)

    for query in (
        "min_percent_delta_override=-1",
        "min_expected_count_override=-1",
        # Zero sigma is not "no filter": the detector divides by the spread, so
        # nothing is ever scored against it.
        "sigma_threshold_override=0",
        # Above the ceiling the false-positive ratchet itself respects.
        "sigma_threshold_override=10.5",
    ):
        resp = await client.post(f"{url}?days=7&{query}")
        assert resp.status_code == 422, query


@pytest.mark.asyncio
async def test_rule_and_monitor_report_one_delivery_total_under_one_name(
    client: AsyncClient,
) -> None:
    """A monitor IS an alert rule, so its all-time delivery count must not have
    two names — and ``delivery_count`` was already taken by the inbox group,
    where it means the deliveries of ONE incident."""
    from tripl.services._alerting_health import load_destination_health

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "One Name", "slug": "one-name"},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    destination_resp = await client.post(
        "/api/v1/projects/one-name/alert-destinations",
        json={
            "type": "slack",
            "name": "Ops",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/one",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/one-name/alert-destinations/{destination_id}/rules",
        json={"name": "Rule", "enabled": True, "filters": []},
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    now = datetime.now(UTC)
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name="ds",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(scan)
        await session.flush()
        for index, status in enumerate((AlertDeliveryStatus.sent, AlertDeliveryStatus.failed)):
            session.add(
                AlertDelivery(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    scan_config_id=scan.id,
                    destination_id=uuid.UUID(destination_id),
                    rule_id=uuid.UUID(rule_id),
                    status=status.value,
                    channel="slack",
                    matched_count=1,
                    created_at=now - timedelta(hours=2 - index),
                )
            )

    listed = (await client.get("/api/v1/projects/one-name/alert-destinations")).json()
    rule = listed[0]["rules"][0]
    monitor = (await client.get(f"/api/v1/projects/one-name/monitors/{rule_id}")).json()

    assert rule["total_deliveries"] == 2
    assert monitor["total_deliveries"] == rule["total_deliveries"]
    assert "delivery_count" not in rule
    # ...and the health field the frontend string-matches agrees across both.
    assert rule["last_delivery_status"] == "failed"
    assert monitor["last_delivery_status"] == rule["last_delivery_status"]

    # The loader itself hands back the ENUM, so the guarantee does not rest on
    # Pydantic coercing a bare string at the response boundary.
    async with TestSessionLocal() as session:
        health = await load_destination_health(session, [uuid.UUID(destination_id)])
    status_value = health[uuid.UUID(destination_id)].rules[uuid.UUID(rule_id)].last_delivery_status
    assert isinstance(status_value, AlertDeliveryStatus)
    assert status_value is AlertDeliveryStatus.failed


def test_monitor_and_test_send_contracts_declare_every_field_they_always_send() -> None:
    """A default on a response model is not a behaviour, it is a claim to the
    generated client that the key may be missing.

    ``muted``/``muted_until``/``last_delivery_at``/``last_delivery_status``
    described the same AlertRule on both payload families and disagreed about
    whether they were optional, so one object had two TypeScript shapes.
    """
    from tripl.main import app

    schemas = app.openapi()["components"]["schemas"]

    shared_rule_fields = {"muted", "muted_until", "last_delivery_at", "last_delivery_status"}
    assert shared_rule_fields <= set(schemas["AlertRuleResponse"]["required"])
    assert shared_rule_fields <= set(schemas["MonitorDetailResponse"]["required"])
    assert {"muted", "muted_until"} <= set(schemas["MonitorSummaryItem"]["required"])
    # The two monitor timestamps have no rule counterpart but are sent just as
    # unconditionally, by the same two builders.
    assert {"last_anomaly_at", "last_notified_at"} <= set(schemas["MonitorSummaryItem"]["required"])
    # Same doctrine for the scan binding: nullable, always sent. A default would
    # let the generated client treat "every scan in the project" and "the server
    # did not say" as one value — and ``AlertRuleResponse`` already declares
    # ``scan_config_id`` required-but-nullable for the very same column, so a
    # default here would give one AlertRule two shapes again.
    assert "scan_config_id" in set(schemas["AlertRuleResponse"]["required"])
    assert {"scan_config_id", "scan_name"} <= set(schemas["MonitorDetailResponse"]["required"])
    # The test-send reply serializes both on every response, including the
    # ``None`` half of each pair — exactly the mismatch ``event_id`` warns about.
    assert {"ok", "error", "sent_at"} <= set(schemas["AlertDestinationTestResponse"]["required"])


@pytest.mark.asyncio
async def test_destination_card_reads_every_mute_against_the_clock_it_was_given(
    client: AsyncClient,
) -> None:
    """``destination_to_response`` used to read ``datetime.now`` itself, so the
    list built one clock PER DESTINATION while its comment promised one for the
    whole response."""
    from tripl.services._alerting_destinations import destination_to_response, get_destination
    from tripl.services._alerting_health import DestinationHealth

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "One Clock", "slug": "one-clock"},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    destination_resp = await client.post(
        "/api/v1/projects/one-clock/alert-destinations",
        json={
            "type": "slack",
            "name": "Ops",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/clock",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = uuid.UUID(destination_resp.json()["id"])

    rule_resp = await client.post(
        f"/api/v1/projects/one-clock/alert-destinations/{destination_id}/rules",
        json={"name": "Rule", "enabled": True, "filters": []},
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    muted_until = datetime.now(UTC) + timedelta(days=1)
    mute_resp = await client.post(
        f"/api/v1/projects/one-clock/monitors/{rule_id}/mute",
        json={"muted_until": muted_until.isoformat()},
    )
    assert mute_resp.status_code == 200

    async with TestSessionLocal() as session:
        destination = await get_destination(
            session,
            project_id=project_id,
            destination_id=destination_id,
        )
        during = destination_to_response(
            destination,
            DestinationHealth(),
            now=muted_until - timedelta(hours=1),
        )
        after = destination_to_response(
            destination,
            DestinationHealth(),
            now=muted_until + timedelta(hours=1),
        )

    # The second call asks about an instant a day in the future. A function
    # reading its own clock would answer "still muted" for both.
    assert during.rules[0].muted is True
    assert after.rules[0].muted is False
    assert after.rules[0].muted_until is not None


@pytest.mark.asyncio
async def test_naming_a_destination_for_the_audit_log_costs_no_rollup_queries(
    client: AsyncClient,
) -> None:
    """Delete and test-send both had to name the destination in the audit entry,
    and both did it by calling ``get_destination`` — which is
    ``get_destination_response``, i.e. the four delete-impact aggregates of
    ``load_destination_health`` plus a second load of the row."""
    from tripl.models.audit_log import AuditLog

    # Shared with test_project_lookup_perf rather than copied: there is one right
    # way to count what the test engine executed.
    from tripl.tests.test_project_lookup_perf import captured_sql
    from tripl.worker.tasks import alerts

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Cheap Name", "slug": "cheap-name"},
    )
    assert project_resp.status_code == 201

    async def _make_destination(name: str) -> str:
        resp = await client.post(
            "/api/v1/projects/cheap-name/alert-destinations",
            json={
                "type": "slack",
                "name": name,
                "enabled": True,
                "webhook_url": "https://hooks.slack.com/services/T1/B1/cheap",
            },
        )
        assert resp.status_code == 201
        return str(resp.json()["id"])

    def _aggregate_statements(statements: list[str]) -> list[str]:
        return [statement for statement in statements if "count(" in statement.lower()]

    sent: list[str] = []

    def _fake_slack(webhook_url: str, message: str, **_kwargs: object) -> None:
        sent.append(message)

    monkeypatched = alerts._send_slack_message
    alerts._send_slack_message = _fake_slack  # type: ignore[assignment]
    try:
        test_target = await _make_destination("Test Me")
        with captured_sql() as statements:
            test_resp = await client.post(
                f"/api/v1/projects/cheap-name/alert-destinations/{test_target}/test"
            )
        assert test_resp.status_code == 200
        assert test_resp.json()["ok"] is True
        assert sent
        assert _aggregate_statements(statements) == []
    finally:
        alerts._send_slack_message = monkeypatched  # type: ignore[assignment]

    delete_target = await _make_destination("Delete Me")
    with captured_sql() as statements:
        delete_resp = await client.delete(
            f"/api/v1/projects/cheap-name/alert-destinations/{delete_target}"
        )
    assert delete_resp.status_code == 204
    assert _aggregate_statements(statements) == []

    # Both entries still NAME the destination the operator acted on, which is the
    # only reason the extra load existed.
    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog.action, AuditLog.target_name).where(
                        AuditLog.target_type == "alert_destination"
                    )
                )
            )
            .tuples()
            .all()
        )
    named = {action: target_name for action, target_name in rows}
    assert named["alert_destination.test"] == "Test Me"
    assert named["alert_destination.delete"] == "Delete Me"


# --- the inbox narrows by more than status --------------------
#
# An analyst working through "the events I already know are fine" had exactly
# one control on a list of 180 incidents. Every filter below is applied to the
# BUILT groups, beside `status`, so the page's own `total` counts what was
# asked for.


async def _seed_filterable_inbox(client: AsyncClient) -> dict[str, uuid.UUID]:
    """Three incidents that differ in every axis the filters read."""
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Inbox Filters", "slug": "inbox-filters", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    groups = {"fresh_spike": uuid.uuid4(), "old_drop": uuid.uuid4(), "release": uuid.uuid4()}
    plan = [
        (groups["fresh_spike"], now - timedelta(hours=1), "event", 120.0, "Checkout completed"),
        (groups["old_drop"], now - timedelta(days=5), "event", -60.0, "Signup started"),
        (
            groups["release"],
            now - timedelta(hours=2),
            "release_regression",
            -40.0,
            "Acme 7.4 rollout",
        ),
    ]
    for group_id, created_at, scope_type, delta, scope_name in plan:
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=created_at,
            items=[
                _inbox_item(
                    scope_type=scope_type,
                    bucket=created_at,
                    percent_delta=delta,
                    correlation_group_id=group_id,
                    scope_name=scope_name,
                )
            ],
        )
    return groups


class _ExplodingRows(list):
    """A row list that refuses to be read.

    The only way to assert that the scope text was NOT built: the work is
    invisible from the outside, and a timing assertion on an SBC proves nothing.
    """

    def __iter__(self):  # type: ignore[override]
        raise AssertionError("the scope haystack was built for a filter that does not search")


def test_inbox_filters_build_the_scope_text_only_when_something_searches() -> None:
    """A keyword argument is evaluated before the call.

    Passing ``scope_haystack=_scope_haystack(rows)`` therefore built that text
    for every group of every request — a set, a sort, a join and a casefold over
    every delivery item — including the overwhelmingly common request that
    searches for nothing (Copilot, PR #162). ``matches`` takes the rows instead
    and reads them last, after every cheap predicate has had its say.
    """
    group = SimpleNamespace(
        latest_delivery_at=datetime.now(UTC),
        scope_types=["event"],
        direction="drop",
    )

    # Filtering by a date, a kind and a direction touches no row.
    for filters in (
        InboxFilters(last_fired_from=datetime.now(UTC) - timedelta(days=1)),
        InboxFilters(scope_type=MetricScopeType.event),
        InboxFilters(direction=AnomalyDirection.drop),
        # A cleared search box posts whitespace, which is not a search.
        InboxFilters(scope="   "),
    ):
        assert filters.matches(group, rows=_ExplodingRows()) is True

    # …and a filter that no longer needs to look does not look either: the
    # direction says no before the scope text would have been built.
    assert (
        InboxFilters(direction=AnomalyDirection.spike, scope="anything").matches(
            group, rows=_ExplodingRows()
        )
        is False
    )

    # `is_active` is what keeps the loop itself off an unfiltered request.
    assert InboxFilters().is_active is False
    assert InboxFilters(scope="  ").is_active is False
    assert InboxFilters(direction=AnomalyDirection.drop).is_active is True


@pytest.mark.asyncio
async def test_inbox_filters_by_when_the_incident_last_fired(client: AsyncClient) -> None:
    """The filter the reporter asked for first, on the column the card leads
    with: `latest_delivery_at`, not when the incident started."""
    groups = await _seed_filterable_inbox(client)
    base = "/api/v1/projects/inbox-filters/alert-inbox"
    now = datetime.now(UTC)

    everything = await client.get(base)
    assert everything.status_code == 200
    assert everything.json()["total"] == 3

    recent = await client.get(
        base, params={"last_fired_from": (now - timedelta(days=1)).isoformat()}
    )
    assert recent.status_code == 200
    assert {item["correlation_group_id"] for item in recent.json()["items"]} == {
        str(groups["fresh_spike"]),
        str(groups["release"]),
    }
    # `total` counts what was asked for, the way it does for `status` — a filter
    # that narrows the page but not the count is how "3 incidents" ends up above
    # two cards.
    assert recent.json()["total"] == 2

    stale = await client.get(base, params={"last_fired_to": (now - timedelta(days=1)).isoformat()})
    assert stale.status_code == 200
    assert [item["correlation_group_id"] for item in stale.json()["items"]] == [
        str(groups["old_drop"])
    ]

    window = await client.get(
        base,
        params={
            "last_fired_from": (now - timedelta(hours=3)).isoformat(),
            "last_fired_to": (now - timedelta(minutes=90)).isoformat(),
        },
    )
    assert window.status_code == 200
    assert [item["correlation_group_id"] for item in window.json()["items"]] == [
        str(groups["release"])
    ]

    # A bare date carries no zone. The stored column is naive under SQLite and
    # aware under Postgres, so an un-normalized comparison is a TypeError on one
    # engine and correct on the other — the worst shape a comparison can have.
    naive = await client.get(
        base, params={"last_fired_from": (now - timedelta(days=1)).replace(tzinfo=None).isoformat()}
    )
    assert naive.status_code == 200
    assert naive.json()["total"] == 2


@pytest.mark.asyncio
async def test_inbox_filters_by_scope_type_and_direction(client: AsyncClient) -> None:
    groups = await _seed_filterable_inbox(client)
    base = "/api/v1/projects/inbox-filters/alert-inbox"

    releases = await client.get(base, params={"scope_type": "release_regression"})
    assert releases.status_code == 200
    assert [item["correlation_group_id"] for item in releases.json()["items"]] == [
        str(groups["release"])
    ]

    drops = await client.get(base, params={"direction": "drop"})
    assert drops.status_code == 200
    assert {item["correlation_group_id"] for item in drops.json()["items"]} == {
        str(groups["old_drop"]),
        str(groups["release"]),
    }

    combined = await client.get(base, params={"scope_type": "event", "direction": "drop"})
    assert combined.status_code == 200
    assert [item["correlation_group_id"] for item in combined.json()["items"]] == [
        str(groups["old_drop"])
    ]

    # Enums, for the reason `status` is one: a typo has to be a 422
    # rather than an empty inbox nobody can explain.
    for params in ({"scope_type": "BOGUS"}, {"direction": "BOGUS"}):
        rejected = await client.get(base, params=params)
        assert rejected.status_code == 422, f"{params} was accepted: {rejected.text}"


@pytest.mark.asyncio
async def test_inbox_scope_search_reaches_past_the_eight_name_display_cap(
    client: AsyncClient,
) -> None:
    """The search must read the incident's ROWS, not its response.

    `_build_inbox_group_response` truncates `scope_names` to eight entries, so a
    search over the payload silently cannot find the ninth scope of a wide
    incident — and a wide incident is exactly the one an operator needs to search
    rather than read.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Inbox Search", "slug": "inbox-search", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    wide_group = uuid.uuid4()
    # Sorted, "zz_quiet_event" lands ninth and is dropped by the [:8] slice.
    names = [f"a{index}_noisy_event" for index in range(8)] + ["zz_quiet_event"]
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=120.0,
                correlation_group_id=wide_group,
                scope_name=name,
            )
            for name in names
        ],
    )

    base = "/api/v1/projects/inbox-search/alert-inbox"
    listed = await client.get(base)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["scope_names"] == sorted(names)[:8]
    assert "zz_quiet_event" not in listed.json()["items"][0]["scope_names"]

    found = await client.get(base, params={"scope": "QUIET"})
    assert found.status_code == 200
    assert [item["correlation_group_id"] for item in found.json()["items"]] == [str(wide_group)]

    missing = await client.get(base, params={"scope": "nothing_is_called_this"})
    assert missing.status_code == 200
    assert missing.json()["total"] == 0

    # Whitespace is not a filter. A cleared search box posts one, and treating it
    # as a needle would empty the inbox.
    blank = await client.get(base, params={"scope": "   "})
    assert blank.status_code == 200
    assert blank.json()["total"] == 1

    # Nor is a NUL. `scope` is a FreeTextFilter like every other free-text filter
    # in the API, so U+0000 is stripped rather than searched for — the two
    # candidate answers are "match nothing" and "the filter the user plainly
    # meant", and schemas/text_filters.py argues for the second. Answering
    # differently here from the eight filters beside it would be inconsistent
    # rather than stricter. The repo-wide pin in test_text_filters.py is what
    # forces the decision to be made at all; this is what the decision WAS.
    laced = await client.get(base, params={"scope": "\x00"})
    assert laced.status_code == 200
    assert laced.json()["total"] == 1

    partial = await client.get(base, params={"scope": "qui\x00et"})
    assert partial.status_code == 200
    assert [item["correlation_group_id"] for item in partial.json()["items"]] == [str(wide_group)]
