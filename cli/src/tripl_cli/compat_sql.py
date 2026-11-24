"""Compile one logical event's reviewed mappings to read-only ClickHouse SQL."""

from __future__ import annotations

import re
from datetime import date

_REQUIRED = {"table", "event_column", "build_column", "platform_column", "time_column", "rules"}


def _literal(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or not value.isprintable()
    ):
        raise ValueError("event and platform names must contain 1–255 printable characters")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _identifier(value: object, *, table: bool = False) -> str:
    parts = value.split(".") if isinstance(value, str) else []
    if not 1 <= len(parts) <= (2 if table else 1) or not all(
        re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]{0,126}", part) for part in parts
    ):
        raise ValueError("expected a column identifier or [database.]table")
    return ".".join(f"`{part}`" for part in parts)


def _bound(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("build bounds must be nonnegative signed 64-bit integers")
    return value


def _source(mapping: object, window: tuple[date, date] | None) -> str:
    if not isinstance(mapping, dict) or set(mapping) - {"dedup_column"} != _REQUIRED:
        raise ValueError("expected table, four source columns, rules and optional dedup_column")
    rules = mapping["rules"]
    if not isinstance(rules, list) or not 1 <= len(rules) <= 64:
        raise ValueError("provide between 1 and 64 mapping rules")
    table = _identifier(mapping["table"], table=True)
    name, platform, build, timestamp = (
        f"src.{_identifier(mapping[key + '_column'])}"
        for key in ("event", "platform", "build", "time")
    )
    key = (
        f"nullIf(toString(src.{_identifier(mapping['dedup_column'])}), '')"
        if "dedup_column" in mapping
        else "CAST(NULL AS Nullable(String))"
    )
    names = set()
    matches = []
    active = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {
            "event",
            "platform",
            "min_build",
            "max_build",
        }:
            raise ValueError("a rule requires event, platform, min_build and max_build")
        low, high = _bound(rule["min_build"]), _bound(rule["max_build"])
        if high < low:
            raise ValueError("max_build must be at least min_build")
        physical = _literal(rule["event"])
        names.add(physical)
        scope = (
            f"(platform_name = {_literal(rule['platform'])} AND app_build BETWEEN {low} AND {high})"
        )
        matches.append(f"if({scope} AND physical_name = {physical}, 1, 0)")
        active.append(f"if({scope}, 1, 0)")
    filters = [f"{name} IN ({', '.join(sorted(names))})"]
    if window:
        for operator, day in zip((">=", "<"), window, strict=True):
            filters.append(
                f"{timestamp} {operator} toDateTime64('{day.isoformat()} 00:00:00', 0, 'UTC')"
            )
    where = "\n    AND ".join(filters)
    return f"""WITH source_rows AS (
  SELECT {name} AS physical_name, {platform} AS platform_name,
         accurateCastOrNull({build}, 'Int64') AS app_build, {timestamp} AS occurred_at,
         {key} AS occurrence_key
  FROM {table} AS src
  WHERE {where}
), matched AS (
  SELECT *, {" + ".join(matches)} AS matching_rules,
         {" + ".join(active)} AS active_rules
  FROM source_rows
), classified AS (
  SELECT *, multiIf(matching_rules = 0, 'unmatched',
                    matching_rules > 1 OR (active_rules > 1 AND occurrence_key IS NULL),
                    'ambiguous', 'mapped') AS mapping_status
  FROM matched
)"""


def _classified(mapping: object, window: tuple[date, date] | None) -> str:
    sql = _source(mapping, window)
    if isinstance(mapping, dict) and "dedup_column" in mapping:
        # Isolate matched overlap candidates: unrelated rows cannot consume their
        # first rank. Only that branch is ranked, keeping null-key partitions small.
        return (
            sql
            + """, results AS (
  SELECT *, mapping_status AS status FROM classified
  WHERE NOT (mapping_status = 'mapped' AND active_rules > 1)
  UNION ALL
  SELECT *, if(row_number() OVER (
      PARTITION BY platform_name, app_build, toDate(occurred_at, 'UTC'), occurrence_key
      ORDER BY occurred_at, physical_name
  ) = 1, 'mapped', 'duplicate') AS status FROM classified
  WHERE mapping_status = 'mapped' AND active_rules > 1
)"""
        )
    return sql + ", results AS (SELECT *, mapping_status AS status FROM classified)"


def compile_model(mapping: object, logical_event: str) -> str:
    """One row per accepted logical occurrence. The source table remains untouched."""
    return (
        _classified(mapping, None)
        + f"""
SELECT {_literal(logical_event)} AS logical_event, occurred_at, platform_name, app_build,
       physical_name, occurrence_key
FROM results WHERE status = 'mapped'
"""
    )


def compile_preview(mapping: object, logical_event: str, start: date, end: date) -> str:
    """Reconcile candidate-name rows in a half-open UTC window of up to 31 days."""
    if type(start) is not date or type(end) is not date or not 1 <= (end - start).days <= 31:
        raise ValueError("preview requires a half-open window of 1–31 days")
    return (
        _classified(mapping, (start, end))
        + f"""
SELECT {_literal(logical_event)} AS logical_event, platform_name, app_build,
       count() AS physical_rows, countIf(status = 'mapped') AS logical_rows,
       countIf(status = 'duplicate') AS duplicate_rows,
       countIf(status = 'unmatched') AS unmatched_rows,
       countIf(status = 'ambiguous') AS ambiguous_rows
FROM results GROUP BY platform_name, app_build ORDER BY platform_name, app_build
SETTINGS max_execution_time = 30, max_rows_to_read = 10000000, read_overflow_mode = 'throw'
"""
    )
