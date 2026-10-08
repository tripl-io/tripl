"""Run-time half of the "event + properties" setup preset (F23.4c, #306).

``core.scan_setup_preset`` maps the preset onto the ordinary config fields, so a
preset scan runs the single-event-type path every other scan runs. Two things
can only be settled once the warehouse has answered, and they live here:

* which columns the run reads. The preset needs the event column and the
  properties column, plus the columns other settings name (app version,
  platform, breakdowns, drift fields). Every other column of a ``SELECT *``
  base query is dropped before the breakdown: each one would otherwise be a
  ``GROUP BY`` key that multiplies the rows without adding anything the preset
  catalogues — a user id column alone runs a scan into its row cap;
* that the properties column is JSON, and that the event type carries a field
  for each of the two columns (``ensure_preset_event_type``). Without the
  fields the generator would skip both columns and name nothing.

Shared by the manual run (``tasks.scan``), the scheduled collection
(``tasks.metrics``) and the dry run, which reads the same columns.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import _is_json_type
from tripl.core.scan_setup_preset import PRESET_EVENT_TYPE_NAME, is_event_properties_preset
from tripl.core.warehouse_types import is_string_type
from tripl.models.event_type import EventType
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks._errors import ScanError
from tripl.worker.utils.event_types import ensure_event_type_with_fields
from tripl.worker.utils.reserved_columns import reserved_catalog_columns


def preset_column_problem(config: ScanConfig, columns: list[ColumnInfo]) -> str | None:
    """Why the preset's two columns cannot be scanned as configured, or None.

    Worded for a person: the run raises it as a ``ScanError`` (which
    ``user_facing_error`` shows verbatim) and the dry run lists it as an error.
    """
    if not is_event_properties_preset(config.setup_preset):
        return None
    by_name = {column.name: column for column in columns}
    event_column = config.event_name_column
    properties_column = config.properties_column
    for role, name in (("event", event_column), ("properties", properties_column)):
        if not name or name not in by_name:
            return (
                f"Scan failed: the {role} column {name!r} is not in the base query's "
                "columns. Select it in the query, or pick another column."
            )
    assert properties_column is not None
    column = by_name[properties_column]
    # A String column the config opted into ``json_string_columns`` (F23.9) is
    # already JSON here: ``columns`` comes from the parsed source
    # (``core.json_string_columns``). So what reaches this refusal is text the
    # config did not ask to parse, and the message says how to ask.
    if not _is_json_type(column.type_name):
        hint = (
            " or tick 'Parse as JSON' for it (ClickHouse, BigQuery, Databricks, Snowflake, "
            "Trino and Athena)"
            if is_string_type(column.type_name)
            else ""
        )
        return (
            f"Scan failed: the properties column {properties_column!r} is "
            f"{column.type_name}, not a JSON column, so its keys cannot be read. "
            f"Pick a JSON, Map or struct column{hint}."
        )
    return None


def preset_scan_columns(config: ScanConfig, columns: list[ColumnInfo]) -> list[ColumnInfo]:
    """The columns a run reads: all of them, or the preset's own.

    ``columns`` has the time column removed already, as every caller does.
    Raises ``ScanError`` when the preset's columns are missing or the properties
    column is not JSON.
    """
    if not is_event_properties_preset(config.setup_preset):
        return columns
    problem = preset_column_problem(config, columns)
    if problem is not None:
        raise ScanError(problem)
    kept = {
        config.event_name_column,
        config.properties_column,
        config.app_version_column,
        config.platform_column,
        *(config.metric_breakdown_columns or ()),
        *(config.distribution_drift_fields or ()),
    }
    # Reserved columns are what other settings name (group rules included);
    # the time column is not in ``columns`` by now.
    kept |= reserved_catalog_columns(config)
    return [column for column in columns if column.name in kept]


def ensure_preset_event_type(
    session: Session, config: ScanConfig, columns: list[ColumnInfo]
) -> EventType:
    """The preset's event type, with a field for the event and properties columns.

    The API files a preset config under an event type when it is saved; this
    covers the one it chose having been deleted since (the FK nulls the id), and
    re-points the config at the recreated one so replay finds it too.
    """
    name = PRESET_EVENT_TYPE_NAME
    if config.event_type_id is not None:
        current = session.get(EventType, config.event_type_id)
        if current is not None:
            name = current.name
    wanted = {config.event_name_column, config.properties_column}
    event_type = ensure_event_type_with_fields(
        session,
        config.project_id,
        name,
        [column for column in columns if column.name in wanted],
        set(),
    )
    if config.event_type_id != event_type.id:
        config.event_type_id = event_type.id
    return event_type
