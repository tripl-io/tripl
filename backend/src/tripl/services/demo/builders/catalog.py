"""Metrics-catalog builder: the orders fact table and four metric definitions.

Definitions are built through the authoritative Pydantic create schemas
(``FactTableCreate``, ``SqlMetricCreate``, ``EventCompositionMetricCreate``,
``FactMetricCreate``) so their validators run (SQL safety, identifier allowlist,
fact-composition rules), then persisted as ORM rows via ``to_create_values()``.
The catalog services are not called: each commits internally.

No values are written here. The provisioning pipeline collects every metric
through the worker's own collectors once the event history is in, so a value
on a catalog chart is what the warehouse answers.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.demo_warehouse import ACTIVE_SESSIONS_SQL, ORDERS_SQL
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricStatus,
    ScanInterval,
)
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.schemas.fact_table import (
    FactTableColumnSchema,
    FactTableCreate,
    FactTableRowFilter,
)
from tripl.schemas.metric_definition import (
    EventCompositionMetricCreate,
    FactMetricCreate,
    FactOperand,
    SqlConfig,
    SqlMetricCreate,
)
from tripl.services.demo.scenario import DemoContext

#: The catalog metric the planned promo and the metric breakdown are about.
REVENUE_METRIC_NAME = "revenue_completed"
#: Revenue is split by the buyer's country (a catalog metric breakdown).
REVENUE_BREAKDOWN_COLUMNS = ("country",)


async def build_catalog(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_fact_table(session, ctx)
    await _build_metric_definitions(session, ctx)


async def _build_fact_table(session: AsyncSession, ctx: DemoContext) -> None:
    orders_fact_create = FactTableCreate(
        name="orders",
        display_name="Orders",
        description="One row per store order, used by the revenue fact metrics.",
        color="#0ea5e9",
        data_source_id=ctx.data_source_id,
        sql=ORDERS_SQL,
        timestamp_column="created_at",
        columns=[
            FactTableColumnSchema(name="created_at", type="timestamp"),
            FactTableColumnSchema(name="order_id", type="string"),
            FactTableColumnSchema(name="amount", type="number"),
            FactTableColumnSchema(name="currency", type="string"),
            FactTableColumnSchema(name="user_id", type="string"),
            FactTableColumnSchema(name="country", type="string"),
            FactTableColumnSchema(name="status", type="string"),
            FactTableColumnSchema(name="payment_method", type="string"),
        ],
        identifier_columns=["user_id"],
        row_filters=[FactTableRowFilter(name="completed", sql="status = 'completed'")],
    )
    orders_fact = FactTable(
        project_id=ctx.project_id, order=0, **orders_fact_create.to_create_values()
    )
    session.add(orders_fact)
    await session.flush()
    ctx.fact_table_id = orders_fact.id


async def _build_metric_definitions(session: AsyncSession, ctx: DemoContext) -> None:
    """One sql, one event_composition ratio, two fact (a single and a ratio)."""
    sql_metric_create = SqlMetricCreate(
        name="active_sessions",
        display_name="Active Sessions",
        description="Distinct app sessions per day.",
        color="#6366f1",
        order=0,
        unit="sessions",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        config=SqlConfig(metric_sql=ACTIVE_SESSIONS_SQL, time_column="ts"),
        data_source_id=ctx.data_source_id,
        interval=ScanInterval.d1,
    )
    conversion_metric_create = EventCompositionMetricCreate(
        name="purchase_conversion",
        display_name="Purchase conversion",
        description="Completed purchases per paywall view.",
        color="#22c55e",
        order=1,
        unit="%",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.ratio,
        numerator_event_id=ctx.event_ids["Purchase Completed"],
        denominator_event_id=ctx.event_ids["Paywall View"],
    )
    revenue_metric_create = FactMetricCreate(
        name=REVENUE_METRIC_NAME,
        display_name="Revenue (completed)",
        description="Sum of completed-order amounts per day, by the buyer's country.",
        color="#f59e0b",
        order=2,
        unit="$",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.single,
        interval=ScanInterval.d1,
        fact_table_id=ctx.fact_table_id,
        aggregation=MetricAggregation.sum,
        measure_column="amount",
        row_filters=["completed"],
        breakdown_columns=list(REVENUE_BREAKDOWN_COLUMNS),
    )
    aov_metric_create = FactMetricCreate(
        name="average_order_value",
        display_name="Average order value",
        description="Completed-order revenue divided by completed-order count.",
        color="#a855f7",
        order=3,
        unit="$",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.ratio,
        interval=ScanInterval.d1,
        numerator=FactOperand(
            fact_table_id=ctx.fact_table_id,
            aggregation=MetricAggregation.sum,
            measure_column="amount",
            row_filters=["completed"],
        ),
        denominator=FactOperand(
            fact_table_id=ctx.fact_table_id,
            aggregation=MetricAggregation.count,
            row_filters=["completed"],
        ),
    )

    for metric_create in (
        sql_metric_create,
        conversion_metric_create,
        revenue_metric_create,
        aov_metric_create,
    ):
        metric_def = MetricDefinition(project_id=ctx.project_id, **metric_create.to_create_values())
        session.add(metric_def)
        await session.flush()
        ctx.metric_ids[metric_create.name] = metric_def.id
