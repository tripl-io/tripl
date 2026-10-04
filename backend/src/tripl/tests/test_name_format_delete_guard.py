"""The other two doors to the name-format outage: plan delete, and branch merge.

Accepting a ``missing_field`` drift is guarded in ``test_schema_drift_guard``.
That closed one of three ways to remove a FieldDefinition a scan's
``event_name_format`` names events by; the two here are the other two, and both
end in the same ``session.delete(field)`` and the same dead scan — every
collection failing on "the event name format references unknown keys"
(root cause of that outage).

The delete door and the merge door together also pin the DIVISION between them:
deleting the field on a BRANCH is allowed (a branch is where you plan a removal
and no scan reads it), and the merge is where that plan is checked.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.data_source import DataSource
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal


async def _project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _event_type(client: AsyncClient, slug: str, name: str = "track") -> uuid.UUID:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _field(client: AsyncClient, slug: str, event_type_id: uuid.UUID, name: str) -> uuid.UUID:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": name, "display_name": name, "field_type": "string"},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _scan_config(
    *,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID | None,
    name_format: str,
    name: str = "Old events (iOS)",
) -> None:
    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add(data_source)
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                data_source_id=data_source.id,
                project_id=project_id,
                event_type_id=event_type_id,
                name=name,
                base_query="SELECT * FROM events",
                time_column="time",
                event_name_format=name_format,
                cardinality_threshold=100,
            )
        )
        await session.commit()


async def _field_exists(event_type_id: uuid.UUID, name: str) -> bool:
    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == event_type_id,
                FieldDefinition.name == name,
            )
        )
        return row is not None


def _delete_url(slug: str, event_type_id: uuid.UUID, field_id: uuid.UUID) -> str:
    return f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields/{field_id}"


# --- door 2: DELETE /event-types/{id}/fields/{id} ---------------------------


@pytest.mark.asyncio
async def test_deleting_a_field_a_scan_names_events_by_is_refused(client: AsyncClient) -> None:
    """The production case, reached from the plan UI in one click instead of the
    drift badge. Must be a 409 — the same status the drift door returns, so the
    two read as one rule."""
    slug = "fdel-blocked"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    field_id = await _field(client, slug, event_type_id, "action")
    await _scan_config(project_id=project_id, event_type_id=event_type_id, name_format="{action}")

    resp = await client.delete(_delete_url(slug, event_type_id, field_id))

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    # The message must name the column, the config AND its format string, and say
    # what to do instead — a bare "conflict" leaves the operator stuck.
    assert "action" in detail
    assert "Old events (iOS)" in detail
    assert "{action}" in detail
    assert "Event name format" in detail
    assert await _field_exists(event_type_id, "action")


@pytest.mark.asyncio
async def test_deleting_a_field_no_name_format_uses_still_works(client: AsyncClient) -> None:
    """The guard must not swallow the ordinary delete it was built around."""
    slug = "fdel-allowed"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    await _field(client, slug, event_type_id, "action")
    field_id = await _field(client, slug, event_type_id, "screen_name")
    await _scan_config(project_id=project_id, event_type_id=event_type_id, name_format="{action}")

    resp = await client.delete(_delete_url(slug, event_type_id, field_id))

    assert resp.status_code == 204, resp.text
    assert not await _field_exists(event_type_id, "screen_name")
    assert await _field_exists(event_type_id, "action")


@pytest.mark.asyncio
async def test_deleting_a_dotted_placeholders_base_column_is_refused(client: AsyncClient) -> None:
    """{event.category} is walked out of the ``event`` column's JSON, and
    generate_events assembles ``col.path`` keys only for columns that have a
    FieldDefinition — so deleting ``event`` kills it exactly as it kills
    {action}."""
    slug = "fdel-dotted"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    field_id = await _field(client, slug, event_type_id, "event")
    await _scan_config(
        project_id=project_id, event_type_id=event_type_id, name_format="{event.category}"
    )

    resp = await client.delete(_delete_url(slug, event_type_id, field_id))

    assert resp.status_code == 409, resp.text
    assert await _field_exists(event_type_id, "event")


@pytest.mark.asyncio
async def test_a_config_bound_to_another_event_type_does_not_block_the_delete(
    client: AsyncClient,
) -> None:
    """The delete door must scope by the event type it is deleting from, not by
    the project: a config bound elsewhere cannot produce events for this type."""
    slug = "fdel-scope"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    other_event_type_id = await _event_type(client, slug, "other_track")
    field_id = await _field(client, slug, event_type_id, "action")
    await _scan_config(
        project_id=project_id, event_type_id=other_event_type_id, name_format="{action}"
    )

    resp = await client.delete(_delete_url(slug, event_type_id, field_id))

    assert resp.status_code == 204, resp.text
    assert not await _field_exists(event_type_id, "action")


# --- door 3: POST /branches/{id}/merge -------------------------------------


async def _branch_event_type_and_field(
    client: AsyncClient, slug: str, branch_id: str, field_name: str
) -> tuple[str, str]:
    resp = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    event_type = next(item for item in resp.json() if item["name"] == "track")
    field = next(f for f in event_type["field_definitions"] if f["name"] == field_name)
    return event_type["id"], field["id"]


async def _branch_removing(client: AsyncClient, slug: str, field_name: str) -> str:
    """A branch whose only change is deleting ``field_name`` from ``track``."""
    created = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "drop-field"})
    assert created.status_code == 201, created.text
    branch_id = created.json()["id"]

    b_et_id, b_field_id = await _branch_event_type_and_field(client, slug, branch_id, field_name)
    # Deleting on the BRANCH is deliberately allowed: no scan reads a branch's
    # plan, and a branch is exactly where "drop the column and rewrite the scan's
    # name format" gets staged. If this ever starts returning 409 the guard has
    # been widened past the live plan and branches stop being usable for the
    # repair the guard itself recommends.
    deleted = await client.delete(
        f"{_delete_url(slug, uuid.UUID(b_et_id), uuid.UUID(b_field_id))}?branch={branch_id}"
    )
    assert deleted.status_code == 204, deleted.text
    return branch_id


async def _approve_and_merge(client: AsyncClient, slug: str, branch_id: str):
    for action in ("submit", "approve"):
        resp = await client.post(
            f"/api/v1/projects/{slug}/branches/{branch_id}/transition",
            json={"action": action},
        )
        assert resp.status_code == 200, resp.text
    return await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")


@pytest.mark.asyncio
async def test_merging_a_branch_that_removes_a_name_format_field_is_refused(
    client: AsyncClient,
) -> None:
    """The whole merge is refused, not just the one deletion.

    Applying the rest and silently keeping the field would leave main disagreeing
    with the branch that was just marked merged, and every later three-way merge
    would compare against a base that never describes that state.
    """
    slug = "fmerge-blocked"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    await _field(client, slug, event_type_id, "action")
    await _scan_config(project_id=project_id, event_type_id=event_type_id, name_format="{action}")

    branch_id = await _branch_removing(client, slug, "action")
    resp = await _approve_and_merge(client, slug, branch_id)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "track.action" in detail
    assert "Old events (iOS)" in detail
    assert "Event name format" in detail
    # Refused means refused: main keeps the field and the branch is not merged.
    assert await _field_exists(event_type_id, "action")
    branch = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}")
    assert branch.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_merging_a_branch_that_removes_an_unnamed_field_still_works(
    client: AsyncClient,
) -> None:
    """The guard must not block the ordinary field-removal merge."""
    slug = "fmerge-allowed"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    await _field(client, slug, event_type_id, "action")
    await _field(client, slug, event_type_id, "screen_name")
    await _scan_config(project_id=project_id, event_type_id=event_type_id, name_format="{action}")

    branch_id = await _branch_removing(client, slug, "screen_name")
    resp = await _approve_and_merge(client, slug, branch_id)

    assert resp.status_code == 200, resp.text
    assert not await _field_exists(event_type_id, "screen_name")
    assert await _field_exists(event_type_id, "action")
