"""Findings that cross module boundaries and so no single module's tests reached.

every reindex runs inside the writer's own transaction and so
shares the writer's identity map. A ``selectinload`` does NOT overwrite a
collection that is already loaded, so ``build_documents`` rebuilt an event's
documents from the tags / field values / meta values the caller had in memory
BEFORE it replaced them, and an event type's field documents from the field list
as it stood before a field was created or deleted. The rebuilt text matched the
stored ``content_hash``, the row was kept, and the index sat exactly one write
behind until some later reindex happened to run from a fresh session. It crosses
``event_service`` and ``field_service``, which is why it waited for the seam.

``event_changes.field`` was ``String(100)`` while
``_record_keyed_changes`` writes ``field:<name>`` / ``meta:<name>`` for names
that may themselves be 100 characters, so editing such a field's value was a
rolled-back 500 on PostgreSQL. SQLite does not enforce VARCHAR widths, so no
database-backed test could have caught it; what is pinned here is the ARITHMETIC
— the longest key the writer can produce against the width the column declares.

eight partial-update schemas let an explicit JSON ``null``
through to a NOT NULL column, where the generic ``setattr`` loops turned a client
error into a blank 500. ``schemas/not_null_update`` was built for exactly
this; these reuse it rather than inventing a second spelling.

``accept_shadow_event`` built the event without passing
``user_id``, so the accepted event's own 'created' history row named nobody,
while the docs promise an accepted candidate is indistinguishable from one you
typed.

``apply_drift_action`` guarded its event-type cache bust on
``event_type.branch_id is None``, a NOT NULL column since 4e5f60718293, so the
guard never fired and accepting a drift left the 300 s ``GET /event-types``
cache serving the field list the accept had just changed.

The demo's own trail: every event now opens with the
``created`` history row a real create writes, and the audit builder files its
field rows in the shape ``api/v1/fields.py`` really writes, names the warehouse
it really seeded, and covers the objects the recipe authors.

Each test names the production line whose revert reddens it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from tripl import cache
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.event_change import EventChange
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_OPEN, SchemaDrift
from tripl.models.search_document import SearchDocument
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW, ShadowEventCandidate
from tripl.schemas.data_source import (
    _DATA_SOURCE_NOT_NULL_UPDATE_FIELDS,
    DataSourceUpdate,
)
from tripl.schemas.event import _NOT_NULL_UPDATE_FIELDS as _EVENT_NOT_NULL_UPDATE_FIELDS
from tripl.schemas.event import EventUpdate
from tripl.schemas.event_type import (
    _EVENT_TYPE_NOT_NULL_UPDATE_FIELDS,
    EventTypeUpdate,
)
from tripl.schemas.field_definition import (
    _FIELD_NOT_NULL_UPDATE_FIELDS,
    FieldDefinitionUpdate,
)
from tripl.schemas.meta_field import _META_FIELD_NOT_NULL_UPDATE_FIELDS, MetaFieldUpdate
from tripl.schemas.project import _PROJECT_NOT_NULL_UPDATE_FIELDS, ProjectUpdate
from tripl.schemas.scan_config import (
    _SCAN_CONFIG_NOT_NULL_UPDATE_FIELDS,
    ScanConfigUpdate,
)
from tripl.schemas.variable import _VARIABLE_NOT_NULL_UPDATE_FIELDS, VariableUpdate
from tripl.services import event_service
from tripl.services.demo.builders import audit as audit_builder
from tripl.services.demo.builders.alerts import (
    _DEMO_SINK_NAME,
    _DISABLED_EXTERNAL_NAME,
    _FIRING_RULE_NAME,
    _HEALTHY_RULE_NAME,
)
from tripl.services.demo.scenario import DemoContext
from tripl.services.project_service import demo_data_source_name
from tripl.tests._project_ids import project_id_by_slug
from tripl.tests.conftest import TestSessionLocal

# --- helpers ------------------------------------------------------------------


async def _project(client: AsyncClient, slug: str) -> str:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _event_type(client: AsyncClient, slug: str, name: str = "pv") -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _documents(project_id: str, entity_type: str) -> list[str]:
    """Titles of the stored search documents of one kind, for one project."""
    async with TestSessionLocal() as session:
        rows = await session.scalars(
            select(SearchDocument.title).where(
                SearchDocument.project_id == uuid.UUID(project_id),
                SearchDocument.entity_type == entity_type,
            )
        )
        return sorted(rows)


def _record_dropped_prefixes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every ``cache.delete_prefix`` call.

    The suite runs without Redis, where the real helper is a no-op, so the bust
    is only observable here — the idiom ``test_field_audit_trail`` and
    ``test_branch_context_main_and_read_only`` both use.
    """
    dropped: list[str] = []

    async def record(prefix: str) -> None:
        dropped.append(prefix)

    monkeypatch.setattr(cache, "delete_prefix", record)
    return dropped


# --- tag replacement reaches the index ----------------------------------------


@pytest.mark.asyncio
async def test_replacing_an_events_tags_reaches_the_index_in_the_same_save(
    client: AsyncClient,
) -> None:
    """Reverting ``.execution_options(populate_existing=True)`` on the ``Event``
    query in ``_search_documents.build_documents`` reddens this.

    Without it the rebuild reads ``event.tags`` out of the identity map, which
    still holds the row the PATCH deleted — so the index keeps a ``#alpha``
    document for a tag that no longer exists and never learns about ``#bravo``.
    The existing coverage renames the event, a SCALAR that is already current in
    memory, which is why nothing caught this.
    """
    slug = "b7s-tag-index"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": "checkout", "tags": ["alpha"]},
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    assert await _documents(project_id, "tag") == ["#alpha"]

    patched = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"tags": ["bravo"]},
    )
    assert patched.status_code == 200, patched.text

    # The whole finding in one assertion: the index holds what the transaction
    # holds, not what the caller had loaded before it wrote.
    assert await _documents(project_id, "tag") == ["#bravo"]

    # And the same thing from the surface the finding is about.
    found = await client.get(f"/api/v1/projects/{slug}/search?q=bravo&types=tag")
    assert found.status_code == 200, found.text
    assert [item["title"] for item in found.json()["items"]] == ["#bravo"]


@pytest.mark.asyncio
async def test_creating_and_deleting_a_field_reaches_the_index_in_the_same_write(
    client: AsyncClient,
) -> None:
    """Reverting ``.execution_options(populate_existing=True)`` on the
    ``EventType`` query in ``_search_documents.build_documents`` reddens this,
    in BOTH directions.

    ``field_service`` loads the event type — which selectin-loads
    ``field_definitions`` — before it adds or deletes a field, so without the
    option the rebuild sees the list as it stood one write ago: a created field
    has no document, and a deleted one keeps the document it had.
    """
    slug = "b7s-field-index"
    project_id = await _project(client, slug)
    event_type_id = await _event_type(client, slug)

    created = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": "screen", "display_name": "Screen", "field_type": "string"},
    )
    assert created.status_code == 201, created.text
    field_id = created.json()["id"]
    assert await _documents(project_id, "field") == ["Screen"]

    removed = await client.delete(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields/{field_id}"
    )
    assert removed.status_code == 204, removed.text
    assert await _documents(project_id, "field") == []


# --- column widths ------------------------------------------------------------


def _column_width(model: type, column: str) -> int:
    length = model.__table__.c[column].type.length  # type: ignore[attr-defined]
    assert isinstance(length, int), f"{model.__name__}.{column} declares no width"
    return length


@pytest.mark.asyncio
async def test_a_keyed_history_row_for_the_longest_legal_name_fits_its_column() -> None:
    """Reverting ``EventChange.field`` to ``String(100)`` reddens this.

    Asserted as arithmetic rather than against a database ON PURPOSE: the suite
    runs on SQLite, which ignores a VARCHAR width entirely, so a test that
    INSERTed the row would pass green here and 500 on PostgreSQL — which is
    precisely how the defect shipped. What is pinned is the longest key the
    writer can produce, measured against the width the column declares, so the
    two cannot drift apart again whichever of them moves.
    """
    written: list[EventChange] = []
    session = cast(Any, SimpleNamespace(add=written.append))
    event = cast(Any, SimpleNamespace(id=uuid.uuid4()))
    budget = _column_width(EventChange, "field")

    for prefix, definition_model in (("field", FieldDefinition), ("meta", MetaFieldDefinition)):
        written.clear()
        definition_id = uuid.uuid4()
        longest_name = "x" * _column_width(definition_model, "name")
        event_service._record_keyed_changes(
            session,
            event=event,
            prefix=prefix,
            names={definition_id: longest_name},
            old={definition_id: "before"},
            new={definition_id: "after"},
            user_id=None,
        )
        assert len(written) == 1, written
        key = written[0].field
        # Not vacuous: the key really is longer than the 100 the column held.
        assert key == f"{prefix}:{longest_name}"
        assert len(key) > 100
        assert len(key) <= budget, (
            f"a {prefix} history key is {len(key)} characters and "
            f"event_changes.field holds {budget}"
        )


# --- explicit null on NOT NULL update fields ----------------------------------


# (schema, a NOT NULL field of it, a nullable field of it that must stay allowed).
# EVERY member of every ``_*_NOT_NULL_UPDATE_FIELDS`` frozenset has a row here —
# ``test_the_table_covers_every_field_each_schema_declares`` below is what keeps
# that true, because a name dropped from a frozenset can only redden a row that
# names it. The nullable column is what keeps this from being satisfied by a
# blanket "reject every null"; it is ``None`` where the schema has no nullable
# field left over to pair with.
_NOT_NULL_PATCH_FIELDS: tuple[tuple[type[BaseModel], str, str | None], ...] = (
    (ProjectUpdate, "name", None),
    (ProjectUpdate, "slug", None),
    (ProjectUpdate, "description", None),
    (EventTypeUpdate, "display_name", None),
    (EventTypeUpdate, "description", None),
    (EventTypeUpdate, "color", None),
    (EventTypeUpdate, "order", None),
    (FieldDefinitionUpdate, "display_name", "enum_options"),
    (FieldDefinitionUpdate, "field_type", "contract_regex"),
    (FieldDefinitionUpdate, "is_required", None),
    (FieldDefinitionUpdate, "description", "contract_min_value"),
    (FieldDefinitionUpdate, "order", "contract_max_value"),
    (FieldDefinitionUpdate, "sensitivity", None),
    (MetaFieldUpdate, "display_name", "default_value"),
    (MetaFieldUpdate, "field_type", "link_template"),
    (MetaFieldUpdate, "is_required", None),
    (MetaFieldUpdate, "allow_multiple", None),
    (MetaFieldUpdate, "order", "enum_options"),
    (MetaFieldUpdate, "sensitivity", None),
    (EventUpdate, "name", "owner_id"),
    (EventUpdate, "description", "sunset_at"),
    (EventUpdate, "status", "superseded_by_event_id"),
    (EventUpdate, "reviewed", None),
    (ScanConfigUpdate, "name", "event_type_id"),
    (ScanConfigUpdate, "base_query", "time_column"),
    (ScanConfigUpdate, "json_value_paths", "platform_column"),
    (ScanConfigUpdate, "event_group_rules", "event_type_column"),
    (ScanConfigUpdate, "metric_breakdown_columns", None),
    (ScanConfigUpdate, "distribution_drift_fields", "event_name_format"),
    (ScanConfigUpdate, "cardinality_threshold", None),
    (ScanConfigUpdate, "setup_preset", None),
    (ScanConfigUpdate, "json_string_columns", None),
    (DataSourceUpdate, "name", "timeout_seconds"),
    (DataSourceUpdate, "db_type", None),
    (DataSourceUpdate, "host", "password"),
    (DataSourceUpdate, "port", None),
    (DataSourceUpdate, "database_name", None),
    (DataSourceUpdate, "username", None),
    (VariableUpdate, "name", None),
    (VariableUpdate, "variable_type", None),
    (VariableUpdate, "description", None),
    (VariableUpdate, "allowed_values", None),
    (VariableUpdate, "bindings", None),
    (VariableUpdate, "excluded_from_scans", None),
)

# The set each schema actually declares, so the table above cannot fall behind
# it. Imported by their private names on purpose: the point of the assertion is
# that the two spellings of "which fields are NOT NULL" agree, and the frozenset
# is the one the validator reads.
_DECLARED_NOT_NULL_FIELDS: tuple[tuple[type[BaseModel], frozenset[str]], ...] = (
    (ProjectUpdate, _PROJECT_NOT_NULL_UPDATE_FIELDS),
    (EventTypeUpdate, _EVENT_TYPE_NOT_NULL_UPDATE_FIELDS),
    (FieldDefinitionUpdate, _FIELD_NOT_NULL_UPDATE_FIELDS),
    (MetaFieldUpdate, _META_FIELD_NOT_NULL_UPDATE_FIELDS),
    (EventUpdate, _EVENT_NOT_NULL_UPDATE_FIELDS),
    (ScanConfigUpdate, _SCAN_CONFIG_NOT_NULL_UPDATE_FIELDS),
    (DataSourceUpdate, _DATA_SOURCE_NOT_NULL_UPDATE_FIELDS),
    (VariableUpdate, _VARIABLE_NOT_NULL_UPDATE_FIELDS),
)


@pytest.mark.parametrize(("schema", "declared"), _DECLARED_NOT_NULL_FIELDS)
def test_the_table_covers_every_field_each_schema_declares(
    schema: type[BaseModel], declared: frozenset[str]
) -> None:
    """The table above tests one field per row, so a field it never names is a
    field nothing pins: dropping that name from its frozenset would leave the
    whole suite green. Seven were missing when this was written — ``db_type``,
    ``description``/``order`` on fields, ``order``/``sensitivity`` on meta
    fields, and ``event_group_rules``/``distribution_drift_fields`` on scan
    configs.

    RED whenever the two drift apart in either direction, which is the point:
    adding a name to a frozenset without a row here fails, and so does a row
    naming a field the schema no longer refuses.
    """
    covered = {field for tested, field, _ in _NOT_NULL_PATCH_FIELDS if tested is schema}
    assert covered == set(declared), sorted(set(declared) ^ covered)


@pytest.mark.parametrize(("schema", "not_null_field", "nullable_field"), _NOT_NULL_PATCH_FIELDS)
def test_an_explicit_null_on_a_not_null_patch_field_is_refused_by_name(
    schema: type[BaseModel], not_null_field: str, nullable_field: str | None
) -> None:
    """Removing the ``_reject_explicit_nulls`` validator from the named schema
    reddens the rows for that schema.

    Three things at once, and all three are the finding: the field is optional
    when UNSENT (which is what ``| None`` is there for), an explicit null is a
    422 that names it rather than an IntegrityError rendered as a blank 500, and
    a genuinely nullable neighbour still accepts its null — a blanket refusal
    would take away the only way to clear those.
    """
    unset = schema()
    assert not_null_field not in unset.model_fields_set

    with pytest.raises(ValidationError) as refused:
        schema.model_validate({not_null_field: None})
    message = str(refused.value)
    assert "cannot be null" in message, message
    assert not_null_field in message, message

    if nullable_field is not None:
        cleared = schema.model_validate({nullable_field: None})
        assert nullable_field in cleared.model_fields_set


@pytest.mark.asyncio
async def test_an_explicit_null_on_a_patch_route_answers_422_not_500(
    client: AsyncClient,
) -> None:
    """The wire half of the same finding: reverting ``ProjectUpdate``'s validator
    reddens this, because ``update_project``'s ``setattr`` loop writes the None
    onto a NOT NULL column and the unhandled-exception handler renders the
    IntegrityError as a blank 500.
    """
    slug = "b7s-null-patch"
    await _project(client, slug)

    refused = await client.patch(f"/api/v1/projects/{slug}", json={"name": None})
    assert refused.status_code == 422, refused.text
    assert "name" in str(refused.json()["detail"])

    # The honest client is untouched: an unsent field still means "leave it".
    kept = await client.patch(f"/api/v1/projects/{slug}", json={"description": "still here"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["name"] == slug


# --- accepting a candidate names the editor -----------------------------------


@pytest.mark.asyncio
async def test_accepting_a_candidate_names_the_editor_in_the_created_history_row(
    client: AsyncClient,
) -> None:
    """Dropping ``user_id=user_id`` from ``reconciliation_service``'s
    ``create_event`` call reddens this: the row is still written, with a null
    editor.

    The docs say an accepted candidate is indistinguishable from one you typed.
    Its first history row is where that was visibly false.
    """
    slug = "b7s-accept-author"
    project_id = uuid.UUID(await _project(client, slug))
    event_type_id = uuid.UUID(await _event_type(client, slug))
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    editor_email = me.json()["email"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            event_type_id=event_type_id,
            name="scan",
            base_query="SELECT 1",
            cardinality_threshold=100,
        )
        session.add(config)
        await session.flush()
        candidate = ShadowEventCandidate(
            project_id=project_id,
            scan_config_id=config.id,
            event_type_id=event_type_id,
            event_name="pv | checkout",
            observed_count=7,
            first_seen_at=datetime(2020, 1, 2, tzinfo=UTC),
            last_seen_at=datetime.now(UTC),
            status=SHADOW_STATUS_NEW,
        )
        session.add(candidate)
        await session.commit()
        candidate_id = candidate.id

    accepted = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate_id}/accept",
        json={"name": "Checkout Screen"},
    )
    assert accepted.status_code == 200, accepted.text
    event_id = accepted.json()["event_id"]

    history = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/history")
    assert history.status_code == 200, history.text
    creations = [row for row in history.json() if row["field"] == "created"]
    assert len(creations) == 1, history.json()
    assert creations[0]["user_email"] == editor_email


# --- accepting a drift busts the event-type cache -----------------------------


@pytest.mark.asyncio
async def test_accepting_a_schema_drift_busts_the_cached_event_type_list(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring ``if event_type.branch_id is None:`` in
    ``schema_drift_service.apply_drift_action`` reddens this, because that
    condition is UNREACHABLE — ``event_types.branch_id`` has been NOT NULL since
    4e5f60718293.

    Accepting a ``new_field`` drift adds a FieldDefinition, which is exactly what
    ``GET /event-types`` serves and caches for 300 s. ``field_service`` busts the
    same cache on every write it makes to the same rows; this is the door that
    did not.
    """
    slug = "b7s-drift-cache"
    await _project(client, slug)
    event_type_id = uuid.UUID(await _event_type(client, slug))

    async with TestSessionLocal() as session:
        drift = SchemaDrift(
            event_type_id=event_type_id,
            scan_config_id=None,
            field_name="platform",
            drift_type="new_field",
            observed_type="String",
            status=SCHEMA_DRIFT_STATUS_OPEN,
        )
        session.add(drift)
        await session.commit()
        drift_id = drift.id

    dropped = _record_dropped_prefixes(monkeypatch)
    applied = await client.post(
        f"/api/v1/projects/{slug}/event-types/drifts/{drift_id}/actions",
        json={"action": "accept"},
    )
    assert applied.status_code == 200, applied.text

    assert cache.prefix_event_types(await project_id_by_slug(slug)) in dropped, dropped

    # Not vacuous: the accept really did change what that list serves.
    async with TestSessionLocal() as session:
        names = await session.scalars(
            select(FieldDefinition.name).where(FieldDefinition.event_type_id == event_type_id)
        )
        assert "platform" in set(names)


# --- the demo trail -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_demo_trail_matches_the_routes_it_imitates(client: AsyncClient) -> None:
    """Three reverts redden this, one per assertion block:

    * putting ``target_type="field"`` and the qualified ``"<type>.<field>"`` key
      back in ``demo/builders/audit._plan_entries`` — a shape
      ``api/v1/fields.py`` has never written;
    * restating the literal ``"Demo warehouse"`` instead of reading
      ``DataSource.name``, which the warehouse builder sets to
      ``demo_data_source_name(slug)``;
    * dropping the ``created`` rows ``demo/builders/activity`` now seeds, which
      left fifteen of the demo's eighteen events opening on an empty History tab.
    """
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    async def trail(action: str) -> list[dict[str, Any]]:
        """Every row this project filed under one action.

        ``GET /audit`` caps ``limit`` at 200 (``api/v1/audit.py``) and the demo
        trail has no fixed length, so fetching "the trail" and filtering here
        would turn each block below into a claim about whichever rows one page
        held. ``action`` is an equality filter in ``audit_service.list_entries``,
        so a call returns that action's rows and nothing else; asserting the
        page against ``total`` — the count of the same filtered set — makes "I
        saw all of them" part of the test rather than an assumption about how
        big the demo happens to be.
        """
        listed = await client.get(f"/api/v1/audit?project_slug={slug}&action={action}&limit=200")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert len(body["items"]) == body["total"], body["total"]
        return body["items"]

    fields = await trail("field.create")
    assert fields, "the demo authored fields and filed nothing for them"
    assert {entry["target_type"] for entry in fields} == {"field_definition"}
    # The BARE name, the way the route writes it: ctx.field_ids is keyed
    # "<event type>.<field>" for the builders' own lookups, not for display.
    assert all("." not in entry["target_name"] for entry in fields), fields

    unscoped = await client.get("/api/v1/audit?action=data_source.create&limit=200")
    warehouses = [
        entry
        for entry in unscoped.json()["items"]
        if entry["target_name"] == demo_data_source_name(slug)
    ]
    assert len(warehouses) == 1, unscoped.json()["items"]

    # The docstring's "one entry per authored object" is now true of the objects
    # the finding enumerated. One query per action, so a missing one is a route
    # that answered "no rows under this action" — not a row that fell off the
    # end of a page — and the failure names which action is missing.
    for authored in (
        "fact_table.create",
        "metric_definition.create",
        "relation.create",
        "event_type.add_owner",
        "variable.override_set",
        "plan_branch.create",
    ):
        assert await trail(authored), authored

    events = await client.get(f"/api/v1/projects/{slug}/events?limit=100")
    assert events.status_code == 200, events.text
    body = events.json()
    items = body["items"]
    # Every demo event, not "at least N of them": a hard-coded floor either rots
    # when the scenario gains an event or silently stops covering the ones past
    # the page. ``total`` is computed over the same filter, so this says the loop
    # below examined the whole set.
    assert items, "the demo authored no events"
    assert len(items) == body["total"], body["total"]
    for item in items:
        history = await client.get(f"/api/v1/projects/{slug}/events/{item['id']}/history")
        assert history.status_code == 200, history.text
        rows = history.json()
        creations = [row for row in rows if row["field"] == "created"]
        assert len(creations) == 1, (item["name"], rows)
        assert creations[0]["new_value"] == item["name"]

    # One act, one row. Reverting the ``change.field != "created"`` filter in
    # ``audit._event_edit_entries`` reddens this: every event would gain an
    # ``event.update`` at EXACTLY its ``event.create`` instant, for one creation.
    creations = {(entry["target_id"], entry["created_at"]) for entry in await trail("event.create")}
    assert creations
    edits = {(entry["target_id"], entry["created_at"]) for entry in await trail("event.update")}
    # Not vacuous: the demo really does file edits, so an ``event.update`` born
    # at its event's creation instant has somewhere to collide.
    assert edits
    assert not (creations & edits), sorted(creations & edits)


# --- duplicate natural keys on merge ------------------------------------------
#
# Nothing here any more, deliberately. This batch added a merge refusal for two
# rows under one natural key and a test that pinned it; review then removed both.
# A branch is how an analyst CLEANS UP a pair of namesakes — delete both copies,
# author one row in their place — so refusing that merge closed the only door out
# of the state it complained about, and reddened two tests in
# test_event_comment_main_twin that hold exactly that workflow.
#
# What shipped for this finding is the diff-side warning, pinned by
# test_plan_diff_and_snapshots. The merge's own half waits on an
# origin id on branch copies, which is where an earlier fix had already put it: telling
# the two rows apart is the thing no message or gate can substitute for.


# --- the demo trail, continued ------------------------------------------------


@pytest.mark.asyncio
async def test_the_alerting_trail_is_ordered_by_name_not_by_a_uuid_tiebreak(
    client: AsyncClient,
) -> None:
    """Reverting ``_alerting_entries`` to ``order_by(created_at, id)`` reddens
    this EVERY time, which is why the ordering is asserted against rows whose
    ids this test chooses.

    Every row in one seed shares a single server-side ``now()``, so ordering by
    ``created_at`` first falls straight through to the uuid4 tie-break and the
    trail's chronology came out different on every seed — while the comment
    above it claimed the ordering was what made the timestamps reproducible.
    Asserting that over the demo's own rows is a coin toss:
    the demo seeds exactly two destinations and two rules, so a random tie-break
    reproduces the name order about one time in four, per seed. The first half
    below removes the chance by giving the rows ids that sort the OPPOSITE way
    to their names; the second half is the demo, which pins the names the first
    half sorts.
    """
    seeded_at = datetime(2021, 6, 7, 8, 9, 10, tzinfo=UTC)
    # Reverse-sorted against the names, so "by id" and "by name" cannot agree.
    high = uuid.UUID("ffffffff-1111-4111-8111-111111111111")
    low = uuid.UUID("00000000-2222-4222-8222-222222222222")

    slug = "b7s-alerting-order"
    project_id = uuid.UUID(await _project(client, slug))
    async with TestSessionLocal() as session:
        session.add_all(
            [
                AlertDestination(
                    id=high,
                    project_id=project_id,
                    type=AlertDestinationType.demo_sink.value,
                    name=_DEMO_SINK_NAME,
                    created_at=seeded_at,
                ),
                AlertDestination(
                    id=low,
                    project_id=project_id,
                    type=AlertDestinationType.slack.value,
                    name=_DISABLED_EXTERNAL_NAME,
                    created_at=seeded_at,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                AlertRule(
                    id=high,
                    destination_id=high,
                    name=_FIRING_RULE_NAME,
                    created_at=seeded_at,
                ),
                AlertRule(
                    id=low,
                    destination_id=high,
                    name=_HEALTHY_RULE_NAME,
                    created_at=seeded_at,
                ),
            ]
        )
        await session.commit()

    # The two orders this query could come back in, derived from the same rows.
    # They disagree — the ids were chosen so that they would — so the assertion
    # below can only be satisfied by the name one, never by a lucky tie-break.
    destinations = ((high, _DEMO_SINK_NAME), (low, _DISABLED_EXTERNAL_NAME))
    rules = ((high, _FIRING_RULE_NAME), (low, _HEALTHY_RULE_NAME))
    by_name = [name for _, name in sorted(destinations, key=lambda row: row[1])] + [
        name for _, name in sorted(rules, key=lambda row: row[1])
    ]
    by_id = [name for _, name in sorted(destinations)] + [name for _, name in sorted(rules)]
    assert by_name != by_id, by_name

    async with TestSessionLocal() as session:
        entries = await audit_builder._alerting_entries(
            session,
            DemoContext(
                project_id=project_id,
                branch_id=uuid.uuid4(),
                slug=slug,
                now=seeded_at,
            ),
        )
    assert [entry.target_name for entry in entries] == by_name
    assert [entry.action for entry in entries] == [
        "alert_destination.create",
        "alert_destination.create",
        "alert_rule.create",
        "alert_rule.create",
    ]

    # And the names that ordering is about are the demo's own. ``_spread_backwards``
    # gives each entry a distinct timestamp in list order, so re-sorting the
    # trail by ``created_at`` recovers exactly the order ``_alerting_entries``
    # returned.
    # One query per action rather than one page of the whole trail: ``GET
    # /audit`` caps ``limit`` at 200 (``api/v1/audit.py``) and the demo trail has
    # no fixed length, so a page would silently decide how many alerting rows
    # this sees — and an ordering assertion over a PREFIX of the rows is the kind
    # that goes green for the wrong reason. ``action`` is an equality filter in
    # ``audit_service.list_entries``, so each call answers with that action's
    # rows or none, and ``total`` — the count of the same filtered set — is
    # asserted against what came back, so the four names below are the demo's
    # whole alerting trail by construction.
    demo_slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    seeded: list[dict[str, Any]] = []
    for action in ("alert_destination.create", "alert_rule.create"):
        listed = await client.get(
            f"/api/v1/audit?project_slug={demo_slug}&action={action}&limit=200"
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert len(body["items"]) == body["total"], body["total"]
        seeded += body["items"]
    seeded.sort(key=lambda entry: entry["created_at"])
    assert [entry["target_name"] for entry in seeded] == by_name
