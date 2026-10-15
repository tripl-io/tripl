"""A query that selects one catalog event's rows from the warehouse.

Derived on request and never stored: the governing scan's own source query,
wrapped, filtered to the event's identity. A changed scan rule or a changed
field value changes the next answer, and there is no copy to fall out of step.

Nothing here opens a warehouse connection. That is also why the scan's
``json_string_columns`` wrapper is not applied: resolving it introspects the
warehouse (``core.json_string_columns.resolve_json_string_source``). It only
re-types string columns as JSON, so the rows are the same, and a parsed column
can never be filtered on here anyway (its value is a JSON template).

The one rule every branch below keeps: a part of the identity that cannot be
written as an exact filter is LEFT OUT and named, never approximated. Leaving
a predicate out returns more rows than the event's, which a reader can see;
a wrong predicate returns fewer, silently.

How an event's identity reads, from ``core.analyzers.event_plan``:

* With an ``event_name_format``, the identity is the formatted name, so only
  the format's columns identify it; other field values may differ between rows
  of one event and must not be filtered on.
* Without one, the default name is built from every field, so every field
  value identifies it.
* A value that is exactly one ``${variable}`` token is a high-cardinality
  column: the event takes every value of it, so leaving it out is exact.
* An event a group rule produced covers every row the rule's conditions match;
  its field values hold the rule's ``/pattern/`` literals, and its other values
  came from whichever source row it was minted from. Only the type filter is
  safe, and the conditions are listed for the reader.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.adapters.measure_validator import (
    SqlDialect,
    dialect_for_db_type,
    quote_identifier,
    quote_sql_string_literal,
    quote_timestamp_literal,
)
from tripl.core.name_template import format_keys
from tripl.core.warehouse_types import TimeKind
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import FieldDefinitionType
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.scan_config import ScanConfig
from tripl.schemas.event import EventWarehouseSqlResponse
from tripl.services import event_service
from tripl.services.scan_config_lookup import (
    governing_config,
    load_governing_scan_configs_by_type,
)
from tripl.worker.utils.scan_naming import scan_group_column

ROW_LIMIT = 100
LOOKBACK = timedelta(days=1)

_WHOLE_TOKEN_RE = re.compile(r"^\$\{[^}{]*\}$")
_TOKEN_RE = re.compile(r"\$\{[^}{]*\}")
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_CAST_TO_TEXT = {
    SqlDialect.bigquery: "STRING",
    SqlDialect.trino: "VARCHAR",
    SqlDialect.athena: "VARCHAR",
}

NO_SCAN_REASON = (
    "No scan reads this event type, so there is no warehouse query to start from. "
    "Bind a scan to the event type, or give a scan an event type column."
)


async def get_event_warehouse_sql(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    *,
    now: datetime | None = None,
) -> EventWarehouseSqlResponse:
    event = await event_service.get_event(session, slug, event_id, branch_id, strict_branch=False)
    by_type = await load_governing_scan_configs_by_type(
        session,
        project_id=event.project_id,
        event_type_ids=[event.event_type_id],
        require_name_format=False,
    )
    config = governing_config(by_type.get(event.event_type_id, []))
    if config is None:
        return _no_sql(NO_SCAN_REASON)
    db_type = await session.scalar(
        select(DataSource.db_type).where(DataSource.id == config.data_source_id)
    )
    try:
        dialect = dialect_for_db_type(str(db_type))
    except ValueError:
        return _no_sql(f"The scan's warehouse type ({db_type}) has no SQL dialect here.")
    return build_event_warehouse_sql(config, event, dialect=dialect, now=now or datetime.now(UTC))


def _no_sql(reason: str) -> EventWarehouseSqlResponse:
    return EventWarehouseSqlResponse(
        sql=None,
        dialect=None,
        scan_config_id=None,
        scan_config_name=None,
        exact=False,
        notes=[],
        reason=reason,
    )


def build_event_warehouse_sql(
    config: ScanConfig, event: Event, *, dialect: SqlDialect, now: datetime
) -> EventWarehouseSqlResponse:
    """Pure: the query text for ``event`` under ``config``, in ``dialect``."""
    predicates: list[str] = []
    notes: list[str] = []
    exact = True

    group_column = scan_group_column(config)
    if group_column is not None:
        predicate = _equals(group_column, event.event_type.name, "string", dialect)
        if predicate is None:
            return _no_sql(f"The scan's event type column {group_column!r} cannot be quoted.")
        predicates.append(predicate)

    values = {fv.field_definition.name: fv for fv in event.field_values}
    rule = _matching_group_rule(config.event_group_rules, event)
    if rule is not None:
        exact = False
        notes.append(
            f"Event made by the group rule {rule['name']!r}: it covers every row matching "
            f"{_describe_rule(rule)}. Those conditions are regular expressions and are not "
            "written as filters."
        )
    else:
        name_format = (config.event_name_format or "").strip()
        keys = list(dict.fromkeys(format_keys(name_format))) if name_format else list(values)
        if not name_format and not event.source_name:
            notes.append("This event was not produced by a scan; the filters are its field values.")
        for key in keys:
            ok, predicate, note = _identity_predicate(
                key, values, config=config, group_column=group_column, dialect=dialect
            )
            if predicate is not None:
                predicates.append(predicate)
            if note is not None:
                notes.append(note)
            exact = exact and ok

    if config.time_column:
        try:
            column = quote_identifier(config.time_column, dialect)
        except ValueError:
            notes.append(f"Time column {config.time_column!r} cannot be quoted; no time window.")
        else:
            since = (now - LOOKBACK).replace(second=0, microsecond=0)
            literal = quote_timestamp_literal(since, dialect, kind=TimeKind.timestamp)
            predicates.append(f"{column} >= {literal}")
            if dialect is SqlDialect.bigquery:
                notes.append(
                    "The time window assumes a TIMESTAMP column; for DATETIME or DATE, "
                    "change the literal's type."
                )

    source = config.base_query.strip().rstrip(";").rstrip()
    lines = ["SELECT *", f"FROM (\n{source}\n) AS scan"]
    if predicates:
        lines.append("WHERE " + "\n  AND ".join(predicates))
    lines.append(f"LIMIT {ROW_LIMIT}")
    header = [f"-- {note}" for note in notes if note]
    sql = "\n".join([*_comment_lines(header), *lines])
    return EventWarehouseSqlResponse(
        sql=sql,
        dialect=dialect.value,
        scan_config_id=config.id,
        scan_config_name=config.name,
        exact=exact,
        notes=notes,
        reason=None,
    )


def _comment_lines(lines: Sequence[str]) -> list[str]:
    # A note can carry a user's value; a newline in it would end the comment.
    return [" ".join(line.splitlines()) for line in lines]


def _identity_predicate(
    key: str,
    values: Mapping[str, EventFieldValue],
    *,
    config: ScanConfig,
    group_column: str | None,
    dialect: SqlDialect,
) -> tuple[bool, str | None, str | None]:
    """``(exact, predicate, note)`` for one identifying column."""
    if group_column is not None and key == group_column:
        return True, None, None
    field_value = values.get(key)
    if field_value is None:
        if key == config.event_type_column:
            return False, None, f"{key!r} names the event but its value is not stored."
        if "." in key:
            return False, None, f"{key!r} is a JSON path; it is not written as a filter."
        return False, None, f"{key!r} has no value on this event; it is not filtered on."
    value = field_value.value or ""
    field_type = str(field_value.field_definition.field_type)
    if field_type == FieldDefinitionType.json:
        return False, None, f"{key!r} is a JSON column; it is not written as a filter."
    if "." in key:
        return False, None, f"{key!r} is a dotted column; it is not written as a filter."
    if _WHOLE_TOKEN_RE.match(value):
        return True, None, None
    if value in _rule_pattern_literals(config.event_group_rules):
        return False, None, f"{key!r} holds a group rule pattern ({value}); not filtered on."
    if _TOKEN_RE.search(value):
        return False, None, f"{key!r} mixes text and a variable ({value}); not filtered on."
    predicate = _equals(key, value, field_type, dialect)
    if predicate is None:
        return False, None, f"{key!r} cannot be quoted as a column name; not filtered on."
    return True, predicate, None


def _equals(column: str, value: str, field_type: str, dialect: SqlDialect) -> str | None:
    try:
        quoted = quote_identifier(column, dialect)
    except ValueError:
        return None
    if value == "":
        # The scan writes a NULL as "" (``event_plan._format_value``).
        if field_type in (FieldDefinitionType.number, FieldDefinitionType.boolean):
            return f"{quoted} IS NULL"
        return f"({quoted} IS NULL OR {quoted} = '')"
    if field_type == FieldDefinitionType.boolean and value.lower() in ("true", "false"):
        return f"{quoted} = {value.upper()}"
    if field_type == FieldDefinitionType.number and _NUMERIC_RE.match(value):
        return f"{quoted} = {value}"
    literal = quote_sql_string_literal(value, dialect)
    cast = _CAST_TO_TEXT.get(dialect)
    if cast is not None and _NUMERIC_RE.match(value):
        # Auto-created fields are typed "string" whatever the column is, and
        # these engines refuse to compare a number column with a string.
        return f"CAST({quoted} AS {cast}) = {literal}"
    return f"{quoted} = {literal}"


def _rule_pattern_literals(rules: Sequence[Mapping[str, object]] | None) -> set[str]:
    """The ``/pattern/`` literals a matched group rule writes into field values."""
    literals: set[str] = set()
    for rule in rules or []:
        conditions = rule.get("conditions")
        for condition in conditions if isinstance(conditions, list) else []:
            if isinstance(condition, Mapping) and condition.get("pattern"):
                literals.add(f"/{str(condition['pattern']).strip()}/")
    return literals


def _matching_group_rule(
    rules: Sequence[Mapping[str, object]] | None, event: Event
) -> Mapping[str, object] | None:
    identity = event.source_name or event.name
    for rule in rules or []:
        name = str(rule.get("name", "")).strip()
        if len(name) > 500:
            # The name ``apply_event_group_rules`` gives the grouped event.
            name = name[:497] + "..."
        if name and name == identity:
            return rule
    return None


def _describe_rule(rule: Mapping[str, object]) -> str:
    conditions = rule.get("conditions")
    parts = []
    for condition in conditions if isinstance(conditions, list) else []:
        if isinstance(condition, Mapping):
            parts.append(f"{condition.get('field')} ~ /{condition.get('pattern')}/")
    joiner = " or " if str(rule.get("condition_logic", "all")).lower() == "any" else " and "
    return joiner.join(parts) or "its conditions"
