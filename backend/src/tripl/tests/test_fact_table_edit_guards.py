"""What a fact-table edit is allowed to refuse, and how it says so.

Three defects in the referential guard on ``update_fact_table``. Each
is pinned by an assertion that goes red if the production change is reverted:

* the guard's filter predicate unioned BOTH ratio operands, so a
  cross-table ratio metric made every table that happened to define a filter of
  the same name un-editable. Filters are named by convention ('exclude_internal'
  on each table), so this is the ordinary case: dropping the filter from the
  table nobody filtered on was refused, citing a metric whose only reference to
  that name lives on the OTHER operand's table. The operator cannot act on that
  refusal — "Edit the metric first" does not help, because the metric is not
  doing anything wrong.
* the guard raised on the FIRST removed filter that blocked, so
  pruning three filters in one form save cost three edit/save/409 round trips to
  learn three facts the server had already computed in one request. Worse, each
  message reads as though the filter it names were the only obstruction.
* ``fact_table_dependents``'s docstring claimed to hold the ONE
  definition of "this metric would break if that fact table changed", but
  ``FactTableUpdate.columns`` is patchable and was not consulted: re-previewing a
  fact table against a query that no longer projects ``amount`` silently stranded
  every metric aggregating it, which then failed in a Celery worker on a metric
  the user never touched. Closed by guarding the column door rather than by
  softening the prose, so the docstring's claim is now true.

Nothing here changes what a fact table IS, only which edits to one are refused
and what the refusal says.
"""

import uuid

import pytest
from httpx import AsyncClient, Response

from tripl.models.metric_definition import MetricDefinition
from tripl.services.fact_table_dependents import (
    metric_named_filters,
    metric_used_columns,
    metrics_needing_column,
    metrics_needing_filter,
)


def _fact_tables_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/fact-tables"


def _metrics_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/metrics"


# ── Seeding helpers ──────────────────────────────────────────────────────────
#
# ``projects.slug`` is globally unique and the test engine is shared across the
# module, so every test passes its own ``suffix``; calling these twice with the
# same one is an IntegrityError, not a test failure anyone enjoys reading.

_DEFAULT_COLUMNS = [
    {"name": "created_at", "type": "timestamp"},
    {"name": "amount", "type": "number"},
    {"name": "user_id", "type": "string"},
    {"name": "region", "type": "string"},
]


async def _create_project(client: AsyncClient, suffix: str) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={
            "name": f"Batch6 Facts {suffix}",
            "slug": f"batch6-facts-{suffix}",
            "description": "",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_fact_table(
    client: AsyncClient,
    slug: str,
    *,
    name: str,
    row_filters: list[dict[str, str]] | None = None,
    columns: list[dict[str, str]] | None = None,
) -> dict:
    resp = await client.post(
        _fact_tables_url(slug),
        json={
            "name": name,
            "display_name": name,
            "sql": "SELECT created_at, amount, user_id, region FROM orders",
            "timestamp_column": "created_at",
            "columns": columns if columns is not None else _DEFAULT_COLUMNS,
            "identifier_columns": ["user_id"],
            "row_filters": row_filters if row_filters is not None else [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_fact_metric(
    client: AsyncClient, slug: str, *, name: str, **config: object
) -> dict:
    payload: dict[str, object] = {
        "kind": "fact",
        "name": name,
        "display_name": name,
        "status": "active",
        "interval": "1d",
        **config,
    }
    payload.setdefault("composition", "single")
    resp = await client.post(_metrics_url(slug), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _patch_fact_table(
    client: AsyncClient, slug: str, fact_table_id: str, body: dict
) -> Response:
    return await client.patch(f"{_fact_tables_url(slug)}/{fact_table_id}", json=body)


# ── the predicate is scoped to the table being edited ────────


async def test_dropping_a_filter_the_ratio_uses_on_its_OTHER_table_is_allowed(
    client: AsyncClient,
) -> None:
    """The false refusal: both tables define 'exclude_internal', one metric uses one.

    Reverting the scoping makes ``metric_named_filters`` union both operands, the
    PATCH becomes a 409, and this test goes red on the status assertion.
    """
    project = await _create_project(client, "scope")
    orders = await _create_fact_table(
        client,
        project["slug"],
        name="orders_scope",
        row_filters=[{"name": "exclude_internal", "sql": "is_internal = 0"}],
    )
    sessions = await _create_fact_table(
        client,
        project["slug"],
        name="sessions_scope",
        row_filters=[{"name": "exclude_internal", "sql": "is_internal = 0"}],
    )
    await _create_fact_metric(
        client,
        project["slug"],
        name="orders_per_session",
        composition="ratio",
        numerator={
            "fact_table_id": orders["id"],
            "aggregation": "count",
            "row_filters": ["exclude_internal"],
        },
        denominator={"fact_table_id": sessions["id"], "aggregation": "count"},
    )

    # SESSIONS' copy of the name is used by nobody: the ratio filters only on the
    # numerator's table.
    dropped = await _patch_fact_table(client, project["slug"], sessions["id"], {"row_filters": []})

    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["row_filters"] == []


async def test_dropping_the_filter_the_ratio_really_uses_is_still_refused(
    client: AsyncClient,
) -> None:
    """The control that stops the scoping from being written as "never 409".

    Same two tables, same metric — this time the edit hits the operand that
    actually names the filter.
    """
    project = await _create_project(client, "scope-control")
    orders = await _create_fact_table(
        client,
        project["slug"],
        name="orders_scope_control",
        row_filters=[{"name": "exclude_internal", "sql": "is_internal = 0"}],
    )
    sessions = await _create_fact_table(
        client,
        project["slug"],
        name="sessions_scope_control",
        row_filters=[{"name": "exclude_internal", "sql": "is_internal = 0"}],
    )
    await _create_fact_metric(
        client,
        project["slug"],
        name="guarded_ratio",
        composition="ratio",
        numerator={
            "fact_table_id": orders["id"],
            "aggregation": "count",
            "row_filters": ["exclude_internal"],
        },
        denominator={"fact_table_id": sessions["id"], "aggregation": "count"},
    )

    refused = await _patch_fact_table(client, project["slug"], orders["id"], {"row_filters": []})

    assert refused.status_code == 409, refused.text
    assert "guarded_ratio" in refused.json()["detail"]
    fetched = await client.get(f"{_fact_tables_url(project['slug'])}/{orders['id']}")
    assert [row["name"] for row in fetched.json()["row_filters"]] == ["exclude_internal"]


def test_metric_named_filters_attributes_each_name_to_its_own_operands_table() -> None:
    numerator_table = uuid.uuid4()
    denominator_table = uuid.uuid4()
    metric = MetricDefinition(
        name="ratio",
        fact_table_id=numerator_table,
        config={
            "numerator": {"fact_table_id": str(numerator_table), "row_filters": ["paid"]},
            "denominator": {"fact_table_id": str(denominator_table), "row_filters": ["eu"]},
        },
    )

    assert metric_named_filters(metric, fact_table_id=numerator_table) == {"paid"}
    assert metric_named_filters(metric, fact_table_id=denominator_table) == {"eu"}
    # Unscoped stays the union: that is a different question, and the
    # callers that ask it are not deciding whether ONE table's edit is safe.
    assert metric_named_filters(metric) == {"paid", "eu"}
    assert metrics_needing_filter([metric], "paid", fact_table_id=denominator_table) == []
    assert metrics_needing_filter([metric], "paid", fact_table_id=numerator_table) == [metric]


def test_an_operand_that_does_not_say_which_table_it_reads_stays_guarded() -> None:
    """Scoping must narrow a KNOWN mismatch, never quietly unguard a corrupt row.

    An operand with no ``fact_table_id``, or an unparseable one, cannot be
    attributed — so it is matched against every table rather than none.
    """
    table = uuid.uuid4()
    headless = MetricDefinition(name="headless", config={"numerator": {"row_filters": ["paid"]}})
    corrupt = MetricDefinition(
        name="corrupt",
        config={"numerator": {"fact_table_id": "not-a-uuid", "row_filters": ["paid"]}},
    )

    assert metrics_needing_filter([headless, corrupt], "paid", fact_table_id=table) == [
        headless,
        corrupt,
    ]


# ── every blocked removal in ONE refusal ─────────────────────


async def test_pruning_three_used_filters_reports_all_three_in_one_refusal(
    client: AsyncClient,
) -> None:
    """Reverting to the raise-on-first loop names only 'a', and 'b'/'c' go missing.

    The lead sentence is asserted verbatim because the round-trip defect was
    invisible in the body: a message naming one filter reads exactly like a
    message about the only problem.
    """
    project = await _create_project(client, "multi")
    fact_table = await _create_fact_table(
        client,
        project["slug"],
        name="pruned_ft",
        row_filters=[
            {"name": "a", "sql": "amount > 0"},
            {"name": "b", "sql": "amount > 1"},
            {"name": "c", "sql": "amount > 2"},
        ],
    )
    for filter_name in ("a", "b", "c"):
        await _create_fact_metric(
            client,
            project["slug"],
            name=f"metric_{filter_name}",
            fact_table_id=fact_table["id"],
            aggregation="count",
            row_filters=[filter_name],
        )

    refused = await _patch_fact_table(
        client, project["slug"], fact_table["id"], {"row_filters": []}
    )

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail.startswith("Cannot remove or rename the row filters 'a', 'b', 'c'.")
    assert "Those row filters are used by 3 metrics" in detail
    for filter_name in ("a", "b", "c"):
        assert f"'metric_{filter_name}'" in detail
    assert detail.endswith("Edit those metrics first, then change those filters.")


async def test_a_single_blocked_filter_still_reads_in_the_singular(
    client: AsyncClient,
) -> None:
    """The plural rewrite must not leave "the row filters 'a'" / "1 metrics"."""
    project = await _create_project(client, "single")
    fact_table = await _create_fact_table(
        client,
        project["slug"],
        name="single_ft",
        row_filters=[
            {"name": "a", "sql": "amount > 0"},
            {"name": "unused", "sql": "amount > 9"},
        ],
    )
    await _create_fact_metric(
        client,
        project["slug"],
        name="only_blocker",
        fact_table_id=fact_table["id"],
        aggregation="count",
        row_filters=["a"],
    )

    # Both filters are dropped; only one of them blocks, so only one is named.
    refused = await _patch_fact_table(
        client, project["slug"], fact_table["id"], {"row_filters": []}
    )

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail.startswith("Cannot remove or rename the row filter 'a'.")
    assert "unused" not in detail
    assert "That row filter is used by 1 metric: 'only_blocker'." in detail
    assert detail.endswith("Edit the metric first, then change the filter.")


# ── the column door, the one the module said it covered ──────


async def test_dropping_a_column_a_metric_aggregates_is_refused(
    client: AsyncClient,
) -> None:
    """A re-preview that stops projecting ``amount`` must not strand sum(amount).

    Reverting the ``columns`` branch makes this PATCH a 200 and the metric fails
    in a Celery worker instead — the exact failure the guard module exists to
    prevent.
    """
    project = await _create_project(client, "columns")
    fact_table = await _create_fact_table(client, project["slug"], name="columns_ft")
    await _create_fact_metric(
        client,
        project["slug"],
        name="revenue",
        fact_table_id=fact_table["id"],
        aggregation="sum",
        measure_column="amount",
    )

    refused = await _patch_fact_table(
        client,
        project["slug"],
        fact_table["id"],
        {
            "columns": [
                {"name": "created_at", "type": "timestamp"},
                {"name": "user_id", "type": "string"},
                {"name": "region", "type": "string"},
            ]
        },
    )

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail.startswith("Cannot remove or rename the column 'amount'.")
    assert "That column is used by 1 metric: 'revenue'." in detail

    # A refusal, not a warning: the stored column list is untouched.
    fetched = await client.get(f"{_fact_tables_url(project['slug'])}/{fact_table['id']}")
    assert [column["name"] for column in fetched.json()["columns"]] == [
        "created_at",
        "amount",
        "user_id",
        "region",
    ]


async def test_dropping_a_column_no_metric_reads_still_saves(client: AsyncClient) -> None:
    """The control that stops the column guard from being written as "always 409".

    Re-introspection legitimately changes the column list; only the names a saved
    metric still points at may block it.
    """
    project = await _create_project(client, "columns-control")
    fact_table = await _create_fact_table(client, project["slug"], name="columns_control_ft")
    await _create_fact_metric(
        client,
        project["slug"],
        name="revenue_control",
        fact_table_id=fact_table["id"],
        aggregation="sum",
        measure_column="amount",
    )

    saved = await _patch_fact_table(
        client,
        project["slug"],
        fact_table["id"],
        {
            "columns": [
                {"name": "created_at", "type": "timestamp"},
                {"name": "amount", "type": "number"},
                {"name": "user_id", "type": "string"},
                {"name": "currency", "type": "string"},
            ]
        },
    )

    assert saved.status_code == 200, saved.text
    assert [column["name"] for column in saved.json()["columns"]] == [
        "created_at",
        "amount",
        "user_id",
        "currency",
    ]


@pytest.mark.parametrize(
    ("body_key", "body_value", "expected_lead"),
    [
        ("row_filters", [], "Cannot remove or rename the row filter 'paid'."),
        (
            "columns",
            [{"name": "created_at", "type": "timestamp"}],
            # ``user_id`` is dropped by the same payload and is NOT named: it is an
            # identifier column of the table, but no metric points at it.
            "Cannot remove or rename the columns 'amount', 'region'.",
        ),
        ("data_source_id", None, "Cannot unbind this fact table's data source."),
    ],
)
async def test_every_referential_door_the_module_claims_to_cover_is_closed(
    client: AsyncClient,
    body_key: str,
    body_value: object,
    expected_lead: str,
) -> None:
    """``fact_table_dependents``'s docstring names three update doors; all three 409.

    This is the assertion that keeps the docstring honest. The ``columns`` row is
    the one the docstring made a false claim about, so reverting the column branch
    turns that parametrisation red.
    """
    project = await _create_project(client, f"doors-{body_key.replace('_', '-')}")
    fact_table = await _create_fact_table(
        client,
        project["slug"],
        name=f"doors_{body_key}_ft",
        row_filters=[{"name": "paid", "sql": "amount > 0"}],
    )
    await _create_fact_metric(
        client,
        project["slug"],
        name=f"door_blocker_{body_key}",
        fact_table_id=fact_table["id"],
        aggregation="sum",
        measure_column="amount",
        row_filters=["paid"],
        breakdown_columns=["region"],
    )

    refused = await _patch_fact_table(
        client, project["slug"], fact_table["id"], {body_key: body_value}
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"].startswith(expected_lead)


def test_metric_used_columns_covers_measure_condition_and_breakdown_columns() -> None:
    """Every column name the collector resolves, from both of the places they live.

    ``measure_column`` / ``distinct_column`` / ``conditions[].column`` are in the
    operand config; the breakdown dimensions are columns on the metric row. Miss
    either half and the guard lets half the strandings through.
    """
    table = uuid.uuid4()
    metric = MetricDefinition(
        name="wide",
        fact_table_id=table,
        config={
            "measure_column": "amount",
            "distinct_column": "user_id",
            "conditions": [{"column": "status", "operator": "eq", "value": "paid"}],
        },
        breakdown_columns=["region"],
        app_version_column="app_version",
        platform_column="platform",
    )

    assert metric_used_columns(metric, fact_table_id=table) == {
        "amount",
        "user_id",
        "status",
        "region",
        "app_version",
        "platform",
    }
    assert metrics_needing_column([metric], "status", fact_table_id=table) == [metric]
    assert metrics_needing_column([metric], "untouched", fact_table_id=table) == []


def test_metric_used_columns_does_not_charge_one_operands_column_to_the_other_table() -> None:
    """The per-table scoping applies to columns too: 'amount' on ORDERS is not on SESSIONS."""
    orders = uuid.uuid4()
    sessions = uuid.uuid4()
    metric = MetricDefinition(
        name="ratio",
        fact_table_id=orders,
        config={
            "numerator": {"fact_table_id": str(orders), "measure_column": "amount"},
            "denominator": {"fact_table_id": str(sessions), "measure_column": "sessions"},
        },
    )

    assert metric_used_columns(metric, fact_table_id=orders) == {"amount"}
    assert metric_used_columns(metric, fact_table_id=sessions) == {"sessions"}
    assert metrics_needing_column([metric], "amount", fact_table_id=sessions) == []
