"""How a scan names its events: by one Event type, or by the values of one column.

A config can carry both fields: the scan form saves both when an Event type is
chosen after a column, and tells the user the column then names nothing. The
chosen Event type wins: the manual run,
the dry run, the scheduled collection, a replay and every metric-row collector
all ask this module, so none of them can decide the other way. They used to
each decide for themselves, and the scheduled path let the column win, so every
tick filed the volume under event types named after the column's values while a
manual run filed it under the chosen type.

The column is still reserved on the single-type path (``reserved_catalog_columns``
and the ``event_type_column=`` argument to the generators): it never becomes a
catalogued field, and an event name format may still read it.
"""

from __future__ import annotations

from collections.abc import Mapping

from tripl.models.scan_config import ScanConfig


def scan_group_column(config: ScanConfig) -> str | None:
    """The column whose values name this scan's event types, or None.

    None when an Event type is chosen, because that type names every row. A
    config with neither returns None too; the runs that write the catalog refuse
    it with ``NO_EVENT_NAMING_MSG``.
    """
    if config.event_type_id is not None:
        return None
    return config.event_type_column or None


def group_column_index(config: ScanConfig, reg_index: Mapping[str, int]) -> int | None:
    """Where the naming column sits in a metric row, or None when the scan does not group."""
    column = scan_group_column(config)
    return reg_index.get(column) if column is not None else None
