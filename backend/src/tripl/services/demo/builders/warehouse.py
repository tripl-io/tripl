"""Warehouse builder: the demo's ``demo`` data source and its catalog scan config.

Authors what a team does when it connects a warehouse: one data source and one
scan config. Nothing is observed here — the source points at the shared demo
warehouse (:mod:`tripl.core.demo_warehouse`), and the provisioning pipeline
(:mod:`tripl.services.demo.pipeline`) runs tripl's real scan and metrics replay
over it. The config is the one the warehouse contract asks for: the scan's base
query, event types grouped by ``event_type``, events named by ``event_name``,
and day-sized replay chunks (the in-memory engine reads at most eight days of
events per query).

The source's host and credential columns are placeholders: the ``demo`` adapter
takes its connection only from settings (``core.demo_warehouse.engine``), so
nothing a user could edit here reaches a connection.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.demo_warehouse import EVENTS_SCAN_SQL, SUGGESTED_JSON_VALUE_PATHS
from tripl.models.data_source import DataSource, DBType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.demo.scenario import DemoContext
from tripl.services.project_service import demo_data_source_name

SCAN_CONFIG_NAME = "Demo scan"
PLATFORM_COLUMN = "platform"
APP_VERSION_COLUMN = "app_version"
#: The JSON column the warehouse's event properties live in.
PROPERTIES_COLUMN = "properties"
#: The properties whose values the scan records (low cardinality, so they name
#: values rather than ids).
JSON_VALUE_PATHS = tuple(f"{PROPERTIES_COLUMN}.{path}" for path in SUGGESTED_JSON_VALUE_PATHS)
#: Distribution drift (PSI) is watched on the user's country, the mix the
#: warehouse shifts on a cycle (``calendar.MIX_SHIFTS``). ``platform`` cannot be
#: one: it is the scan's platform column, collected on its own path
#: (``check_scalar_columns_unreserved``). Each field is a breakdown of every
#: collected row, so a field nothing shifts would only cost the replay time.
DISTRIBUTION_DRIFT_FIELDS = ("country",)
#: How far back the catalog sync of each collection (and the first scan) reads.
#: Short on purpose: a field's observed values come from this window, and an
#: unplanned name the warehouse emits in short bursts should reach the shadow
#: inbox through the replay rather than be adopted into the catalog by the first
#: scan that happens to overlap a burst.
SCAN_LOOKBACK_HOURS = 3


async def build_warehouse(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_data_source(session, ctx)
    await _build_scan_config(session, ctx)


async def _build_data_source(session: AsyncSession, ctx: DemoContext) -> None:
    # Every data-source route is fenced to the request's organization, so a
    # source left in the column default would be invisible to a demo created in
    # any other organization.
    organization_id = await session.scalar(
        select(Project.organization_id).where(Project.id == ctx.project_id)
    )
    data_source = DataSource(
        project_id=ctx.project_id,
        organization_id=organization_id,
        name=demo_data_source_name(ctx.slug),
        db_type=DBType.demo.value,
        host="demo-warehouse",
        port=0,
        database_name="demo",
        username="",
        password_encrypted="",
    )
    session.add(data_source)
    await session.flush()
    ctx.data_source_id = data_source.id


async def _build_scan_config(session: AsyncSession, ctx: DemoContext) -> None:
    assert ctx.data_source_id is not None
    scan_config = ScanConfig(
        data_source_id=ctx.data_source_id,
        project_id=ctx.project_id,
        name=SCAN_CONFIG_NAME,
        # Only the modelled low-cardinality columns: ``user_id``/``session_id``
        # back the distinct-sessions sql metric, not the catalog, and a scan
        # grouping by them would mint a field of raw ids on every event type.
        base_query=EVENTS_SCAN_SQL,
        time_column="event_time",
        event_type_column="event_type",
        event_name_format="{event_name}",
        json_value_paths=list(JSON_VALUE_PATHS),
        distribution_drift_fields=list(DISTRIBUTION_DRIFT_FIELDS),
        metric_breakdown_columns=[],
        interval="1h",
        replay_chunk_interval="1d",
        scan_lookback_hours=SCAN_LOOKBACK_HOURS,
        # The plan is the authority: the warehouse's unplanned names
        # (app_heartbeat_v1's bursts) wait in the reconciliation inbox instead
        # of joining the catalog as in_review events the next collection makes.
        auto_create_events=False,
        anomaly_detection_enabled=True,
        platform_column=PLATFORM_COLUMN,
        app_version_column=APP_VERSION_COLUMN,
    )
    session.add(scan_config)
    await session.flush()
    ctx.scan_config_id = scan_config.id
