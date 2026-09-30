"""Build a scan-config preview payload from a warehouse adapter.

This runs synchronously against the data source (connect, introspect columns,
fetch sample rows, discover JSON paths) and can be slow on large tables, so it
is invoked from the ``preview_scan_config_async`` Celery task rather than inline
in the request handler. The returned dict matches ``ScanConfigPreviewResponse``
and is JSON-serializable for storage on ``ScanPreviewJob.result_summary``.
"""

from __future__ import annotations

from datetime import datetime
from math import inf

from tripl.core.adapters.base import BaseAdapter, ColumnInfo
from tripl.core.analyzers.cardinality import _is_json_type
from tripl.core.analyzers.event_plan import _format_value
from tripl.core.property_schema import infer_property_type
from tripl.core.warehouse_types import is_string_type
from tripl.json_paths import (
    decode_json_path_value,
    flatten_json_paths,
    format_json_path_value,
    group_json_value_paths,
    json_safe,
    object_property_paths,
)

JSON_PATH_DISCOVERY_LIMIT = 1000
JSON_PATH_SAMPLE_LIMIT = 3
JSON_PATH_SAMPLE_ROW_LIMIT = 1000
# Example values the event + properties summary shows per key.
EVENT_PROPERTY_SAMPLE_LIMIT = 3


def _is_feature_worth_sampling(unique_count: int, total_rows: int) -> bool:
    if unique_count <= 1:
        return False
    if unique_count >= total_rows:
        return False
    return unique_count <= max(10, total_rows // 2)


def _select_diverse_preview_rows(
    columns: list[ColumnInfo],
    raw_rows: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    if len(raw_rows) <= limit:
        return raw_rows

    column_map = {column.name: column for column in columns}
    feature_values_by_name: dict[str, set[str]] = {}
    row_features: list[list[tuple[str, str]]] = []

    for row in raw_rows:
        features: list[tuple[str, str]] = []
        for column_name, raw_value in row.items():
            column = column_map[column_name]
            if _is_json_type(column.type_name):
                parsed_value = decode_json_path_value(raw_value)
                for path, nested_value in flatten_json_paths(parsed_value):
                    feature_name = f"{column_name}.{path}"
                    feature_value = format_json_path_value(nested_value)
                    features.append((feature_name, feature_value))
                    feature_values_by_name.setdefault(feature_name, set()).add(feature_value)
                continue

            feature_value = format_json_path_value(raw_value)
            features.append((column_name, feature_value))
            feature_values_by_name.setdefault(column_name, set()).add(feature_value)
        row_features.append(features)

    eligible_feature_names = {
        feature_name
        for feature_name, values in feature_values_by_name.items()
        if _is_feature_worth_sampling(len(values), len(raw_rows))
    }
    if not eligible_feature_names:
        return raw_rows[:limit]

    remaining_indices = list(range(len(raw_rows)))
    chosen_indices: list[int] = []
    seen_features: set[tuple[str, str]] = set()

    while remaining_indices and len(chosen_indices) < limit:
        best_index: int | None = None
        best_gain = -1
        best_penalty = inf

        for row_index in remaining_indices:
            eligible_features = {
                feature
                for feature in row_features[row_index]
                if feature[0] in eligible_feature_names
            }
            unseen_gain = len(eligible_features - seen_features)
            penalty = len(eligible_features & seen_features)

            if unseen_gain > best_gain or (unseen_gain == best_gain and penalty < best_penalty):
                best_index = row_index
                best_gain = unseen_gain
                best_penalty = penalty

        if best_index is None:
            break

        chosen_indices.append(best_index)
        seen_features.update(
            feature for feature in row_features[best_index] if feature[0] in eligible_feature_names
        )
        remaining_indices.remove(best_index)

        if best_gain <= 0 and len(chosen_indices) >= 1:
            break

    ordered_indices = sorted(chosen_indices)
    for row_index in range(len(raw_rows)):
        if len(ordered_indices) >= limit:
            break
        if row_index in chosen_indices:
            continue
        ordered_indices.append(row_index)

    return [raw_rows[row_index] for row_index in ordered_indices[:limit]]


def _merge_json_path_samples(
    discovered: dict[str, dict[str, list[object]]],
    selected: dict[str, list[str]],
    json_column_names: list[str],
) -> dict[str, dict[str, list[object]]]:
    merged = {
        column_name: {
            path: list(values[:JSON_PATH_SAMPLE_LIMIT])
            for path, values in discovered.get(column_name, {}).items()
        }
        for column_name in json_column_names
    }

    for column_name in json_column_names:
        selected_paths = selected.get(column_name, [])
        if not selected_paths:
            continue
        column_samples = merged.setdefault(column_name, {})
        for path in selected_paths:
            column_samples.setdefault(path, [])

    return merged


def _get_json_path_samples(
    adapter: BaseAdapter,
    base_query: str,
    json_column_names: list[str],
    *,
    time_column: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> dict[str, dict[str, list[object]]]:
    if not json_column_names:
        return {}

    # No ``except AttributeError`` fallback here any more. It existed for an
    # "adapter without native discovery", but ``BaseAdapter.get_json_path_samples``
    # IS that fallback — a concrete default that samples rows and flattens the
    # JSON locally — and every adapter the registry can build derives from
    # ``BaseAdapter``. So the except could no longer fire for a missing method,
    # only for an AttributeError raised INSIDE an adapter (a None client, a
    # renamed attribute), which it answered with a local 1000-row sample instead
    # of the warehouse-side discovery ClickHouse and Postgres override this with:
    # a different, worse result, reported as success and logged nowhere. An
    # adapter bug now reaches ``preview_scan_config_async`` and fails the job,
    # like every other adapter failure in that task.
    return adapter.get_json_path_samples(
        base_query,
        json_column_names,
        time_column=time_column,
        time_from=time_from,
        time_to=time_to,
        path_limit=JSON_PATH_DISCOVERY_LIMIT,
        sample_limit=JSON_PATH_SAMPLE_LIMIT,
        sample_row_limit=JSON_PATH_SAMPLE_ROW_LIMIT,
    )


def _value_at(document: object, path: str) -> object:
    """The value at a dotted ``path`` of a decoded JSON document, or None."""
    current = document
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def summarize_event_properties(
    columns: list[ColumnInfo],
    rows: list[dict[str, object]],
    *,
    event_name_column: str,
    properties_column: str,
) -> dict[str, object]:
    """What the "event + properties" preset yields from a preview's sample rows.

    One entry per event name, busiest first, each with every key its rows'
    properties carried: the share of the event's rows that carried it, the type
    ``infer_property_type`` reads off the values (the one a run would set), and
    a few example values. The name is the event column's value formatted the way
    the planner formats it, and a row whose name comes out empty is skipped, as
    a run skips it.

    Keys are the properties a run catalogues: the JSON's leaf paths, with a
    nested object folded into ONE property the way the scan folds it
    (``object_property_paths``, F23.4e) — the preset pins no path, so every
    object folds. A summary of a sample: an event or key missing here may still
    exist, which the dry run answers for the whole lookback window.
    """
    summary: dict[str, object] = {
        "event_name_column": event_name_column,
        "properties_column": properties_column,
        "sample_rows": 0,
        "events": [],
        "error": None,
    }
    by_name = {column.name: column for column in columns}
    for role, name in (("event", event_name_column), ("properties", properties_column)):
        if name not in by_name:
            summary["error"] = f"The {role} column {name!r} is not in the query's columns."
            return summary
    properties_type = by_name[properties_column].type_name
    if not _is_json_type(properties_type):
        hint = (
            " Tick 'Parse as JSON' to read its text as JSON."
            if is_string_type(properties_type)
            else ""
        )
        summary["error"] = (
            f"The properties column {properties_column!r} is {properties_type}, "
            f"not a JSON column.{hint}"
        )
        return summary

    named_rows: list[tuple[str, object]] = []
    leaf_paths: set[str] = set()
    for row in rows:
        name = _format_value(row.get(event_name_column))
        if not name:
            continue
        properties = decode_json_path_value(row.get(properties_column))
        named_rows.append((name, properties))
        leaf_paths.update(path for path, _value in flatten_json_paths(properties))
    folded = object_property_paths(leaf_paths)

    rows_by_event: dict[str, int] = {}
    values_by_event: dict[str, dict[str, list[object]]] = {}
    for name, properties in named_rows:
        rows_by_event[name] = rows_by_event.get(name, 0) + 1
        paths = values_by_event.setdefault(name, {})
        seen: set[str] = set()
        for leaf, leaf_value in flatten_json_paths(properties):
            path = folded.get(leaf, leaf)
            paths.setdefault(path, [])
            # Presence counts rows, so a property repeated inside one row (the
            # leaves of one object) counts once.
            if path not in seen:
                seen.add(path)
                value = _value_at(properties, path) if path != leaf else leaf_value
                paths[path].append(value)

    events: list[dict[str, object]] = []
    for name, count in sorted(rows_by_event.items(), key=lambda item: (-item[1], item[0])):
        properties_out: list[dict[str, object]] = []
        for path, values in sorted(values_by_event[name].items()):
            inferred = infer_property_type(values)
            examples: list[str] = []
            for value in values:
                if value is None:
                    continue
                text = format_json_path_value(value)
                if text not in examples:
                    examples.append(text)
                if len(examples) >= EVENT_PROPERTY_SAMPLE_LIMIT:
                    break
            properties_out.append(
                {
                    "path": path,
                    "presence": len(values) / count,
                    "type": inferred[0] if inferred is not None else None,
                    "sample_values": examples,
                }
            )
        events.append({"name": name, "sample_rows": count, "properties": properties_out})
    summary["sample_rows"] = sum(rows_by_event.values())
    summary["events"] = events
    return summary


def build_preview_payload(
    adapter: BaseAdapter,
    base_query: str,
    limit: int,
    *,
    time_column: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    event_name_column: str | None = None,
    properties_column: str | None = None,
) -> dict[str, object]:
    """Connect and sample rows for a fast, single-query preview.

    Deliberately does NOT discover JSON paths: that scan can be slow on large
    tables and is requested separately via ``build_json_paths_payload`` (the
    "Discover JSON keys" action). The shape still mirrors
    ``ScanConfigPreviewResponse``; ``json_columns`` lists the JSON-typed columns
    with empty ``paths`` so the UI knows discovery is available.

    With both ``event_name_column`` and ``properties_column`` (the "event +
    properties" preset) the payload also carries ``event_properties``,
    summarised from every fetched row rather than the few shown, so the keys an
    event carries are not limited to the rows picked for their diversity.
    """
    adapter.test_connection()

    columns = adapter.get_columns(base_query)
    column_map = {column.name: column for column in columns}
    preview_fetch_limit = min(max(limit * 8, 50), 200)
    row_column_names, row_values = adapter.get_preview_rows(
        base_query,
        limit=preview_fetch_limit,
        time_column=time_column,
        time_from=time_from,
        time_to=time_to,
    )

    sampled_rows = [
        {name: value for name, value in zip(row_column_names, row, strict=False)}
        for row in row_values
    ]
    raw_rows = _select_diverse_preview_rows(columns, sampled_rows, limit=limit)
    # ``json_safe`` is not decoration: this payload is assigned to
    # ``ScanPreviewJob.result_summary``, an ``sa.JSON`` column, and the sync
    # worker engine registers no ``json_serializer`` (worker/db.py), so plain
    # ``json.dumps`` encodes it at commit. One ``datetime`` left inside an array
    # or a map fails that commit and the job is reported as an internal error.
    preview_rows = [
        {
            name: json_safe(
                decode_json_path_value(value)
                if _is_json_type(column_map[name].type_name)
                else value
            )
            for name, value in row.items()
        }
        for row in raw_rows
    ]

    json_columns = [
        {"column": column.name, "paths": []}
        for column in columns
        if _is_json_type(column.type_name)
    ]

    payload: dict[str, object] = {
        "columns": [
            {
                "name": column.name,
                "type_name": column.type_name,
                "is_nullable": column.is_nullable,
            }
            for column in columns
        ],
        "rows": preview_rows,
        "json_columns": json_columns,
    }
    if event_name_column and properties_column:
        payload["event_properties"] = json_safe(
            summarize_event_properties(
                columns,
                sampled_rows,
                event_name_column=event_name_column,
                properties_column=properties_column,
            )
        )
    return payload


def build_json_paths_payload(
    adapter: BaseAdapter,
    base_query: str,
    json_value_paths: list[str],
    *,
    time_column: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> dict[str, object]:
    """Discover JSON path candidates for the source query.

    This is the slow half of the preview (it scans the source to enumerate
    nested JSON keys), so it runs as its own worker job behind an explicit
    "Discover JSON keys" action. Returns ``{"json_columns": [...]}`` where each
    JSON column carries its discovered ``paths`` (plus any already-selected
    ``json_value_paths`` so they stay visible even with no sampled value).
    """
    adapter.test_connection()

    columns = adapter.get_columns(base_query)
    json_column_names = [column.name for column in columns if _is_json_type(column.type_name)]
    discovered_json_path_samples = _get_json_path_samples(
        adapter,
        base_query,
        json_column_names,
        time_column=time_column,
        time_from=time_from,
        time_to=time_to,
    )
    json_path_samples = _merge_json_path_samples(
        discovered_json_path_samples,
        group_json_value_paths(json_value_paths),
        json_column_names,
    )

    json_columns: list[dict[str, object]] = []
    for column in columns:
        if not _is_json_type(column.type_name):
            continue
        sample_values_by_path = json_path_samples.get(column.name, {})
        json_columns.append(
            {
                "column": column.name,
                "paths": [
                    {
                        "full_path": f"{column.name}.{path}",
                        "path": path,
                        "sample_values": [
                            format_json_path_value(sample_value)
                            for sample_value in sample_values_by_path[path]
                        ],
                    }
                    for path in sorted(sample_values_by_path)
                ],
            }
        )

    return {"json_columns": json_columns}
