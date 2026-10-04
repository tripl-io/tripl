"""Regression coverage for schema drift acceptance, reset, and snooze lapse."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.field_definition import FieldDefinition
from tripl.models.schema_drift import SchemaDrift
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.tests.conftest import TestSessionLocal


async def _project_and_type(client: AsyncClient, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    project = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project.status_code == 201, project.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    assert event_type.status_code == 201, event_type.text
    return uuid.UUID(project.json()["id"]), uuid.UUID(event_type.json()["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed", "expected"),
    [("RECORD", "json"), ("STRUCT<a INT64>", "json"), ("Int64", "string")],
)
async def test_accept_new_field_uses_detector_type(
    client: AsyncClient, observed: str, expected: str
) -> None:
    slug = f"drift-new-{expected}-{uuid.uuid4().hex[:6]}"
    _, event_type_id = await _project_and_type(client, slug)
    async with TestSessionLocal() as session:
        drift = SchemaDrift(
            event_type_id=event_type_id,
            field_name="payload",
            drift_type="new_field",
            observed_type=observed,
        )
        session.add(drift)
        await session.commit()
        drift_id = drift.id
    response = await client.post(
        f"/api/v1/projects/{slug}/event-types/drifts/{drift_id}/actions",
        json={"action": "accept"},
    )
    assert response.status_code == 200, response.text
    async with TestSessionLocal() as session:
        field = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == event_type_id,
                FieldDefinition.name == "payload",
            )
        )
        assert field is not None
        assert field.field_type == expected


@pytest.mark.asyncio
async def test_accept_changed_type_writes_detector_type(client: AsyncClient) -> None:
    slug = "drift-type-record"
    _, event_type_id = await _project_and_type(client, slug)
    async with TestSessionLocal() as session:
        session.add(
            FieldDefinition(
                event_type_id=event_type_id,
                name="payload",
                display_name="Payload",
                field_type="string",
                order=1,
            )
        )
        drift = SchemaDrift(
            event_type_id=event_type_id,
            field_name="payload",
            drift_type="type_changed",
            observed_type="RECORD",
            declared_type="string",
        )
        session.add(drift)
        await session.commit()
        drift_id = drift.id
    response = await client.post(
        f"/api/v1/projects/{slug}/event-types/drifts/{drift_id}/actions",
        json={"action": "accept"},
    )
    assert response.status_code == 200, response.text
    async with TestSessionLocal() as session:
        field = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == event_type_id,
                FieldDefinition.name == "payload",
            )
        )
        assert field is not None
        assert field.field_type == "json"


@pytest.mark.asyncio
async def test_reset_includes_orphaned_schema_drift_but_not_other_project(
    client: AsyncClient,
) -> None:
    _, owned_type_id = await _project_and_type(client, "drift-reset-owned")
    _, other_type_id = await _project_and_type(client, "drift-reset-other")
    async with TestSessionLocal() as session:
        owned = SchemaDrift(
            event_type_id=owned_type_id,
            scan_config_id=None,
            field_name="orphaned",
            drift_type="new_field",
        )
        other = SchemaDrift(
            event_type_id=other_type_id,
            scan_config_id=None,
            field_name="other",
            drift_type="new_field",
        )
        session.add_all([owned, other])
        await session.commit()
        owned_id, other_id = owned.id, other.id
    response = await client.post("/api/v1/projects/drift-reset-owned/danger/reset-drifts", json={})
    assert response.status_code == 200, response.text
    assert response.json()["schema_drifts"] == 1
    async with TestSessionLocal() as session:
        assert await session.get(SchemaDrift, owned_id) is None
        assert await session.get(SchemaDrift, other_id) is not None


@pytest.mark.asyncio
async def test_expired_snoozes_rejoin_schema_and_variable_open_counts(
    client: AsyncClient,
) -> None:
    slug = "drift-snooze-lapse"
    project_id, event_type_id = await _project_and_type(client, slug)
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": str(event_type_id), "name": "Onboarding"},
    )
    assert event.status_code == 201, event.text
    second_event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": str(event_type_id), "name": "Checkout"},
    )
    assert second_event.status_code == 201, second_event.text
    variable = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "variant", "allowed_values": ["a"]},
    )
    assert variable.status_code == 201, variable.text
    event_id = uuid.UUID(event.json()["id"])
    variable_id = uuid.UUID(variable.json()["id"])
    expired = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(days=1)
    async with TestSessionLocal() as session:
        for field, snoozed_until in (
            ("expired", expired),
            ("future", future),
            ("without_deadline", None),
        ):
            session.add(
                SchemaDrift(
                    event_type_id=event_type_id,
                    field_name=field,
                    drift_type="new_field",
                    status="snoozed",
                    snoozed_until=snoozed_until,
                )
            )
        session.add(
            VariableValueDrift(
                project_id=project_id,
                variable_id=variable_id,
                event_id=event_id,
                observed_values=["b"],
                status="snoozed",
                snoozed_until=expired,
            )
        )
        session.add(
            VariableValueDrift(
                project_id=project_id,
                variable_id=variable_id,
                event_id=uuid.UUID(second_event.json()["id"]),
                observed_values=["c"],
                status="snoozed",
                snoozed_until=None,
            )
        )
        await session.commit()
    events = await client.get(f"/api/v1/projects/{slug}/events")
    assert events.status_code == 200, events.text
    assert {item["drift_count"] for item in events.json()["items"]} == {2}
    schema_list = await client.get(f"/api/v1/projects/{slug}/event-types/{event_type_id}/drifts")
    assert schema_list.status_code == 200, schema_list.text
    assert {item["field_name"] for item in schema_list.json()["items"]} == {
        "expired",
        "future",
        "without_deadline",
    }
    variables = await client.get(f"/api/v1/projects/{slug}/variables")
    assert variables.status_code == 200, variables.text
    assert variables.json()["items"][0]["open_drift_count"] == 2
    value_list = await client.get(f"/api/v1/projects/{slug}/variables/drifts")
    assert value_list.status_code == 200, value_list.text
    assert value_list.json()["total"] == 2
