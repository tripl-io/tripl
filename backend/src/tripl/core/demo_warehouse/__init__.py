"""The demo warehouse: one deterministic model, served from PostgreSQL or memory.

The plan is authored; observations are scanned. The seeder authors a demo's
tracking plan from :mod:`.roster`; everything observed (metrics, anomalies,
drift, regressions, shadow events) comes from tripl's own pipeline reading this
warehouse. Every demo reads the same warehouse: the model runs on the global
UTC clock (:mod:`.calendar`), never on a demo's id or creation time.

Contract (what the rest of tripl may rely on)
---------------------------------------------
The catalog, from :mod:`.roster` (pure data; names are warehouse values):

* ``event_types()``, ``event_type(name)``, ``events()``, ``event(name)``,
  ``live_events()``, ``events_of_type(name)``, ``variables()``,
  ``unplanned_events()``, plus the story names ``KEPT_EVENT_NAMES``,
  ``KEPT_EVENT_TYPES``, ``SHADOW_CANDIDATE_NAME``, ``DUPLICATE_PAIRS``,
  ``SCHEMA_DRIFT_EVENT``, ``VARIABLE_DRIFT_EVENT``, ``DEAD_EVENT``,
  ``DOCUMENTED_PRODUCTS``, ``UNDOCUMENTED_PRODUCT``.

The calendar, from :mod:`.calendar` (pure functions of UTC instants):

* ``incidents_between(t0, t1) -> list[Incident]`` and ``latest_incident(at)``:
  one incident a day from 13:00 UTC (``Incident.kind/scope/target/start/end/
  multiplier/platform/description``);
* ``promo_windows(t0, t1)``: the weekly promo (``PROMO_EVENT``, ``PROMO_TITLE``);
* ``releases_between(t0, t1)``, ``version_shares(at)``, ``regression_for(index)``,
  ``regressions_by_version(at)``: the release train and what each release breaks;
* ``mix_shift_windows(t0, t1)``: the recurring mix shifts (distribution drift);
* ``unplanned_bursts(t0, t1)``: when the unplanned names fire.

The tables, from :mod:`.schema`: ``EVENTS_SCAN_SQL`` (the catalog scan's base
query: low-cardinality columns only), ``ORDERS_SQL``, ``ACTIVE_SESSIONS_SQL``
(the distinct-sessions sql metric both engines answer), ``SCAN_COLUMNS``,
``SUGGESTED_JSON_VALUE_PATHS``, ``TABLE_COLUMNS``.

Engines and status:

* ``build_demo_adapter(*, timeout_seconds=None, now=None) -> BaseAdapter``: the
  adapter a ``db_type='demo'`` source runs on; ``registry.build_adapter(ds)``
  calls it, so callers normally go through the registry;
* ``warehouse_status(*, refresh=False) -> WarehouseStatus``: engine, mode,
  ``written_through``, ``oldest`` retained day, size, retention, last run;
  cached a minute per process;
* ``available_through(now)``: the end of the data the warehouse holds when the
  writer is on schedule (the in-memory engine's horizon).

What a ``demo`` source's scan config should carry: ``base_query =
EVENTS_SCAN_SQL``, ``event_type_column='event_type'``, ``time_column=
'event_time'``, name format ``{event_name}``, ``replay_chunk_interval='1d'``
(the in-memory engine reads at most eight days of events per query).
"""

from __future__ import annotations

from tripl.core.demo_warehouse.calendar import (
    Incident,
    MixShift,
    Regression,
    incidents_between,
    latest_incident,
    mix_shift_windows,
    promo_windows,
    regression_for,
    regressions_by_version,
    releases_between,
    unplanned_bursts,
    version_shares,
)
from tripl.core.demo_warehouse.engine import build_demo_adapter, connection_message, engine_of
from tripl.core.demo_warehouse.roster import (
    DEAD_EVENT,
    DOCUMENTED_PRODUCTS,
    DUPLICATE_PAIRS,
    KEPT_EVENT_NAMES,
    KEPT_EVENT_TYPES,
    SCHEMA_DRIFT_EVENT,
    SHADOW_CANDIDATE_NAME,
    UNDOCUMENTED_PRODUCT,
    VARIABLE_DRIFT_EVENT,
    EventDef,
    EventTypeDef,
    FieldDef,
    PropertyDef,
    UnplannedEventDef,
    VariableDef,
    event,
    event_type,
    event_types,
    events,
    events_of_type,
    live_events,
    unplanned_events,
    variables,
)
from tripl.core.demo_warehouse.rows import MODEL_VERSION, available_through
from tripl.core.demo_warehouse.schema import (
    ACTIVE_SESSIONS_SQL,
    EVENTS_SCAN_SQL,
    ORDERS_SQL,
    SCAN_COLUMNS,
    SUGGESTED_JSON_VALUE_PATHS,
    TABLE_COLUMNS,
)
from tripl.core.demo_warehouse.status import WarehouseStatus, warehouse_status

__all__ = [
    "ACTIVE_SESSIONS_SQL",
    "DEAD_EVENT",
    "DOCUMENTED_PRODUCTS",
    "DUPLICATE_PAIRS",
    "EVENTS_SCAN_SQL",
    "KEPT_EVENT_NAMES",
    "KEPT_EVENT_TYPES",
    "MODEL_VERSION",
    "ORDERS_SQL",
    "SCAN_COLUMNS",
    "SCHEMA_DRIFT_EVENT",
    "SHADOW_CANDIDATE_NAME",
    "SUGGESTED_JSON_VALUE_PATHS",
    "TABLE_COLUMNS",
    "UNDOCUMENTED_PRODUCT",
    "VARIABLE_DRIFT_EVENT",
    "EventDef",
    "EventTypeDef",
    "FieldDef",
    "Incident",
    "MixShift",
    "PropertyDef",
    "Regression",
    "UnplannedEventDef",
    "VariableDef",
    "WarehouseStatus",
    "available_through",
    "build_demo_adapter",
    "connection_message",
    "engine_of",
    "event",
    "event_type",
    "event_types",
    "events",
    "events_of_type",
    "incidents_between",
    "latest_incident",
    "live_events",
    "mix_shift_windows",
    "promo_windows",
    "regression_for",
    "regressions_by_version",
    "releases_between",
    "unplanned_bursts",
    "unplanned_events",
    "variables",
    "version_shares",
    "warehouse_status",
]
