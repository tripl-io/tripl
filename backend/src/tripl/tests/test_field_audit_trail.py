"""Fields and their audit trail.

Both findings have their DEFECT outside this file's subject, in another service
each — ``schema_drift_service`` for the cache bust and ``demo/builders/audit``
for the audit trail. A test does not have to live beside the code it pins,
so the two REGRESSION tests below drive those two fixes through the API and
name the exact edit that reddens them:

* ``schema_drift_service.apply_drift_action`` guarded its
  event-type cache bust on ``event_type.branch_id is None``, a NOT NULL column
  since the branches migration, so accepting a drift never busted the 300 s
  ``GET /event-types`` cache although it had just added, retyped or deleted a
  FieldDefinition. The fix reads the row's own branch the way
  ``field_service._on_main`` does. Both arms are pinned in one test, because
  one arm alone is not the spec: an unconditional bust satisfies the main arm,
  and the guard as written satisfies the branch arm.

* the demo audit builder claimed "one entry per authored
  object" while the relation, the event-type owner grant and the variable
  override had nothing in the trail, and filed the rows it did write in shapes
  the real routes have never used. ``test_reindex_identity_map.py`` pins that those
  action strings now EXIST; what is pinned here is their SHAPE, which nothing
  else looks at.

The three tests after those two are REFERENCE, not regression: they assert what
``field_service`` and ``api/v1/fields.py`` already did before this change and
still do after it, and they are green either way. They are kept because the
cache-bust fix is written as "mirror ``field_service._on_main``", and that
claim is worth something only if the thing being mirrored is itself held still.
Do not count them toward regression coverage.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl import cache
from tripl.models.field_definition import FieldDefinition
from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_OPEN, SchemaDrift
from tripl.tests._project_ids import project_id_by_slug
from tripl.tests.conftest import TestSessionLocal


def _record_dropped_prefixes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every ``cache.delete_prefix`` call. The suite runs without Redis,
    where the real helper is a no-op, so the bust is only observable here."""
    dropped: list[str] = []

    async def record(prefix: str) -> None:
        dropped.append(prefix)

    monkeypatch.setattr(cache, "delete_prefix", record)
    return dropped


async def _project_with_event_type(client: AsyncClient, slug: str) -> str:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "screen_view", "display_name": "Screen view"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _new_field(name: str) -> dict[str, str]:
    return {"name": name, "display_name": name.replace("_", " ").title(), "field_type": "string"}


async def _open_new_field_drift(event_type_id: uuid.UUID, field_name: str) -> uuid.UUID:
    """An open ``new_field`` drift, the accept of which ADDS a FieldDefinition.

    Seeded directly: drift rows are written by the scan worker, and a scan is
    not something an API test can run.
    """
    async with TestSessionLocal() as session:
        drift = SchemaDrift(
            event_type_id=event_type_id,
            scan_config_id=None,
            field_name=field_name,
            drift_type="new_field",
            observed_type="String",
            status=SCHEMA_DRIFT_STATUS_OPEN,
        )
        session.add(drift)
        await session.commit()
        return drift.id


async def _field_names(event_type_id: uuid.UUID) -> set[str]:
    async with TestSessionLocal() as session:
        return set(
            (
                await session.scalars(
                    select(FieldDefinition.name).where(
                        FieldDefinition.event_type_id == event_type_id
                    )
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_the_drift_door_busts_mains_event_type_cache_and_only_mains(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting a drift changes the field list ``GET /event-types`` caches for
    300 s, so the main accept must drop ``prefix_event_types`` — and the branch
    accept must not, because a branch write changes no row main's cached list
    serves.

    TWO reverts redden this, one per phase, and neither redden the other:

    * Phase 1 reddens on restoring ``if event_type.branch_id is None:`` in
      ``schema_drift_service.apply_drift_action``. That condition is
      UNREACHABLE — ``event_types.branch_id`` is NOT NULL in the current
      schema — so the bust never happens and
      ``assert cache.prefix_event_types(project_id) in dropped`` fails.
    * Phase 2 reddens on deleting the ``branch is not None and branch.kind ==
      BranchKind.main.value`` guard and busting unconditionally, which is the
      obvious over-correction for phase 1: the branch accept would then drop
      main's list cache and ``assert ... not in dropped`` fails.
    """
    slug = "b7-drift-cache-arms"
    main_event_type_id = uuid.UUID(await _project_with_event_type(client, slug))

    # Phase 1: main.
    drift_id = await _open_new_field_drift(main_event_type_id, "platform")
    dropped = _record_dropped_prefixes(monkeypatch)
    applied = await client.post(
        f"/api/v1/projects/{slug}/event-types/drifts/{drift_id}/actions",
        json={"action": "accept"},
    )
    assert applied.status_code == 200, applied.text
    # Not vacuous: the accept really did change what that list serves.
    assert "platform" in await _field_names(main_event_type_id)
    assert cache.prefix_event_types(await project_id_by_slug(slug)) in dropped, dropped

    # Phase 2: the same accept, on a working branch's own copy of the type.
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    # The branch deep-copies the plan, so its event type is a different row.
    listing = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    assert listing.status_code == 200, listing.text
    branch_event_type_id = uuid.UUID(
        next(et["id"] for et in listing.json() if et["name"] == "screen_view")
    )
    assert branch_event_type_id != main_event_type_id

    branch_drift_id = await _open_new_field_drift(branch_event_type_id, "app_version")
    dropped = _record_dropped_prefixes(monkeypatch)
    applied = await client.post(
        f"/api/v1/projects/{slug}/event-types/drifts/{branch_drift_id}/actions",
        json={"action": "accept"},
    )
    assert applied.status_code == 200, applied.text
    # Same non-vacuity check: the accept did write a field, it just wrote it
    # somewhere main's cached list does not reach.
    assert "app_version" in await _field_names(branch_event_type_id)
    assert "app_version" not in await _field_names(main_event_type_id)
    assert cache.prefix_event_types(await project_id_by_slug(slug)) not in dropped, dropped


@pytest.mark.asyncio
async def test_the_demo_files_its_authored_objects_in_their_routes_own_shape(
    client: AsyncClient,
) -> None:
    """The three plan objects ``_authored_plan_entries`` added to the demo trail
    carry the ``target_type``, ``target_name`` and payload their REAL routes
    record — including the two routes that deliberately file an empty name.

    ``test_reindex_identity_map.py::test_the_demo_trail_matches_the_routes_it_imitates``
    asserts these three action strings now exist at all. Nothing asserts their
    shape, which is the half a wrong builder gets wrong quietly: a row in the
    Audit tab that the product itself cannot produce reads as evidence of a
    feature that does not exist.

    Reverts that redden this, in ``demo/builders/audit._authored_plan_entries``:

    * deleting the helper, or its ``+ await _authored_plan_entries(...)`` in
      ``build_audit`` — all three ``assert len(...) == 1`` fail;
    * filing the EVENT's name on the override row (``event_names.get`` in place
      of ``variable_names.get``), which is the shape the route
      was fixed out of — ``target_name`` becomes "Trial Started" and the
      product_id assertion fails;
    * giving ``relation.create`` or ``event_type.add_owner`` a non-empty
      ``target_name``, or the relation a ``target_type`` other than "relation"
      — ``api/v1/relations.py`` passes no ``target_name`` at all and
      ``api/v1/event_type_owners.py`` passes ``""``.
    """
    created = await client.post("/api/v1/projects/demo")
    assert created.status_code == 202, created.text
    slug = created.json()["slug"]

    types = await client.get(f"/api/v1/projects/{slug}/event-types")
    assert types.status_code == 200, types.text
    type_id_by_name = {et["name"]: uuid.UUID(et["id"]) for et in types.json()}

    async def payload_of(entry: dict[str, Any]) -> dict[str, Any]:
        # The list response carries no payload on purpose (AuditEntryResponse);
        # it travels one row at a time, which is the door the Audit tab uses
        # when a reader expands a row.
        detail = await client.get(f"/api/v1/projects/{slug}/audit/{entry['id']}")
        assert detail.status_code == 200, detail.text
        return detail.json()["payload"]

    async def rows(action: str) -> list[dict[str, Any]]:
        """Every row this project filed under one action.

        Asked for BY action rather than sliced out of the whole trail.
        ``GET /audit`` caps ``limit`` at 200 (``api/v1/audit.py``), and the demo
        trail has no fixed length — it grows with every object a future builder
        authors — so "the demo filed exactly one" and "the page I happened to
        fetch held exactly one" are different claims, and only the first is the
        one these assertions mean. ``action`` is an equality filter in
        ``audit_service.list_entries`` and ``total`` counts the same filtered
        set the page slices, so the assertion below states that this page IS the
        set: a trail that outgrows one page reddens here, loudly, instead of
        quietly narrowing what the counts below are about.
        """
        listed = await client.get(f"/api/v1/projects/{slug}/audit?action={action}&limit=200")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert len(body["items"]) == body["total"], body["total"]
        return body["items"]

    # The relation: the recipe authors exactly one, click -> screen_view.
    relations = await rows("relation.create")
    assert len(relations) == 1, relations
    relation = relations[0]
    assert relation["target_type"] == "relation"
    # A relation has no standalone name, and the route files none.
    assert relation["target_name"] == ""
    relation_payload = await payload_of(relation)
    assert uuid.UUID(relation_payload["source_event_type_id"]) == type_id_by_name["click"]
    assert uuid.UUID(relation_payload["target_event_type_id"]) == type_id_by_name["screen_view"]

    # The owner grant: one, on screen_view, naming the grantee in the payload
    # rather than in the title — which is how api/v1/event_type_owners.py
    # records it, empty target_name and all.
    grants = await rows("event_type.add_owner")
    assert len(grants) == 1, grants
    grant = grants[0]
    assert grant["target_type"] == "event_type"
    assert grant["target_name"] == ""
    assert uuid.UUID(grant["target_id"]) == type_id_by_name["screen_view"]
    grant_payload = await payload_of(grant)
    # The demo's creator granted it to themselves; whoever that is, the row's
    # actor and its payload have to agree, or the grant names nobody real.
    assert uuid.UUID(grant_payload["user_id"]) == uuid.UUID(grant["user_id"])

    # The variable override: the TARGET is the variable, and the event it was
    # scoped to belongs in the payload beside it.
    # The recipe authors two property-list entries: product_id's value override
    # on Trial Started, and platform listed as required (no override) on Home
    # Screen View. Each is one PUT, so each files one row.
    overrides = await rows("variable.override_set")
    assert len(overrides) == 2, overrides
    assert all(row["target_type"] == "variable" for row in overrides), overrides
    by_variable = {row["target_name"]: row for row in overrides}
    assert set(by_variable) == {"product_id", "platform"}, overrides

    override_payload = await payload_of(by_variable["product_id"])
    assert override_payload["event_name"] == "Trial Started"
    assert override_payload["values"] == ["prod_monthly", "prod_annual"]
    assert "required" not in override_payload, override_payload

    # The required-only entry: the route files what its request body set
    # (``model_dump(exclude_unset=True)``), so no ``values`` key at all.
    required_payload = await payload_of(by_variable["platform"])
    assert required_payload["event_name"] == "Home Screen View"
    assert required_payload["required"] is True
    assert "values" not in required_payload, required_payload


# --- reference: the shapes the two fixes above were written to match ----------


@pytest.mark.asyncio
async def test_every_field_write_on_main_busts_the_cached_event_type_list(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REFERENCE, green before and after this change. ``EventTypeResponse``
    carries ``field_definitions`` and the main list is cached for 300 s, so
    every write that changes a field on main has to drop
    ``prefix_event_types``. All five of them do, and that is the behaviour the
    drift door was fixed to join.

    Reddens on removing ``if is_main: await cache.delete_prefix(...)`` from any
    of ``create_field``, ``bulk_create_fields``, ``update_field``,
    ``delete_field`` or ``reorder_fields`` in ``field_service`` — none of which
    this change touches.
    """
    slug = "b7-field-cache-main"
    event_type_id = await _project_with_event_type(client, slug)
    url = f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields"
    dropped = _record_dropped_prefixes(monkeypatch)

    async def write(label: str, method: str, path: str, **kwargs: Any) -> Any:
        dropped.clear()
        resp = await client.request(method, f"{url}{path}", **kwargs)
        assert resp.status_code in (200, 201, 204), f"{label}: {resp.status_code} {resp.text}"
        prefix = cache.prefix_event_types(await project_id_by_slug(slug))
        assert prefix in dropped, f"{label} left the list cache standing"
        return None if resp.status_code == 204 else resp.json()

    field = await write("create", "POST", "", json=_new_field("platform"))
    await write("bulk create", "POST", "/bulk", json={"fields": [_new_field("app_version")]})
    await write("update", "PATCH", f"/{field['id']}", json={"display_name": "Platform name"})
    await write("reorder", "PATCH", "/reorder", json={"field_ids": [field["id"]]})
    await write("delete", "DELETE", f"/{field['id']}")


@pytest.mark.asyncio
async def test_a_field_write_on_a_working_branch_leaves_mains_event_type_cache_alone(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REFERENCE, green before and after this change. The other arm: a branch
    write changes no row main's cached list serves, and ``_on_main`` reads that
    off the event type's own branch rather than off the caller's ``?branch=``
    parameter.

    Reddens on making ``field_service._on_main`` unconditionally true — the
    shape the drift-accept fix must NOT take when it stops keying on
    ``branch_id is None``.
    """
    slug = "b7-field-cache-branch"
    await _project_with_event_type(client, slug)

    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    # The branch deep-copies the plan, so its event type is a different row.
    listing = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    assert listing.status_code == 200, listing.text
    branch_event_type_id = next(et["id"] for et in listing.json() if et["name"] == "screen_view")

    dropped = _record_dropped_prefixes(monkeypatch)
    created = await client.post(
        f"/api/v1/projects/{slug}/event-types/{branch_event_type_id}/fields?branch={branch_id}",
        json=_new_field("platform"),
    )

    assert created.status_code == 201, created.text
    assert cache.prefix_event_types(await project_id_by_slug(slug)) not in dropped


@pytest.mark.asyncio
async def test_field_audit_rows_type_their_target_field_definition_and_name_the_bare_field(
    client: AsyncClient,
) -> None:
    """REFERENCE, green before and after this change. The shape a real field
    write files: ``target_type="field_definition"``, ``target_name`` the field's
    own name — never ``"<event type>.<field>"`` — and ``target_id`` the
    FieldDefinition's id, on the bulk route as well as the single one. This is
    the ground truth the demo audit builder was wrong against.

    Reddens on changing ``target_type`` or ``target_name`` in the ``field.*``
    ``audit_service.record`` calls of ``api/v1/fields.py`` — including to the
    ``("field", "screen_view.platform")`` shape the demo audit builder seeded,
    which is what the audit fix asked the builder to abandon. The builder side
    of that is pinned by ``test_reindex_identity_map.py``.
    """
    slug = "b7-field-audit-shape"
    event_type_id = await _project_with_event_type(client, slug)
    url = f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields"

    single = await client.post(url, json=_new_field("platform"))
    assert single.status_code == 201, single.text
    field_id = single.json()["id"]
    bulk = await client.post(f"{url}/bulk", json={"fields": [_new_field("app_version")]})
    assert bulk.status_code == 201, bulk.text
    patched = await client.patch(f"{url}/{field_id}", json={"display_name": "Platform name"})
    assert patched.status_code == 200, patched.text
    deleted = await client.delete(f"{url}/{field_id}")
    assert deleted.status_code == 204, deleted.text

    audit = await client.get(f"/api/v1/projects/{slug}/audit")
    assert audit.status_code == 200, audit.text
    rows = [entry for entry in audit.json()["items"] if entry["action"].startswith("field.")]

    assert {entry["action"] for entry in rows} == {"field.create", "field.update", "field.delete"}
    assert {entry["target_type"] for entry in rows} == {"field_definition"}
    creates = {entry["target_name"] for entry in rows if entry["action"] == "field.create"}
    # Both routes file the bare name: the qualified key is the builder's invention.
    assert creates == {"platform", "app_version"}
    platform = next(
        entry
        for entry in rows
        if entry["action"] == "field.create" and entry["target_name"] == "platform"
    )
    assert uuid.UUID(platform["target_id"]) == uuid.UUID(field_id)
