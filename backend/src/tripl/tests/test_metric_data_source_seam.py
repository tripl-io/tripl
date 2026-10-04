"""The three defects that span two modules.

* one question, two answers. The ``sql``-metric doors decided
  which data source a project may use by OWNERSHIP; the fact-table doors demanded
  a ``ScanConfig`` in the project. Both are now the ownership rule in
  ``services/data_source_scope``, which WIDENS the fact-table doors: a
  workspace-global warehouse nobody scans is shared, and reachable from every
  project. That is a deliberate decision, and the tests below say so out loud.
* the same misconfiguration answered 400 on the fact-table
  preview door and 404 on the three others, with two different sentences, inside
  one wizard flow. All four now answer 404 with one sentence.
* an explicit ``null`` on a PATCH field whose column is NOT NULL
  reached the database and came back as a blank 500. It is a 422 naming the
  field, on the metric PATCH, the metric bulk-update and the fact-table PATCH.

Each assertion is written so that reverting the production change turns it red;
where that is not obvious from the assertion, the docstring says how.
"""

import uuid

import pytest
from httpx import AsyncClient

from tripl.core.adapters.base import ColumnInfo
from tripl.models.data_source import DataSource
from tripl.models.scan_config import ScanConfig
from tripl.services import fact_table_introspection_service
from tripl.services.data_source_scope import DATA_SOURCE_NOT_AVAILABLE
from tripl.tests.conftest import TestSessionLocal


def _fact_tables_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/fact-tables"


def _metrics_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/metrics"


# ── Seeding helpers ──────────────────────────────────────────────────────────
#
# ``projects.slug`` and ``data_sources.name`` are globally unique, so every
# helper takes a ``suffix``: calling one twice against the same in-memory engine
# without it is an IntegrityError, not a test failure anyone enjoys reading.


async def _create_project(client: AsyncClient, *, suffix: str = "") -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={
            "name": f"Batch6 Seam{suffix}",
            "slug": f"batch6-seam{suffix}",
            "description": "",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_data_source(client: AsyncClient, *, suffix: str = "") -> dict:
    """A workspace-global data source: ``project_id`` NULL, scanned by nobody."""
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Batch6 Seam CH{suffix}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "seam_db",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _scan_with(project: dict, data_source: dict, *, suffix: str = "") -> None:
    """Give the project a ScanConfig on the source — which CLAIMS it.

    On a workspace-global source this is the only evidence of a claim the
    ownership rule reads: scanned by some other project and not by this one means
    "theirs". Seeded directly because the point is the binding, not the
    scan-config API.
    """
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(data_source["id"]),
                name=f"seam-binding{suffix}",
                base_query="SELECT 1",
            )
        )
        await session.commit()


async def _own_data_source(project: dict, data_source: dict) -> None:
    """Stamp ``data_sources.project_id`` — the other, stronger kind of claim."""
    async with TestSessionLocal() as session:
        row = await session.get(DataSource, uuid.UUID(data_source["id"]))
        assert row is not None
        row.project_id = uuid.UUID(project["id"])
        await session.commit()


def _fact_table_payload(name: str, data_source_id: str | None = None) -> dict:
    payload: dict[str, object] = {
        "name": name,
        "display_name": name,
        "sql": "SELECT created_at, amount FROM orders",
        "timestamp_column": "created_at",
        "columns": [
            {"name": "created_at", "type": "timestamp"},
            {"name": "amount", "type": "number"},
        ],
    }
    if data_source_id is not None:
        payload["data_source_id"] = data_source_id
    return payload


async def _create_sql_metric(client: AsyncClient, slug: str, data_source_id: str) -> dict:
    resp = await client.post(
        _metrics_url(slug),
        json={
            "kind": "sql",
            "name": "seam_metric",
            "display_name": "Seam metric",
            "data_source_id": data_source_id,
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── one scope rule, four doors ──────────────────────────────


async def test_a_fact_table_may_bind_a_warehouse_nobody_scans(client: AsyncClient) -> None:
    """The widening, stated deliberately: shared means shared.

    A workspace-global data source (``project_id`` NULL) that NO project has a
    ScanConfig for is now bindable from any project. That is what the NULL means,
    and it is the configuration an owner creates for a warehouse that is queried
    but never scanned — which the ``sql``-metric door has always accepted and the
    fact-table door refused, for the same id, in the same workspace.

    RED before the fix: the old rule required a ScanConfig in THIS project, so
    this save answered 404.
    """
    project = await _create_project(client)
    data_source = await _create_data_source(client)

    resp = await client.post(
        _fact_tables_url(project["slug"]),
        json=_fact_table_payload("shared_orders", data_source["id"]),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["data_source_id"] == data_source["id"]


async def test_previewing_a_fact_table_on_a_warehouse_nobody_scans_runs(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview door agrees with the save door it precedes in the wizard.

    The adapter is stubbed because the scope verdict is what is under test, not
    ClickHouse: reaching the stub at all is the proof that the check passed.

    RED before the fix: the preview refused this source with 400 "Data source is
    not available in this project.", so the stub was never reached.
    """
    project = await _create_project(client, suffix="-p")
    data_source = await _create_data_source(client, suffix="-p")

    def _columns(*_args: object, **_kwargs: object) -> list[ColumnInfo]:
        return [ColumnInfo(name="created_at", type_name="DateTime")]

    monkeypatch.setattr(fact_table_introspection_service, "_run_introspection", _columns)

    resp = await client.post(
        f"{_fact_tables_url(project['slug'])}/preview",
        json={"data_source_id": data_source["id"], "sql": "SELECT created_at FROM orders"},
    )

    assert resp.status_code == 200, resp.text
    assert [column["name"] for column in resp.json()["columns"]] == ["created_at"]


async def test_a_fact_table_may_not_bind_a_warehouse_another_project_scans(
    client: AsyncClient,
) -> None:
    """The negative control: the rule is ownership, not "allow everything".

    Without this, "shared is shared" could be satisfied by deleting the check.
    """
    project_a = await _create_project(client, suffix="-a")
    project_b = await _create_project(client, suffix="-b")
    data_source = await _create_data_source(client, suffix="-a")
    await _scan_with(project_b, data_source)

    resp = await client.post(
        _fact_tables_url(project_a["slug"]),
        json=_fact_table_payload("borrowed_orders", data_source["id"]),
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == DATA_SOURCE_NOT_AVAILABLE


async def test_a_fact_table_may_not_bind_a_warehouse_another_project_owns(
    client: AsyncClient,
) -> None:
    """Ownership is STRICTER than the binding rule in exactly one corner.

    A source stamped with another project's ``project_id`` — the demo case — is
    refused here even though this project scans it, where "has a ScanConfig in
    this project" would have waved it through.

    RED before the fix: the ScanConfig below satisfied the old rule and the save
    returned 201.
    """
    project_a = await _create_project(client, suffix="-o")
    project_b = await _create_project(client, suffix="-o2")
    data_source = await _create_data_source(client, suffix="-o")
    await _own_data_source(project_b, data_source)
    await _scan_with(project_a, data_source, suffix="-o")

    resp = await client.post(
        _fact_tables_url(project_a["slug"]),
        json=_fact_table_payload("demo_orders", data_source["id"]),
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == DATA_SOURCE_NOT_AVAILABLE


async def test_the_metric_door_and_the_fact_table_door_agree_on_one_source(
    client: AsyncClient,
) -> None:
    """The seam itself: both doors, one id, one verdict.

    The finding was not that either answer was wrong on its own but that the two
    disagreed. This drives the same out-of-scope source at the ``sql``-metric
    save and the fact-table save and asserts they answer identically.
    """
    project_a = await _create_project(client, suffix="-agree")
    project_b = await _create_project(client, suffix="-agree2")
    data_source = await _create_data_source(client, suffix="-agree")
    await _scan_with(project_b, data_source, suffix="-agree")

    metric_resp = await client.post(
        _metrics_url(project_a["slug"]),
        json={
            "kind": "sql",
            "name": "cross_project",
            "display_name": "Cross project",
            "data_source_id": data_source["id"],
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        },
    )
    fact_resp = await client.post(
        _fact_tables_url(project_a["slug"]),
        json=_fact_table_payload("cross_project", data_source["id"]),
    )

    assert metric_resp.status_code == fact_resp.status_code == 404
    assert metric_resp.json()["detail"] == fact_resp.json()["detail"] == DATA_SOURCE_NOT_AVAILABLE


# ── one status and one sentence on all four doors ───────────


async def test_the_fact_table_wizard_answers_the_same_way_twice(client: AsyncClient) -> None:
    """Preview then Save on a bad id used to teach the user two different things.

    RED before the fix: the preview answered 400 (``FactTableIntrospectionError``
    was mapped to 400 wholesale) while the save answered 404, so the status
    equality below failed.
    """
    project_a = await _create_project(client, suffix="-w")
    project_b = await _create_project(client, suffix="-w2")
    data_source = await _create_data_source(client, suffix="-w")
    await _scan_with(project_b, data_source, suffix="-w")

    preview = await client.post(
        f"{_fact_tables_url(project_a['slug'])}/preview",
        json={"data_source_id": data_source["id"], "sql": "SELECT created_at FROM orders"},
    )
    save = await client.post(
        _fact_tables_url(project_a["slug"]),
        json=_fact_table_payload("wizard_orders", data_source["id"]),
    )

    assert preview.status_code == save.status_code == 404, preview.text
    assert preview.json()["detail"] == save.json()["detail"] == DATA_SOURCE_NOT_AVAILABLE


async def test_a_data_source_id_that_resolves_to_nothing_answers_the_same(
    client: AsyncClient,
) -> None:
    """A missing row and an out-of-scope row stay indistinguishable, both doors.

    Deliberate: a project-scoped API key cannot read the workspace data-source
    inventory, so for that caller uniform wording is the one thing still
    withheld. This is the assertion that fails if someone "helpfully" splits the
    two branches into distinct messages.
    """
    project = await _create_project(client, suffix="-ghost")
    ghost = str(uuid.uuid4())

    preview = await client.post(
        f"{_fact_tables_url(project['slug'])}/preview",
        json={"data_source_id": ghost, "sql": "SELECT created_at FROM orders"},
    )
    save = await client.post(
        _fact_tables_url(project["slug"]),
        json=_fact_table_payload("ghost_orders", ghost),
    )

    assert preview.status_code == save.status_code == 404
    assert preview.json()["detail"] == save.json()["detail"] == DATA_SOURCE_NOT_AVAILABLE


async def test_a_preview_with_no_data_source_at_all_is_still_a_400(client: AsyncClient) -> None:
    """The 404 is for a SCOPE verdict; a malformed request stays a 400.

    Keeps the ``DataSourceNotAvailableError`` subclass honest — if it were
    widened to every introspection failure, this would flip to 404.
    """
    project = await _create_project(client, suffix="-nods")

    resp = await client.post(
        f"{_fact_tables_url(project['slug'])}/preview",
        json={"data_source_id": None, "sql": "SELECT created_at FROM orders"},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "A data source is required to preview fact-table columns."


# ── explicit null on a NOT NULL field ───────────────────────


@pytest.mark.parametrize(
    "field",
    ["display_name", "description", "color", "order", "status", "reviewed", "breakdown_columns"],
)
async def test_an_explicit_null_on_a_metric_patch_is_a_422(client: AsyncClient, field: str) -> None:
    """Every NOT NULL column reachable from the metric PATCH, one per case.

    RED before the fix: the schema accepted the null, ``update_metric_definition``
    ``setattr``-ed it onto the column, and the commit raised ``IntegrityError``,
    which the unhandled-exception handler rendered as a 500.
    """
    project = await _create_project(client, suffix=f"-null-{field.replace('_', '-')}")
    data_source = await _create_data_source(client, suffix=f"-null-{field}")
    metric = await _create_sql_metric(client, project["slug"], data_source["id"])

    resp = await client.patch(
        f"{_metrics_url(project['slug'])}/{metric['id']}",
        json={field: None},
    )

    assert resp.status_code == 422, resp.text
    assert field in resp.text


async def test_an_explicit_null_owner_id_on_a_metric_patch_still_unassigns(
    client: AsyncClient,
) -> None:
    """The negative control: ``owner_id`` IS nullable and a null means "clear it".

    Without this the fix could be "reject every null", which would break the one
    place the product sends one on purpose.
    """
    project = await _create_project(client, suffix="-owner")
    data_source = await _create_data_source(client, suffix="-owner")
    metric = await _create_sql_metric(client, project["slug"], data_source["id"])

    resp = await client.patch(
        f"{_metrics_url(project['slug'])}/{metric['id']}",
        json={"owner_id": None},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["owner_id"] is None


async def test_an_explicit_null_on_a_bulk_update_is_a_422(client: AsyncClient) -> None:
    """The bulk route feeds ``exclude_unset`` values straight into ``.values()``.

    RED before the fix: ``reviewed=None`` reached the UPDATE and the commit raised.
    """
    project = await _create_project(client, suffix="-bulk")
    data_source = await _create_data_source(client, suffix="-bulk")
    metric = await _create_sql_metric(client, project["slug"], data_source["id"])

    resp = await client.post(
        f"{_metrics_url(project['slug'])}/bulk-update",
        json={"metric_ids": [metric["id"]], "reviewed": None},
    )

    assert resp.status_code == 422, resp.text
    assert "reviewed" in resp.text


async def test_a_bulk_update_that_only_clears_the_owner_still_works(client: AsyncClient) -> None:
    """``owner_id: null`` is how a bulk unassign is spelled — it must survive."""
    project = await _create_project(client, suffix="-bulkowner")
    data_source = await _create_data_source(client, suffix="-bulkowner")
    metric = await _create_sql_metric(client, project["slug"], data_source["id"])

    resp = await client.post(
        f"{_metrics_url(project['slug'])}/bulk-update",
        json={"metric_ids": [metric["id"]], "owner_id": None},
    )

    assert resp.status_code == 204, resp.text


@pytest.mark.parametrize("field", ["sql", "timestamp_column", "columns", "row_filters"])
async def test_an_explicit_null_on_a_fact_table_patch_is_a_422(
    client: AsyncClient, field: str
) -> None:
    """``order`` was guarded and named this exact 500; its neighbours were not.

    RED before the fix: only ``order`` had a null-rejecting validator, so each of
    these set a NOT NULL column to NULL and the commit raised.
    """
    project = await _create_project(client, suffix=f"-ftnull-{field.replace('_', '-')}")
    created = await client.post(
        _fact_tables_url(project["slug"]),
        json=_fact_table_payload(f"ft_{field}"),
    )
    assert created.status_code == 201, created.text

    resp = await client.patch(
        f"{_fact_tables_url(project['slug'])}/{created.json()['id']}",
        json={field: None},
    )

    assert resp.status_code == 422, resp.text
    assert field in resp.text


async def test_an_explicit_null_data_source_id_on_a_fact_table_patch_still_unbinds(
    client: AsyncClient,
) -> None:
    """The negative control: ``data_source_id: null`` is the UNBIND gesture.

    It must reach the service — which has its own referential refusal for a table
    metrics still read — rather than being stopped at the schema as a null.
    """
    project = await _create_project(client, suffix="-unbind")
    data_source = await _create_data_source(client, suffix="-unbind")
    created = await client.post(
        _fact_tables_url(project["slug"]),
        json=_fact_table_payload("unbind_me", data_source["id"]),
    )
    assert created.status_code == 201, created.text

    resp = await client.patch(
        f"{_fact_tables_url(project['slug'])}/{created.json()['id']}",
        json={"data_source_id": None},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data_source_id"] is None
