"""Accepting a missing_field drift must not delete a column a scan names events by.

Regression suite for tripl-3mmh, the root cause of the four-day tripl-lpin
outage: accepting a (probably false-positive) ``missing_field`` drift on
``action`` deleted the FieldDefinition, and the scan config whose
``event_name_format`` was ``{action}`` could no longer name a single event.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.data_source import DataSource
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.tests.conftest import TestSessionLocal

SLUG = "drift-guard"
ACTIONS_URL = f"/api/v1/projects/{SLUG}/event-types/drifts"


async def _project_and_event_type(client: AsyncClient) -> tuple[uuid.UUID, uuid.UUID]:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Drift Guard", "slug": SLUG, "description": ""},
    )
    assert project_resp.status_code == 201, project_resp.text
    event_type_resp = await client.post(
        f"/api/v1/projects/{SLUG}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    assert event_type_resp.status_code == 201, event_type_resp.text
    return uuid.UUID(project_resp.json()["id"]), uuid.UUID(event_type_resp.json()["id"])


async def _seed(
    *,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
    name_format: str | None,
    bind_config_to_event_type: bool = True,
    config_project_id: uuid.UUID | None = None,
    config_event_type_id: uuid.UUID | None = None,
    config_event_type_column: str | None = "event_type",
    declared_fields: tuple[str, ...] = ("action",),
    drift_field: str = "action",
    drift_type: str = "missing_field",
    config_name: str = "Old events (iOS)",
) -> uuid.UUID:
    """Data source + scan config + declared fields + one drift; returns drift id.

    ``config_project_id`` / ``config_event_type_id`` put the scan config somewhere
    OTHER than the drift's own project/event type, which is what pins the loader's
    scoping: without them a predicate that matched every config in the database
    would leave this whole suite green.

    ``config_event_type_column`` only reaches the row when the config is UNBOUND,
    because the two are alternatives rather than a pair: a scan either names one
    event type or discovers its types from a column. Unbound-with-a-column is the
    grouped scan; unbound-WITHOUT one (``config_event_type_column=None``) is the
    orphan ``ON DELETE SET NULL`` leaves behind when a bound event type is
    deleted, and since tripl-0zpq.254 the loader tells the two apart.
    """
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
        bound_event_type_id = (
            config_event_type_id
            if config_event_type_id is not None
            else (event_type_id if bind_config_to_event_type else None)
        )
        # A bound scan never carries a discovery column — the two are the two
        # ways of answering "which event type does this row belong to", not a
        # pair — so the column is stamped on the unbound seeds only.
        discovery_column = config_event_type_column if bound_event_type_id is None else None
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                data_source_id=data_source.id,
                project_id=config_project_id if config_project_id is not None else project_id,
                event_type_id=bound_event_type_id,
                event_type_column=discovery_column,
                name=config_name,
                base_query="SELECT * FROM events",
                time_column="time",
                event_name_format=name_format,
                cardinality_threshold=100,
            )
        )
        for order, name in enumerate(declared_fields):
            session.add(
                FieldDefinition(
                    event_type_id=event_type_id,
                    name=name,
                    display_name=name,
                    field_type="string",
                    is_required=False,
                    order=order,
                )
            )
        drift = SchemaDrift(
            event_type_id=event_type_id,
            scan_config_id=None,
            field_name=drift_field,
            drift_type=drift_type,
            observed_type="String" if drift_type == "type_changed" else None,
            declared_type="string",
            sample_value=None,
        )
        session.add(drift)
        await session.commit()
        return drift.id


async def _extra_event_type(client: AsyncClient, name: str) -> uuid.UUID:
    """A SECOND event type in the same project, for the scoping negatives."""
    resp = await client.post(
        f"/api/v1/projects/{SLUG}/event-types",
        json={"name": name, "display_name": name},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _extra_project(client: AsyncClient, slug: str) -> uuid.UUID:
    """A SECOND project, for the project-scoping negative."""
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _field(event_type_id: uuid.UUID, name: str) -> FieldDefinition | None:
    async with TestSessionLocal() as session:
        return await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == event_type_id,
                FieldDefinition.name == name,
            )
        )


@pytest.mark.asyncio
async def test_accept_missing_field_is_blocked_when_a_name_format_uses_the_column(
    client: AsyncClient,
) -> None:
    """The production case: {action} + a missing_field drift on ``action``.

    Must fail on main, where the accept deletes the field and every subsequent
    collection dies with "the event name format references unknown keys".
    """
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id, event_type_id=event_type_id, name_format="{action}"
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "action" in detail
    assert "Old events (iOS)" in detail
    assert "Event name format" in detail
    assert await _field(event_type_id, "action") is not None


@pytest.mark.asyncio
async def test_accept_missing_field_is_blocked_by_a_project_wide_config(
    client: AsyncClient,
) -> None:
    """A grouped scan discovers its event types from a column, so its name format
    governs this event type too — the ``or_`` NULL arm, BOTH halves of it.

    ``event_type_id IS NULL`` alone is not the grouped scan and has not been one
    since tripl-0zpq.254; the config also needs the ``event_type_column`` it
    discovers types from. The sibling test below is the same seed without that
    column, and it must NOT block.
    """
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        bind_config_to_event_type=False,
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 409, resp.text
    assert await _field(event_type_id, "action") is not None


@pytest.mark.asyncio
async def test_accept_is_not_blocked_by_a_scan_orphaned_by_a_deleted_event_type(
    client: AsyncClient,
) -> None:
    """The NULL arm's OTHER half, and the only place this door pins it.

    ``scan_configs.event_type_id`` is ``ON DELETE SET NULL``, so deleting a bound
    event type — or merging a branch that removed it — leaves a config with no
    binding AND no ``event_type_column``. That config discovers nothing and
    collects nothing, so its ``{action}`` says nothing about this event type's
    fields; until tripl-0zpq.254 it read as project-wide and blocked every
    missing_field accept in the project.

    Delete the ``ScanConfig.event_type_column.is_not(None)`` line from the
    ``and_(...)`` in ``load_governing_scan_configs_by_type`` and this orphan
    starts governing again: the accept goes back to 409 and both assertions
    below fail.
    """
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        bind_config_to_event_type=False,
        config_event_type_column=None,
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert await _field(event_type_id, "action") is None


@pytest.mark.asyncio
async def test_accept_is_not_blocked_by_a_config_bound_to_another_event_type(
    client: AsyncClient,
) -> None:
    """The event-type arm's NEGATIVE. A config bound to a DIFFERENT event type
    cannot produce events for this one, so its ``{action}`` says nothing about
    this drift. Widen the loader to every config in the project and this goes
    red — which is the mutation the rest of the suite could not see."""
    project_id, event_type_id = await _project_and_event_type(client)
    other_event_type_id = await _extra_event_type(client, "other_track")
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        config_event_type_id=other_event_type_id,
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert await _field(event_type_id, "action") is None


@pytest.mark.asyncio
async def test_accept_is_not_blocked_by_a_config_in_another_project(
    client: AsyncClient,
) -> None:
    """The project filter's NEGATIVE, pinned through the NULL arm on purpose.

    The other project's config is a REAL grouped scan — ``event_type_id IS
    NULL`` and an ``event_type_column`` to discover types from — so the ``or_``
    matches it on every count and ONLY ``project_id`` keeps it out. Drop that
    term and one project's grouped scan starts governing another's plan. Seed it
    without the column and this stops testing the project filter, because the
    NULL arm would then reject it whatever project it sat in."""
    project_id, event_type_id = await _project_and_event_type(client)
    other_project_id = await _extra_project(client, "drift-guard-other")
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        bind_config_to_event_type=False,
        config_project_id=other_project_id,
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert await _field(event_type_id, "action") is None


@pytest.mark.asyncio
async def test_accept_missing_field_succeeds_for_a_column_no_name_format_uses(
    client: AsyncClient,
) -> None:
    """The guard must not swallow the ordinary accept it was built around."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        declared_fields=("action", "screen_name"),
        drift_field="screen_name",
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    assert await _field(event_type_id, "screen_name") is None
    assert await _field(event_type_id, "action") is not None


@pytest.mark.asyncio
async def test_accept_missing_field_succeeds_when_no_config_sets_a_name_format(
    client: AsyncClient,
) -> None:
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(project_id=project_id, event_type_id=event_type_id, name_format=None)

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert await _field(event_type_id, "action") is None


@pytest.mark.asyncio
async def test_accept_missing_field_ignores_a_blank_name_format(client: AsyncClient) -> None:
    """A config whose format is "   " does not block an accept.

    This test does NOT pin the ``.strip()`` filter and never could: ``"   "``
    yields no placeholders, so the guard would let this through with or without
    the filter. ``test_load_governing_scan_configs_drops_a_blank_name_format``
    below is where the filter itself is pinned, at the one place it now lives.
    """
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(project_id=project_id, event_type_id=event_type_id, name_format="   ")

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert await _field(event_type_id, "action") is None


@pytest.mark.asyncio
async def test_load_governing_scan_configs_drops_a_blank_name_format(
    client: AsyncClient,
) -> None:
    """The ``.strip()`` filter, pinned where it now lives.

    ``load_governing_scan_configs`` promises "scan configs with a NON-BLANK name
    format", and every caller reads that promise rather than re-checking. The
    refactor moved the filter out of ``_resolve_event_name_format`` into shared
    code, and nothing observed it any more — deleting ``.strip()`` left the whole
    guard suite green. Here it is observable: the blank config is loaded by the
    SQL (``IS NOT NULL`` passes) and dropped only by the Python filter.
    """
    from tripl.services.scan_config_lookup import load_governing_scan_configs

    project_id, event_type_id = await _project_and_event_type(client)
    await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="   ",
        config_name="Blank format",
    )
    await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        declared_fields=(),
        # A distinct field: SchemaDrift is unique on (event_type, field, type)
        # and this second _seed only exists to add the second scan config.
        drift_field="screen_name",
        config_name="Real format",
    )

    async with TestSessionLocal() as session:
        configs = await load_governing_scan_configs(
            session, project_id=project_id, event_type_id=event_type_id
        )

    assert [config.name for config in configs] == ["Real format"]


@pytest.mark.asyncio
async def test_accept_missing_field_is_blocked_for_a_dotted_placeholder_base_column(
    client: AsyncClient,
) -> None:
    """{event.category} is walked out of the ``event`` column's JSON, and
    generate_events only assembles ``col.path`` keys for columns that have a
    FieldDefinition — so deleting ``event`` kills it exactly as it kills {action}."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{event.category}",
        declared_fields=("event",),
        drift_field="event",
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 409, resp.text
    assert await _field(event_type_id, "event") is not None


@pytest.mark.asyncio
async def test_accept_missing_field_is_not_blocked_by_a_dotted_placeholder_on_another_column(
    client: AsyncClient,
) -> None:
    """The guard matches the BASE column, never a path segment: {event.category}
    does not protect a top-level column that happens to be called ``category``."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{event.category}",
        declared_fields=("event", "category"),
        drift_field="category",
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert await _field(event_type_id, "category") is None
    assert await _field(event_type_id, "event") is not None


@pytest.mark.asyncio
async def test_accept_missing_field_with_no_field_definition_is_a_noop_not_a_conflict(
    client: AsyncClient,
) -> None:
    """Nothing to delete means nothing to protect — this stays a no-op accept."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        declared_fields=(),
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_force_accepts_a_name_format_column_when_a_note_explains_why(
    client: AsyncClient,
) -> None:
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id, event_type_id=event_type_id, name_format="{action}"
    )

    resp = await client.post(
        f"{ACTIONS_URL}/{drift_id}/actions",
        json={"action": "accept", "force": True, "note": "column dropped upstream"},
    )

    assert resp.status_code == 200, resp.text
    assert await _field(event_type_id, "action") is None


@pytest.mark.asyncio
async def test_force_without_a_note_is_rejected(client: AsyncClient) -> None:
    """The override is non-reflexive: no note, no force (422 from the validator)."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id, event_type_id=event_type_id, name_format="{action}"
    )

    resp = await client.post(
        f"{ACTIONS_URL}/{drift_id}/actions",
        json={"action": "accept", "force": True},
    )

    assert resp.status_code == 422, resp.text
    assert await _field(event_type_id, "action") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["snooze", "false_positive", "reopen"])
async def test_force_is_rejected_on_every_action_but_accept(
    client: AsyncClient, action: str
) -> None:
    """`force` overrides one guard, and that guard only fires on accept.

    Accepting it elsewhere would make the contract looser than the thing it
    overrides, and would silently swallow a client that sent it by mistake - on
    a field whose whole purpose is to be hard to reach by accident.
    """
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id, event_type_id=event_type_id, name_format="{action}"
    )

    body: dict[str, object] = {"action": action, "force": True, "note": "why not"}
    if action == "snooze":
        body["snoozed_until"] = "2099-01-01T00:00:00Z"

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json=body)

    assert resp.status_code == 422, resp.text
    assert "force is only meaningful when action is accept" in resp.text
    assert await _field(event_type_id, "action") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["snooze", "false_positive", "reopen"])
async def test_those_same_actions_work_without_force(client: AsyncClient, action: str) -> None:
    """The positive control: the rejection above is about `force`, not the action."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id, event_type_id=event_type_id, name_format="{action}"
    )

    body: dict[str, object] = {"action": action}
    if action == "snooze":
        body["snoozed_until"] = "2099-01-01T00:00:00Z"

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json=body)

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_other_drift_types_on_a_name_format_column_still_accept(
    client: AsyncClient,
) -> None:
    """The guard is missing_field-only: a type_changed accept updates, not deletes."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format="{action}",
        drift_type="type_changed",
    )

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "accept"})

    assert resp.status_code == 200, resp.text
    field = await _field(event_type_id, "action")
    assert field is not None
    assert field.field_type == "string"


@pytest.mark.asyncio
async def test_accept_keeps_a_note_stored_by_an_earlier_action(client: AsyncClient) -> None:
    """Separate defect: ``note`` is optional on every action, so assigning it
    unconditionally erased a note an earlier action had stored."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format=None,
        drift_field="screen_name",
        declared_fields=("screen_name",),
    )

    snooze = await client.post(
        f"{ACTIONS_URL}/{drift_id}/actions",
        json={
            "action": "snooze",
            "note": "waiting on the iOS release",
            "snoozed_until": "2099-01-01T00:00:00Z",
        },
    )
    assert snooze.status_code == 200, snooze.text

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "false_positive"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution_note"] == "waiting on the iOS release"


@pytest.mark.asyncio
async def test_an_explicit_null_note_clears_it_without_reopening(client: AsyncClient) -> None:
    """OMITTED and EXPLICITLY NULL are different requests.

    The keep-the-note fix above tested ``data.note is None``, which collapses the
    two and took away the only way to CLEAR a wrong note short of reopening the
    drift — i.e. made it permanent. ``model_fields_set`` tells them apart.
    """
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format=None,
        drift_field="screen_name",
        declared_fields=("screen_name",),
    )

    snooze = await client.post(
        f"{ACTIONS_URL}/{drift_id}/actions",
        json={
            "action": "snooze",
            "note": "wrong note, pasted from another drift",
            "snoozed_until": "2099-01-01T00:00:00Z",
        },
    )
    assert snooze.status_code == 200, snooze.text

    resp = await client.post(
        f"{ACTIONS_URL}/{drift_id}/actions",
        json={"action": "snooze", "note": None, "snoozed_until": "2099-01-01T00:00:00Z"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution_note"] is None
    assert resp.json()["status"] == "snoozed"


@pytest.mark.asyncio
async def test_reopen_clears_the_resolution_note(client: AsyncClient) -> None:
    """A reopened drift has no resolution any more, so its note goes with it."""
    project_id, event_type_id = await _project_and_event_type(client)
    drift_id = await _seed(
        project_id=project_id,
        event_type_id=event_type_id,
        name_format=None,
        drift_field="screen_name",
        declared_fields=("screen_name",),
    )

    accepted = await client.post(
        f"{ACTIONS_URL}/{drift_id}/actions",
        json={"action": "accept", "note": "gone for good"},
    )
    assert accepted.status_code == 200, accepted.text

    resp = await client.post(f"{ACTIONS_URL}/{drift_id}/actions", json={"action": "reopen"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution_note"] is None
