"""Seed data shared by the incident summary tests (F14, #267).

One project with an event, a scan, a Slack rule and one inbox incident on the
event scope. Everything else (attribution, verdicts, comments, extra
deliveries) is added per test through the helpers below.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.user import User
from tripl.services import llm_service
from tripl.tests.conftest import TestSessionLocal

# Recent and hour-aligned.
BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
SENSITIVE_VALUE = "alice@example.com"
SAMPLE_VALUE = "sample-secret-4242"


@dataclass(frozen=True)
class SeededIncident:
    slug: str
    project_id: uuid.UUID
    event_type_id: uuid.UUID
    event_id: uuid.UUID
    scan_config_id: uuid.UUID
    destination_id: uuid.UUID
    rule_id: uuid.UUID
    group_id: uuid.UUID

    @property
    def summary_url(self) -> str:
        return f"/api/v1/projects/{self.slug}/alert-inbox/{self.group_id}/summary"


async def seed_incident(
    client: AsyncClient, slug: str, *, sample_value: str | None = None
) -> SeededIncident:
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
        json={"data_source_id": data_source.json()["id"], "name": "Scan", "base_query": "SELECT 1"},
    )
    assert scan.status_code == 201, scan.text
    destination = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Slack",
            "enabled": True,
            "webhook_url": f"https://hooks.slack.com/services/T1/B1/{uuid.uuid4().hex[:8]}",
        },
    )
    assert destination.status_code == 201, destination.text
    rule = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination.json()['id']}/rules",
        json={"name": "Slack rule", "enabled": True, "filters": []},
    )
    assert rule.status_code == 201, rule.text
    project = await client.get(f"/api/v1/projects/{slug}")
    seeded = SeededIncident(
        slug=slug,
        project_id=uuid.UUID(project.json()["id"]),
        event_type_id=uuid.UUID(event_type.json()["id"]),
        event_id=uuid.UUID(event.json()["id"]),
        scan_config_id=uuid.UUID(scan.json()["id"]),
        destination_id=uuid.UUID(destination.json()["id"]),
        rule_id=uuid.UUID(rule.json()["id"]),
        group_id=uuid.uuid4(),
    )
    await add_item(seeded, bucket=BUCKET, sample_value=sample_value)
    return seeded


async def add_item(
    seeded: SeededIncident,
    *,
    bucket: datetime,
    group_id: uuid.UUID | None = None,
    sample_value: str | None = None,
    actual: float = 30,
    expected: float = 120,
) -> None:
    """One delivery with one event-scope item in ``group_id`` (default: the incident)."""
    async with TestSessionLocal() as session:
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=seeded.project_id,
            scan_config_id=seeded.scan_config_id,
            destination_id=seeded.destination_id,
            rule_id=seeded.rule_id,
            status="sent",
            channel="slack",
            matched_count=1,
            created_at=bucket,
        )
        session.add(delivery)
        await session.flush()
        session.add(
            AlertDeliveryItem(
                delivery_id=delivery.id,
                scope_type="event",
                scope_ref=str(seeded.event_id),
                scope_name="Landing Viewed",
                event_type_id=None,
                event_id=seeded.event_id,
                bucket=bucket,
                direction="drop",
                actual_count=actual,
                expected_count=expected,
                absolute_delta=expected - actual,
                percent_delta=75.0,
                sample_value=sample_value,
                correlation_group_id=group_id or seeded.group_id,
            )
        )
        await session.commit()


async def add_sensitive_attribution(seeded: SeededIncident, *, bucket: datetime = BUCKET) -> None:
    """A sensitive ``email`` field and an attribution that splits by it."""
    async with TestSessionLocal() as session:
        session.add(
            FieldDefinition(
                event_type_id=seeded.event_type_id,
                name="email",
                display_name="Email",
                field_type="string",
                sensitivity="pii",
            )
        )
        anomaly = MetricAnomaly(
            scan_config_id=seeded.scan_config_id,
            scope_type="event",
            scope_ref=str(seeded.event_id),
            event_id=seeded.event_id,
            event_type_id=None,
            bucket=bucket,
            actual_count=30,
            expected_count=120,
            stddev=10,
            z_score=-9,
            direction="drop",
            created_at=bucket,
        )
        session.add(anomaly)
        await session.flush()
        session.add(
            MetricAnomalyAttribution(
                anomaly_id=anomaly.id,
                delta=-90.0,
                columns=[
                    {
                        "column": "properties.user_email",
                        "explained_share": 0.9,
                        "gross_movement": 90.0,
                        "values": [
                            {
                                "value": SENSITIVE_VALUE,
                                "delta": -81.0,
                                "expected": 100.0,
                                "actual": 19.0,
                                "share": 0.9,
                            }
                        ],
                    }
                ],
                release=None,
            )
        )
        await session.commit()


async def add_comment(seeded: SeededIncident, body: str) -> None:
    async with TestSessionLocal() as session:
        session.add(EventPhotoComment(event_id=seeded.event_id, body=body))
        await session.commit()


async def registered_user() -> User:
    async with TestSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        return user


def enable_ai(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(llm_service, "is_enabled", lambda config=None: enabled)


class FakeLlm:
    """Records every call; answers with ``reply`` (None = provider failure)."""

    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def __call__(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str | None:
        self.calls.append({"system": system_prompt, "user": user_prompt, **kwargs})
        return self.reply

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
        monkeypatch.setattr(llm_service, "complete", self)
        return self


GOOD_REPLY = (
    '{"sentences": ['
    '{"text": "Landing Viewed dropped 75% below expected [1].", '
    '"role": "what_broke", "facts": [1]}'
    "]}"
)
