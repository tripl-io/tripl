"""The one-step "event + properties" scan setup (F23.4c, #306).

Many warehouses store analytics as one row per event: a column holding the
event's name and a JSON column holding its properties (Segment, RudderStack,
Amplitude and Mixpanel exports, GA4 flattened). The custom setup can express
that, but only by knowing four of its internals: a name format of exactly
``{event}``, no JSON value paths (a listed path becomes a literal value instead
of a property), no Event type column (it would give every event its own event
type) and an event type to file the events under.

The preset is a MAPPING onto those existing fields, not a second pipeline:
:func:`apply_setup_preset` derives them from the two columns the user picked,
so the scan task, the scheduled collection, replay and the dry run run the
single-event-type path they already run, and name events exactly as before. What
the preset adds on top is small and lives in ``worker.utils.scan_preset``: the
run reads only the columns the preset needs (a ``SELECT *`` base query would
otherwise multiply the breakdown by every other column), and it makes sure the
two fields exist on the event type.

``setup_preset``, ``event_name_column`` and ``properties_column`` are stored on
the config so an edit reopens the preset rather than a custom form that happens
to match it.

Pure: imported by the API schemas, the scan service and the worker alike.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Literal

from tripl.core.name_template import format_keys

ScanSetupPreset = Literal["custom", "event_properties"]

SETUP_PRESET_CUSTOM: ScanSetupPreset = "custom"
SETUP_PRESET_EVENT_PROPERTIES: ScanSetupPreset = "event_properties"

# The event type a preset scan files its events under when none was chosen.
# An event type is a folder, and the preset has one folder's worth of events.
PRESET_EVENT_TYPE_NAME = "Events"


def preset_event_name_format(event_name_column: str) -> str:
    """The name format that names each event by its event column alone."""
    return "{" + event_name_column + "}"


def is_event_properties_preset(setup_preset: str | None) -> bool:
    return setup_preset == SETUP_PRESET_EVENT_PROPERTIES


def apply_setup_preset(
    fields: Mapping[str, object],
    *,
    explicit: Collection[str],
) -> dict[str, object]:
    """Check the preset against the config and return the fields it derives.

    ``fields`` is the config as it will be stored (on an update: the saved
    config with the patch merged in). ``explicit`` names the fields the caller
    actually sent: only those can conflict. A value merged in from the saved
    config is the preset's to overwrite, which is what makes switching a custom
    scan to the preset (and back) a one-field edit.

    Raises ``ValueError`` with a message a person can act on; the schemas turn
    it into a 422 as they do for their other cross-field checks.
    """
    preset = fields.get("setup_preset") or SETUP_PRESET_CUSTOM
    event_column = _column(fields.get("event_name_column"))
    properties_column = _column(fields.get("properties_column"))

    if not is_event_properties_preset(str(preset)):
        for key in ("event_name_column", "properties_column"):
            if key in explicit and fields.get(key):
                raise ValueError(f"{key} is only used by the event_properties setup preset")
        return {"event_name_column": None, "properties_column": None}

    if event_column is None:
        raise ValueError("the event_properties setup preset needs event_name_column")
    if properties_column is None:
        raise ValueError("the event_properties setup preset needs properties_column")
    if event_column == properties_column:
        raise ValueError("event_name_column and properties_column must be different columns")
    for role in ("time_column", "app_version_column", "platform_column"):
        if fields.get(role) in (event_column, properties_column):
            raise ValueError(f"event_name_column and properties_column cannot also be the {role}")

    name_format = preset_event_name_format(event_column)
    # The format grammar ends a key at the first ``}``; a column it cannot
    # spell cannot name an event.
    if format_keys(name_format) != [event_column]:
        raise ValueError("event_name_column cannot be used in an event name format")

    if "event_type_column" in explicit and fields.get("event_type_column"):
        raise ValueError(
            "the event_properties setup preset names events from event_name_column; "
            "event_type_column must be empty"
        )
    if "event_name_format" in explicit and fields.get("event_name_format") not in (
        None,
        "",
        name_format,
    ):
        raise ValueError(
            "the event_properties setup preset derives event_name_format from event_name_column"
        )
    if "json_value_paths" in explicit and fields.get("json_value_paths"):
        raise ValueError(
            "the event_properties setup preset catalogues every key of the properties column; "
            "json_value_paths must be empty"
        )
    if "event_group_rules" in explicit and fields.get("event_group_rules"):
        raise ValueError(
            "the event_properties setup preset does not use event group rules; "
            "switch to the custom setup to merge event names"
        )

    return {
        "event_name_column": event_column,
        "properties_column": properties_column,
        "event_type_column": None,
        "event_name_format": name_format,
        "json_value_paths": [],
        "event_group_rules": [],
    }


def _column(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
