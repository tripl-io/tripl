"""The base every per-warehouse connection settings model extends, and the
naming rules the models share."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

# How many extra schemas one schema browse may span, for every warehouse whose
# browse is one ``information_schema`` statement whatever the count (Databricks,
# Snowflake, Trino, Athena): it bounds the IN list and the autocomplete payload,
# not a number of billed jobs. BigQuery's dataset allowlist has a bound of its
# own, because each of its datasets is a billed catalog job.
MAX_SCHEMA_ALLOWLIST = 50


class _ConnectionSettingsBase(BaseModel):
    # The whole point: an unknown key is an error, not a silently stored no-op.
    model_config = ConfigDict(extra="forbid")


def object_name(
    value: str | None, *, pattern: re.Pattern[str], label: str, kind: str
) -> str | None:
    """``value`` trimmed, or None when it is empty.

    Raises ``ValueError`` when it does not match ``pattern``. The message names
    the setting itself ("schema_name 'a.b' is not a valid schema name"), because
    ``data_source._explain`` quotes a validator's message as it is.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not pattern.match(trimmed):
        raise ValueError(f"{label} {trimmed!r} is not a valid {kind}")
    return trimmed


def object_name_list(
    values: list[str] | None,
    *,
    pattern: re.Pattern[str],
    label: str,
    kind: str,
    limit: int,
    too_many: str | None = None,
) -> list[str] | None:
    """An allowlist of names, each checked like :func:`object_name`.

    Blank entries are skipped and repeats dropped before the ``limit`` is
    applied; None when nothing is left. ``too_many`` replaces the default
    overflow message when the limit needs its reason told.
    """
    if values is None:
        return None
    cleaned: list[str] = []
    for raw in values:
        name = object_name(raw, pattern=pattern, label=f"{label} entry", kind=kind)
        if name is not None and name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > limit:
        raise ValueError(too_many or f"{label} accepts at most {limit} schemas")
    return cleaned or None
