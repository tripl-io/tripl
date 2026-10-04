"""Batch 5, lane W6-facttables: fact-table authoring, disclosure and project scope.

Six defects, each pinned by an assertion that goes red if the production change is
reverted:

* ``GET /metrics/{id}/generated-sql`` promises "the exact adapter
  SQL used by collection" and then disclosed ``LIMIT 100000`` for a statement the
  collector runs as ``LIMIT 100001``. The ``+ 1`` is the probe row
  ``_reject_truncated_rows`` needs, so the two numbers can never be equal; the
  endpoint has to disclose the executed one.
* a fact-table edit could strand a saved metric: renaming or
  dropping a named row filter, unbinding the data source, or deleting the table
  all left a metric pointing at something that is gone, and the failure only
  appeared later inside a Celery worker. The same walk also 404'd the whole
  ``/generated-sql`` request when ONE operand's fact table was missing.
* both "collect now" surfaces reported the bare bounded manual
  window while the worker widens it to each metric's own resume point. A fresh
  ``1w`` metric was reported as 28 days and scanned as 210.
* a warehouse type name longer than 255 characters raised a
  ``ValidationError`` inside the preview handler and 500'd the whole preview over
  one irrelevant column.
* two row filters could share a name; the collector resolves the
  FIRST match, so one of the two fragments could never run and the save-time
  membership check (a SET of names) could not see the ambiguity.
* Project scoping of a metric's data source (new, out of a
  confirmed sub-claim of an earlier review, and NOT the policy half of that issue):
  both the sql-metric
  PREVIEW and the sql-metric SAVE resolved ``data_source_id`` with no project
  term, so an editor in project A could run — and, worse, schedule — SQL against
  project B's warehouse credential.

Nothing here touches an auth dependency or a role check; the gate question in
that review is the repo owner's and is deliberately untouched.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

import tripl.core.adapters.registry as adapter_registry

# Imported for its side effect and BEFORE any worker task module: that package is
# import-order sensitive (alerts -> celery_app -> metrics -> alerts) and
# celery_app's bottom-of-file registration is what pulls the task modules in an
# order they all survive. Entering any other way hits a partially initialised
# module.
import tripl.worker.celery_app  # noqa: F401
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.schemas.fact_table import (
    NATIVE_TYPE_MAX_LEN,
    FactTableColumnSchema,
    FactTableCreate,
    FactTableRowFilter,
    FactTableUpdate,
)
from tripl.services import metric_preview_service
from tripl.services.fact_table_dependents import (
    fact_table_conflict_detail,
    metric_named_filters,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics import metric_collect


def _fact_tables_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/fact-tables"


def _metrics_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/metrics"


# ── Seeding helpers ──────────────────────────────────────────────────────────
#
# Every helper takes an optional ``suffix`` because ``projects.slug`` and
# ``data_sources.name`` are globally unique: calling one twice against the same
# in-memory engine without it is an IntegrityError, not a test failure anyone
# enjoys reading.


async def _create_project(client: AsyncClient, *, suffix: str = "") -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={
            "name": f"Batch5 Facts{suffix}",
            "slug": f"batch5-facts{suffix}",
            "description": "",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_data_source(client: AsyncClient, *, suffix: str = "") -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Batch5 CH{suffix}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _bind_data_source(project: dict, data_source: dict, *, suffix: str = "") -> None:
    """Make the data source a member of the project the way the app defines it.

    A data source is global; membership is "the project has at least one
    ``ScanConfig`` bound to it". Seeded directly because the point of the test is
    the membership, not the scan-config API.
    """
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(data_source["id"]),
                name=f"batch5-binding{suffix}",
                base_query="SELECT 1",
            )
        )
        await session.commit()


async def _create_fact_table(
    client: AsyncClient,
    slug: str,
    *,
    name: str,
    data_source_id: str | None = None,
    row_filters: list[dict[str, str]] | None = None,
) -> dict:
    payload: dict[str, object] = {
        "name": name,
        "display_name": name,
        "sql": "SELECT created_at, amount, user_id FROM orders",
        "timestamp_column": "created_at",
        "columns": [
            {"name": "created_at", "type": "timestamp"},
            {"name": "amount", "type": "number"},
            {"name": "user_id", "type": "string"},
        ],
        "identifier_columns": ["user_id"],
        "row_filters": row_filters if row_filters is not None else [],
    }
    if data_source_id is not None:
        payload["data_source_id"] = data_source_id
    resp = await client.post(_fact_tables_url(slug), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_fact_metric(
    client: AsyncClient,
    slug: str,
    *,
    name: str,
    fact_table_id: str,
    interval: str = "1w",
    row_filters: list[str] | None = None,
) -> dict:
    payload: dict[str, object] = {
        "kind": "fact",
        "name": name,
        "display_name": name,
        "status": "active",
        "composition": "single",
        "fact_table_id": fact_table_id,
        "aggregation": "count",
        "interval": interval,
    }
    if row_filters is not None:
        payload["row_filters"] = row_filters
    resp = await client.post(_metrics_url(slug), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class _DispatchRecorder:
    """Stands in for ``task.delay`` and records the positional args it was given."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        return type("_Result", (), {"id": self.task_id})()


class _PreviewStubAdapter:
    """Minimal warehouse stub for the sql-metric preview path."""

    def __init__(self, columns: list[str], rows: list[tuple[object, ...]]) -> None:
        self.columns = columns
        self.rows = rows
        self.calls = 0

    def get_preview_rows(
        self, base_query: str, limit: int = 10, **_kwargs: object
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self.calls += 1
        return self.columns, self.rows

    def close(self) -> None:
        return None


# The SQL below is the shape ``test_metrics_catalog_api::TestPreview`` already
# proves clears both the read-only gate and the ClickHouse dialect lint; reusing it
# keeps these tests about the project-scope check and nothing else.
_PREVIEW_SQL = "SELECT toStartOfHour(ts) AS t, count() AS value FROM e GROUP BY t"


# ── an over-long warehouse type name ─────────────────────────


def test_over_long_native_type_is_truncated_from_the_tail() -> None:
    """A labelled Enum8 must not 500 the preview, and its HEAD must survive.

    Truncating from the front would pass a naive length assertion while breaking
    every consumer: ``core.warehouse_types.classify_time`` / ``classify_complex``
    match on the head of the type string.
    """
    long_type = "Enum8(" + "'x' = 1, " * 40 + ")"
    assert len(long_type) > NATIVE_TYPE_MAX_LEN

    column = FactTableColumnSchema(name="status", type="string", native_type=long_type)

    assert column.native_type is not None
    assert len(column.native_type) == NATIVE_TYPE_MAX_LEN
    assert column.native_type.startswith("Enum8(")
    assert column.native_type.endswith("…")


def test_short_native_type_and_absent_native_type_are_untouched() -> None:
    """The negative control: the validator must only fire on an over-long string."""
    short = FactTableColumnSchema(name="a", type="number", native_type="Int64")
    assert short.native_type == "Int64"
    assert FactTableColumnSchema(name="a", type="number").native_type is None


def test_native_type_bound_matches_the_declared_field_constraint() -> None:
    """Widening one without the other would silently reintroduce the 500."""
    declared = [
        getattr(item, "max_length", None)
        for item in FactTableColumnSchema.model_fields["native_type"].metadata
        if getattr(item, "max_length", None) is not None
    ]
    assert declared == [NATIVE_TYPE_MAX_LEN]


# ── duplicate row-filter names ───────────────────────────────


def test_create_rejects_two_row_filters_sharing_a_name() -> None:
    with pytest.raises(ValidationError) as excinfo:
        FactTableCreate(
            name="orders",
            display_name="Orders",
            sql="SELECT created_at, amount FROM orders",
            timestamp_column="created_at",
            row_filters=[
                FactTableRowFilter(name="paid", sql="amount > 0"),
                FactTableRowFilter(name="paid", sql="status = 'paid'"),
            ],
        )
    assert "paid" in str(excinfo.value)


def test_update_rejects_two_row_filters_sharing_a_name() -> None:
    """``FactTableUpdate`` is a separate class and goes unguarded if only create is."""
    with pytest.raises(ValidationError) as excinfo:
        FactTableUpdate(
            row_filters=[
                FactTableRowFilter(name="paid", sql="amount > 0"),
                FactTableRowFilter(name="paid", sql="status = 'paid'"),
            ]
        )
    assert "paid" in str(excinfo.value)


def test_distinct_row_filter_names_still_save() -> None:
    """Negative control: without it the validator could be "reject any row_filters"."""
    created = FactTableCreate(
        name="orders",
        display_name="Orders",
        sql="SELECT created_at, amount FROM orders",
        timestamp_column="created_at",
        row_filters=[
            FactTableRowFilter(name="paid", sql="amount > 0"),
            FactTableRowFilter(name="refunded", sql="status = 'refunded'"),
        ],
    )
    assert [row.name for row in created.row_filters] == ["paid", "refunded"]
    # An absent ``row_filters`` on a PATCH means "leave alone" and must not be
    # treated as an empty list by the validator.
    untouched = FactTableUpdate(display_name="x")
    assert untouched.model_dump(exclude_unset=True) == {"display_name": "x"}


async def test_api_rejects_duplicate_row_filter_names(client: AsyncClient) -> None:
    project = await _create_project(client)
    resp = await client.post(
        _fact_tables_url(project["slug"]),
        json={
            "name": "dup_filters_ft",
            "display_name": "Dup filters",
            "sql": "SELECT created_at, amount FROM orders",
            "timestamp_column": "created_at",
            "row_filters": [
                {"name": "paid", "sql": "amount > 0"},
                {"name": "paid", "sql": "status = 'paid'"},
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "paid" in resp.text


# ── referential guards on fact-table edits ───────────────────


async def test_renaming_a_referenced_row_filter_is_refused_and_nothing_is_written(
    client: AsyncClient,
) -> None:
    project = await _create_project(client)
    fact_table = await _create_fact_table(
        client,
        project["slug"],
        name="guarded_ft",
        row_filters=[{"name": "paid", "sql": "amount > 0"}],
    )
    await _create_fact_metric(
        client,
        project["slug"],
        name="paid_orders",
        fact_table_id=fact_table["id"],
        interval="1d",
        row_filters=["paid"],
    )

    resp = await client.patch(
        f"{_fact_tables_url(project['slug'])}/{fact_table['id']}",
        json={"row_filters": [{"name": "paid_users", "sql": "amount > 0"}]},
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "paid_orders" in detail
    assert "paid" in detail

    # The refusal must be a refusal, not a warning: the stored row is unchanged.
    fetched = await client.get(f"{_fact_tables_url(project['slug'])}/{fact_table['id']}")
    assert fetched.status_code == 200, fetched.text
    assert [row["name"] for row in fetched.json()["row_filters"]] == ["paid"]


async def test_deleting_a_referenced_fact_table_is_refused(client: AsyncClient) -> None:
    project = await _create_project(client)
    fact_table = await _create_fact_table(client, project["slug"], name="guarded_delete_ft")
    await _create_fact_metric(
        client,
        project["slug"],
        name="delete_blocker",
        fact_table_id=fact_table["id"],
        interval="1d",
    )

    resp = await client.delete(f"{_fact_tables_url(project['slug'])}/{fact_table['id']}")

    assert resp.status_code == 409, resp.text
    assert "delete_blocker" in resp.json()["detail"]
    still_there = await client.get(f"{_fact_tables_url(project['slug'])}/{fact_table['id']}")
    assert still_there.status_code == 200, still_there.text


async def test_unbinding_a_referenced_fact_tables_data_source_is_refused(
    client: AsyncClient,
) -> None:
    project = await _create_project(client)
    data_source = await _create_data_source(client)
    await _bind_data_source(project, data_source)
    fact_table = await _create_fact_table(
        client, project["slug"], name="bound_ft", data_source_id=data_source["id"]
    )
    await _create_fact_metric(
        client,
        project["slug"],
        name="unbind_blocker",
        fact_table_id=fact_table["id"],
        interval="1d",
    )

    resp = await client.patch(
        f"{_fact_tables_url(project['slug'])}/{fact_table['id']}",
        json={"data_source_id": None},
    )

    assert resp.status_code == 409, resp.text
    assert "unbind_blocker" in resp.json()["detail"]
    fetched = await client.get(f"{_fact_tables_url(project['slug'])}/{fact_table['id']}")
    assert fetched.json()["data_source_id"] == data_source["id"]


async def test_a_fact_table_with_no_dependent_metrics_still_edits_and_deletes(
    client: AsyncClient,
) -> None:
    """The control that stops the guard from being written as "always 409".

    Same three edits, same shapes, no dependent metric — all three must succeed.
    """
    project = await _create_project(client)
    data_source = await _create_data_source(client)
    await _bind_data_source(project, data_source)
    fact_table = await _create_fact_table(
        client,
        project["slug"],
        name="free_ft",
        data_source_id=data_source["id"],
        row_filters=[{"name": "paid", "sql": "amount > 0"}],
    )
    url = f"{_fact_tables_url(project['slug'])}/{fact_table['id']}"

    renamed = await client.patch(
        url, json={"row_filters": [{"name": "paid_users", "sql": "amount > 0"}]}
    )
    assert renamed.status_code == 200, renamed.text
    assert [row["name"] for row in renamed.json()["row_filters"]] == ["paid_users"]

    unbound = await client.patch(url, json={"data_source_id": None})
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["data_source_id"] is None

    deleted = await client.delete(url)
    assert deleted.status_code == 204, deleted.text


async def test_an_unrelated_patch_is_not_blocked_by_a_dependent_metric(
    client: AsyncClient,
) -> None:
    """A PATCH that never mentions ``row_filters`` must not trip the guard.

    ``exclude_unset`` is the whole mechanism; get this wrong and every cosmetic
    edit on a referenced fact table 409s.
    """
    project = await _create_project(client)
    fact_table = await _create_fact_table(
        client,
        project["slug"],
        name="cosmetic_ft",
        row_filters=[{"name": "paid", "sql": "amount > 0"}],
    )
    await _create_fact_metric(
        client,
        project["slug"],
        name="cosmetic_blocker",
        fact_table_id=fact_table["id"],
        interval="1d",
        row_filters=["paid"],
    )

    resp = await client.patch(
        f"{_fact_tables_url(project['slug'])}/{fact_table['id']}",
        json={"display_name": "Renamed for the UI only"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Renamed for the UI only"


async def test_generated_sql_names_the_broken_metric_instead_of_404ing(
    client: AsyncClient,
) -> None:
    """A dangling ratio operand is a fact about ONE metric, not a missing request.

    The operand's ``fact_table_id`` lives in opaque config JSON with no foreign key
    behind it, so a row written before the delete guard existed can still point at
    a table that is gone. ``get_fact_table`` raises ``HTTPException(404)``, which is
    neither ``ScanError`` nor ``ValueError`` and used to walk straight past the
    handler and 404 the whole ``/generated-sql`` request.
    """
    project = await _create_project(client)
    data_source = await _create_data_source(client)
    await _bind_data_source(project, data_source)
    numerator_table = await _create_fact_table(
        client, project["slug"], name="ratio_num_ft", data_source_id=data_source["id"]
    )
    denominator_table = await _create_fact_table(
        client, project["slug"], name="ratio_den_ft", data_source_id=data_source["id"]
    )
    created = await client.post(
        _metrics_url(project["slug"]),
        json={
            "kind": "fact",
            "name": "dangling_ratio",
            "display_name": "Dangling ratio",
            "status": "active",
            "composition": "ratio",
            "interval": "1d",
            "numerator": {"fact_table_id": numerator_table["id"], "aggregation": "count"},
            "denominator": {"fact_table_id": denominator_table["id"], "aggregation": "count"},
        },
    )
    assert created.status_code == 201, created.text
    metric = created.json()

    # Repoint the DENOMINATOR at a fact table that does not exist, which is exactly
    # the state a pre-guard delete left behind: the operand id is opaque config
    # JSON with no foreign key, so nothing about the delete ever touched it. Done
    # in the database rather than through the API because the save path validates
    # it — the point is a row that was written before anything did.
    async with TestSessionLocal() as session:
        stored = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
        assert stored is not None
        config = dict(stored.config or {})
        denominator = dict(config["denominator"])
        denominator["fact_table_id"] = str(uuid.uuid4())
        config["denominator"] = denominator
        stored.config = config
        await session.commit()

    resp = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}/generated-sql")

    assert resp.status_code == 422, resp.text
    assert "dangling_ratio" in resp.json()["detail"]


# ── what /generated-sql discloses ──────────────────────


async def test_generated_sql_discloses_the_executed_limit_and_window(
    client: AsyncClient,
) -> None:
    """The disclosed statement must be the one that runs, LIMIT and literals alike.

    The LIMIT assertion spells ``+ 1`` rather than naming the new helper on
    purpose: written against ``metric_query_fetch_limit()`` it would pass
    vacuously if both sides ever drifted together.

    ``1w`` is the interval that separates the two window rules. The manual window
    is capped by ``MANUAL_COLLECT_MAX_WINDOW`` at 4 buckets (28 days) while the
    resume fallback for a metric with no stored values is
    ``DEFAULT_COLLECTION_BUCKETS`` = 30 buckets (210 days), and the worker takes
    the earlier of the two. ``1d`` and ``1h`` cannot tell the rules apart, which
    is exactly why the existing collect-now tests pass today.
    """
    project = await _create_project(client)
    data_source = await _create_data_source(client)
    await _bind_data_source(project, data_source)
    fact_table = await _create_fact_table(
        client, project["slug"], name="weekly_ft", data_source_id=data_source["id"]
    )
    metric = await _create_fact_metric(
        client,
        project["slug"],
        name="weekly_orders",
        fact_table_id=fact_table["id"],
        interval="1w",
    )

    resp = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}/generated-sql")

    assert resp.status_code == 200, resp.text
    queries = resp.json()["queries"]
    assert queries, "expected at least one compiled statement"
    executed_limit = metric_collect.METRIC_QUERY_ROW_LIMIT + 1
    for query in queries:
        assert f"LIMIT {executed_limit}" in query["sql"], query["sql"][-120:]

    window_from = min(datetime.fromisoformat(query["window_from"]) for query in queries)
    window_to = max(datetime.fromisoformat(query["window_to"]) for query in queries)
    assert window_to - window_from == timedelta(weeks=30)


async def test_collect_now_reports_the_widened_window_but_dispatches_the_bounded_one(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report widens; the dispatch does not.

    Widening is the WORKER's rule, read against the resume point that is current
    at execution time (``metric_collect._effective_value_window`` and, for the
    batch, ``_run_fact_metrics_batch``'s per-interval
    ``compute_manual_collect_window``). If the handler pre-widened what it
    dispatched, the authority for that rule would have quietly moved into the API.
    The worker half is pinned by
    ``test_fact_metrics_batch.test_manual_batch_keeps_the_backlog_of_a_lagging_swept_in_metric``.
    """
    recorder = _DispatchRecorder("task-fact")
    monkeypatch.setattr(metric_collect.collect_fact_metrics_batch, "delay", recorder)

    project = await _create_project(client)
    fact_table = await _create_fact_table(client, project["slug"], name="weekly_collect_ft")
    metric = await _create_fact_metric(
        client,
        project["slug"],
        name="weekly_collect",
        fact_table_id=fact_table["id"],
        interval="1w",
    )

    resp = await client.post(f"{_metrics_url(project['slug'])}/{metric['id']}/collect")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    reported_from = datetime.fromisoformat(body["window_from"])
    reported_to = datetime.fromisoformat(body["window_to"])
    assert reported_to - reported_from == timedelta(weeks=30)

    assert len(recorder.calls) == 1
    dispatched_from = datetime.fromisoformat(str(recorder.calls[0][1]))
    dispatched_to = datetime.fromisoformat(str(recorder.calls[0][2]))
    assert dispatched_to == reported_to
    assert dispatched_to - dispatched_from == timedelta(days=28)


def test_fetch_limit_follows_a_patched_ceiling() -> None:
    """The two numbers must stay one apart even when a test moves the ceiling.

    A module-level ``METRIC_QUERY_FETCH_LIMIT = METRIC_QUERY_ROW_LIMIT + 1`` would
    be frozen at import and would NOT follow
    ``monkeypatch.setattr(metric_collect, "METRIC_QUERY_ROW_LIMIT", n)``, which is
    what ``test_scans`` and ``test_batch3_c2`` already do.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(metric_collect, "METRIC_QUERY_ROW_LIMIT", 7)
        assert metric_collect.metric_query_fetch_limit() == 8
    assert metric_collect.metric_query_fetch_limit() == metric_collect.METRIC_QUERY_ROW_LIMIT + 1


# ── Project scoping of a metric's data source ────────────────────────────────


async def test_saving_a_sql_metric_against_another_projects_data_source_is_refused(
    client: AsyncClient,
) -> None:
    """The persistent half, and the worse one: the beat then runs it unattended.

    A saved ``sql`` metric carries the ``data_source_id`` its free-text SELECT is
    executed under. With no project term on the check, an editor in project A could
    store a metric against project B's warehouse credential and the five-minute
    catalog beat would run it forever with nobody watching the result.
    """
    project_a = await _create_project(client)
    project_b = await _create_project(client, suffix="-b")
    data_source = await _create_data_source(client)
    await _bind_data_source(project_b, data_source)

    resp = await client.post(
        _metrics_url(project_a["slug"]),
        json={
            "kind": "sql",
            "name": "cross_project_sql",
            "display_name": "Cross project SQL",
            "data_source_id": data_source["id"],
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        },
    )

    assert resp.status_code == 404, resp.text
    # One message for both "no such row" and "out of scope". Not anti-enumeration
    # — ``GET /api/v1/data-sources`` hands a session user the whole inventory
    # anyway — but a project-scoped API key is refused on those slug-less routes,
    # so for THAT caller uniform wording is the one thing still withheld.
    assert resp.json()["detail"] == "Data source is not available in this project."


async def test_repointing_a_sql_metric_at_another_projects_data_source_is_refused(
    client: AsyncClient,
) -> None:
    """The UPDATE arm's refusal, which nothing else asserts.

    ``_apply_definition_update`` re-runs creation's checks, so a PATCH carrying a
    ``definition`` block re-resolves ``data_source_id`` through
    ``load_project_data_source``. Create and preview each had a refusal test; the
    update path had only ``test_editing_a_metric_on_a_shared_warehouse_...``,
    which drives the same PATCH in the ALLOW direction — so removing that call
    from ``_apply_definition_update`` left every existing assertion satisfied.
    The metrics form resubmits the whole ``definition`` on every edit, so this is
    the path a repoint actually takes, and a metric that reached another
    project's credential this way is then run by the catalog beat unattended.

    The source the metric starts on is workspace-global and scanned by nobody,
    which the rule permits: it refuses a source that is identifiably ANOTHER
    project's, not one that lacks a ``ScanConfig`` in this one.
    """
    project_a = await _create_project(client)
    project_b = await _create_project(client, suffix="-b")
    own_source = await _create_data_source(client)
    other_source = await _create_data_source(client, suffix="-b")
    await _bind_data_source(project_b, other_source, suffix="-b")

    created = await client.post(
        _metrics_url(project_a["slug"]),
        json={
            "kind": "sql",
            "name": "repoint_scope_sql",
            "display_name": "Repoint scope SQL",
            "data_source_id": own_source["id"],
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        },
    )
    assert created.status_code == 201, created.text
    metric = created.json()
    assert metric["data_source_id"] == own_source["id"]

    resp = await client.patch(
        f"{_metrics_url(project_a['slug'])}/{metric['id']}",
        json={
            "definition": {
                "kind": "sql",
                "data_source_id": other_source["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            }
        },
    )

    assert resp.status_code == 404, resp.text
    # The same message every branch of the check raises — see the create test.
    assert resp.json()["detail"] == "Data source is not available in this project."

    # And the refusal lands before anything is written: the check runs first in
    # ``_apply_definition_update``, so the stored binding is untouched.
    after = await client.get(f"{_metrics_url(project_a['slug'])}/{metric['id']}")
    assert after.status_code == 200, after.text
    assert after.json()["data_source_id"] == own_source["id"]


async def test_previewing_against_another_projects_data_source_never_reaches_the_warehouse(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = await _create_project(client)
    project_b = await _create_project(client, suffix="-b")
    data_source = await _create_data_source(client)
    await _bind_data_source(project_b, data_source)

    def _never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the warehouse adapter must not be reached")

    monkeypatch.setattr(metric_preview_service, "_run_preview_query", _never)

    resp = await client.post(
        f"{_metrics_url(project_a['slug'])}/preview",
        json={
            "data_source_id": data_source["id"],
            "sql": _PREVIEW_SQL,
            "time_column": "t",
            "interval": "1h",
        },
    )

    # Reverting the scope check makes this a 200 — the module reports warehouse
    # failures as a 200 with ``error`` set, so the status code is the assertion
    # that carries the weight here, not the stub.
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Data source is not available in this project."


async def test_previewing_against_the_projects_own_data_source_still_runs(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the guard must be a project check, not a blanket refusal."""
    project = await _create_project(client)
    data_source = await _create_data_source(client)
    await _bind_data_source(project, data_source)

    bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    adapter = _PreviewStubAdapter(["t", "value"], [(bucket, 1.0)])
    monkeypatch.setattr(adapter_registry, "build_adapter", lambda _ds: adapter)

    resp = await client.post(
        f"{_metrics_url(project['slug'])}/preview",
        json={
            "data_source_id": data_source["id"],
            "sql": _PREVIEW_SQL,
            "time_column": "t",
            "interval": "1h",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["error"] is None
    assert adapter.calls == 1


# ── fact_table_dependents: the shared predicate and the shared sentence ──────


def test_metric_named_filters_reads_both_ratio_operands_and_the_legacy_key() -> None:
    """Reading only ``row_filters`` would let a legacy config's filter be renamed away.

    ``_fact_conditions._effective_filter_names`` folds a legacy single
    ``row_filter`` string into the list, and both ratio operands carry their own
    set, so the guard has to look in all three places.
    """
    metric = MetricDefinition(
        name="ratio",
        config={
            "numerator": {"row_filters": ["paid"]},
            "denominator": {"row_filter": "legacy_eu"},
        },
    )
    assert metric_named_filters(metric) == {"paid", "legacy_eu"}

    single = MetricDefinition(name="single", config={"row_filters": ["paid", "eu"]})
    assert metric_named_filters(single) == {"paid", "eu"}

    assert metric_named_filters(MetricDefinition(name="none", config={})) == set()


def test_conflict_detail_agrees_with_its_own_count() -> None:
    """One sentence has to read correctly at both grammatical numbers.

    "(s)" and a back-reference that disagrees with its own count are both defects
    this repo has already paid for once, in
    ``scan_config_lookup.name_format_conflict_detail``.
    """
    one = fact_table_conflict_detail(
        metrics=[MetricDefinition(name="Revenue")],
        lead="Cannot delete this fact table.",
        reason="This fact table is read by",
        then="delete the fact table",
    )
    assert "read by 1 metric: 'Revenue'." in one
    assert "that metric's collection fails" in one
    assert "Edit the metric first, then delete the fact table." in one

    two = fact_table_conflict_detail(
        metrics=[MetricDefinition(name="Revenue"), MetricDefinition(name="Orders")],
        lead="Cannot delete this fact table.",
        reason="This fact table is read by",
        then="delete the fact table",
    )
    assert "read by 2 metrics: 'Revenue'; 'Orders'." in two
    assert "those metrics' collections fail" in two
    assert "Edit those metrics first" in two


def test_conflict_detail_bounds_how_many_metrics_it_spells_out() -> None:
    """Nothing bounds a project's metric count; the 409 body must not scale with it."""
    metrics = [MetricDefinition(name=f"metric_{index}") for index in range(25)]
    detail = fact_table_conflict_detail(
        metrics=metrics,
        lead="Cannot delete this fact table.",
        reason="This fact table is read by",
        then="delete the fact table",
    )
    assert "read by 25 metrics:" in detail
    assert "metric_0" in detail
    assert "metric_24" not in detail
    assert "and 15 more" in detail


async def test_a_shared_warehouse_no_project_scans_can_back_a_sql_metric(
    client: AsyncClient,
) -> None:
    """The configuration a ScanConfig-binding rule would have locked out.

    A ``sql`` metric needs no scan: ``check_metric_definitions_due`` selects
    active metrics with no ``ScanConfig`` join, so a warehouse a project queries
    ONLY for metrics collects forever without one. Creating a scan config is
    owner-only, so requiring a binding would have meant an editor may use only
    warehouses an owner had already bound — with no other way for an owner to
    bless one. A workspace-global source (``project_id`` NULL) that nobody scans
    is shared by definition, and this is the test that says so.

    Red if the rule goes back to requiring a ScanConfig in this project: the save
    404s with "Data source is not available in this project." on a source the
    owner created for exactly this use.
    """
    project = await _create_project(client)
    data_source = await _create_data_source(client)

    resp = await client.post(
        _metrics_url(project["slug"]),
        json={
            "kind": "sql",
            "name": "metrics_only_warehouse",
            "display_name": "Metrics-only warehouse",
            "data_source_id": data_source["id"],
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["data_source_id"] == data_source["id"]


async def test_editing_a_metric_on_a_shared_warehouse_is_not_refused_by_the_scope_check(
    client: AsyncClient,
) -> None:
    """Editing must not be collateral damage of the scope rule.

    The check runs on EVERY definition update, and the metric form resends the
    stored ``definition`` whatever the user touched — so a rule this metric's own
    data source cannot satisfy makes the metric permanently uneditable, including
    a change of display name alone. That is a silent, total regression for the
    install, which is why it is pinned separately from the create path.
    """
    project = await _create_project(client)
    data_source = await _create_data_source(client)
    created = (
        await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "editable_on_shared",
                "display_name": "Editable",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        )
    ).json()

    resp = await client.patch(
        f"{_metrics_url(project['slug'])}/{created['id']}",
        json={
            "display_name": "Renamed",
            "definition": {
                "kind": "sql",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Renamed"


async def test_a_data_source_owned_by_another_project_is_refused_even_if_nobody_scans_it(
    client: AsyncClient,
) -> None:
    """Ownership is the rule, and ``project_id`` states it outright.

    A non-NULL ``data_sources.project_id`` scopes a source to one project — the
    column exists for generated demo workspaces, so their synthetic warehouse is
    cleaned up with the project instead of leaking a workspace-wide orphan. Such
    a source is another project's whether or not a scan config points at it, so
    the absence of a scan must not make it borrowable.
    """
    project_a = await _create_project(client)
    project_b = await _create_project(client, suffix="-owned")
    data_source = await _create_data_source(client)

    async with TestSessionLocal() as session:
        row = await session.get(DataSource, uuid.UUID(data_source["id"]))
        assert row is not None
        row.project_id = uuid.UUID(project_b["id"])
        await session.commit()

    resp = await client.post(
        _metrics_url(project_a["slug"]),
        json={
            "kind": "sql",
            "name": "borrowed_owned_source",
            "display_name": "Borrowed",
            "data_source_id": data_source["id"],
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        },
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Data source is not available in this project."


async def test_previewing_a_fact_table_leaves_an_audit_row(client: AsyncClient) -> None:
    """The one capability preview held over the saved paths was invisibility.

    Authoring a fact table or a metric writes a row an owner can read back; the
    previews wrote nothing, so an editor could run SQL against a warehouse
    credential and leave no trace. That — not the data access, which the saved
    paths already grant — was the real argument for moving these routes behind
    the owner gate. Recording them is what makes the editor
    boundary defensible, so it is pinned here.

    The row carries the SQL on purpose: a trail that says only "someone
    previewed something" answers none of the questions an owner would ask it.
    """
    project = await _create_project(client)
    data_source = await _create_data_source(client)
    sql = "SELECT created_at, amount FROM orders"

    await client.post(
        f"{_fact_tables_url(project['slug'])}/preview",
        json={"data_source_id": data_source["id"], "sql": sql, "timestamp_column": "created_at"},
    )

    async with TestSessionLocal() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.action == "fact_table.preview")))
            .scalars()
            .all()
        )
    assert len(rows) == 1, "the fact-table preview must leave exactly one audit row"
    assert rows[0].payload is not None
    assert rows[0].payload["sql"] == sql
