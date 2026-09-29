from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime

#: How many property entries (``<json_column>.<path>``) one scan config may list
#: in ``metric_breakdown_columns``, and separately in ``distribution_drift_fields``.
#: A scalar breakdown reads a column the scan already groups on; a property one
#: parses the JSON document on every row of the window, once per property, so
#: their number is what bounds the extra cost of one collection query.
MAX_PROPERTY_FIELDS = 10

# One segment of a property path, and the column in front of it: the grammar
# every adapter's JSON path helper already enforces before it interpolates a
# segment into SQL. Checked at the save boundary too, so a path no warehouse
# would accept is refused while the user is still choosing it.
_PROPERTY_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize_json_value_paths(paths: Iterable[str] | None) -> list[str]:
    if not paths:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = raw_path.strip()
        if not path or "." not in path:
            continue
        if path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return sorted(normalized)


def group_json_value_paths(paths: Iterable[str] | None) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for full_path in normalize_json_value_paths(paths):
        column_name, json_path = full_path.split(".", 1)
        grouped.setdefault(column_name, []).append(json_path)
    return grouped


def set_nested_value(target: dict[str, object], dotted_path: str, value: object) -> None:
    parts = [part for part in dotted_path.split(".") if part]
    if not parts:
        return

    cursor = target
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def decode_json_path_value(raw_value: object) -> object:
    if raw_value is None or isinstance(raw_value, (bool, int, float, list, dict)):
        return raw_value
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value
    return str(raw_value)


def json_safe(value: object) -> object:
    """Rebuild a warehouse value as JSON-native data, all the way down.

    Adapters hand back raw driver values and a container hides them from every
    top-level type check: a ClickHouse ``Array(DateTime)`` arrives as a list of
    ``datetime``, a ``Map(Date, String)`` as a dict with ``date`` keys, a
    BigQuery ``STRUCT<ARRAY<TIMESTAMP>>`` as nested lists. Anything that then
    reaches ``json.dumps`` — this module's renderer below, or SQLAlchemy
    serialising a preview payload into ``ScanPreviewJob.result_summary`` —
    raised ``TypeError`` and the operator was told "Scan failed due to an
    internal error."

    ``json.dumps(..., default=str)`` is the obvious cheaper fix and it does not
    work: ``default`` is consulted for values only, never for dict KEYS, so a
    ``Map(Date, String)`` keeps raising. Rebuilding the tree covers both halves
    with one rule.

    Keys become ``str``. For int and finite float keys that is byte-identical
    to the coercion ``json.dumps`` already applied; what changes is their ORDER
    under ``sort_keys=True`` (``"1", "10", "2"`` rather than ``1, 2, 10``), and
    that ordering is NOT confined to display text. :func:`format_json_path_value`
    below is what ``catalog_sync._formatted_samples`` renders into the strings a
    variable context stores (``VariableValue.values``), what
    ``event_plan.raw_values_from_row`` feeds into the kwargs an event name is
    built from — and that name is the ``Event.source_name`` a series is filed
    under — and what a preview payload keeps as a path's sample values in
    ``ScanPreviewJob.result_summary``. A reordering here is therefore a change to
    stored data, and through the name format a change to event identity.

    No reachable input is known to move today: every container we have traced to
    that renderer is decoded from JSON text (``decode_json_path_value``'s
    ``json.loads``, or a ``toJSONString`` column), so its keys are strings
    already and sorting them changes nothing. That is a precondition, not a
    guarantee — nothing enforces it, and a driver-native mapping with non-string
    keys (the ``Map(Date, String)`` above) reaching the renderer would re-order
    one and mint a new identity from it. In exchange ``sort_keys=True`` stops
    being a second latent ``TypeError``: keys of mixed types are not orderable.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        # Matches what the preview payload has always emitted for a top-level
        # datetime; ``str()`` would spell the same instant with a space.
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)


def format_json_path_value(raw_value: object) -> str:
    value = decode_json_path_value(raw_value)
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    # Only a list or a dict can reach here: every branch above and
    # ``decode_json_path_value``'s trailing ``str()`` have taken the rest. Its
    # LEAVES, though, are still raw driver values, which is why the container
    # goes through ``json_safe`` first.
    #
    # Deliberate asymmetry: a scalar datetime is stringified by
    # ``decode_json_path_value`` ("2026-04-12 10:30:00") while one nested in a
    # container renders as isoformat. Unifying them would move sample-value and
    # variable-value strings that the drift detector compares across runs, for
    # no gain — this function's output is display text and a dedup key.
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True)


def object_property_paths(
    paths: Iterable[str],
    *,
    pinned: Collection[str] = (),
    documented: Collection[str] = (),
) -> dict[str, str]:
    """Which leaf paths of one JSON column belong to an object property (F23).

    A nested object is ONE property with a sub-schema, not a set of dotted
    leaves (owner decision 5). The warehouse reports leaf paths only
    (``JSONAllPaths``, the PostgreSQL walk, the flattening fallback), so the
    objects are read off their shape: a key with a path below it is an object.

    Returns ``leaf path -> property path`` for every leaf that folds into an
    object property; a leaf missing from the result stays its own property.
    The SHALLOWEST prefix that can fold wins, and a prefix folds when

    * some observed path lies below it (it is an object at all);
    * no observed path IS it (a key that is a scalar on some rows and an object
      on others keeps its dotted leaves — one type per property);
    * no ``pinned`` path lies at or below it. A pinned path is one whose value
      something reads on its own — a kept ``json_value_paths`` entry, a
      name-format placeholder — and it stays a dotted leaf. Its parent unfolds
      one level, so the pin's siblings can still fold into objects of their own.

    ``documented`` are the paths of properties a person has made their own. A
    documented LEAF is pinned like the above; a documented OBJECT is where its
    subtree folds, never a shallower ancestor, so the documented property keeps
    being the one the template names.

    Paths are relative to the column.
    """
    leaves = sorted(set(paths))
    leaf_set = set(leaves)
    documented_set = set(documented)
    pins = set(pinned) | (documented_set & leaf_set)
    fold_points = documented_set - leaf_set
    roots: dict[str, str] = {}

    def pinned_at_or_below(prefix: str) -> bool:
        below = prefix + "."
        return any(pin == prefix or pin.startswith(below) for pin in pins)

    def fold(prefix: str, members: list[str]) -> None:
        # ``members`` are the leaves strictly below ``prefix`` (every leaf of the
        # column when ``prefix`` is empty), grouped here by their next segment.
        depth = prefix.count(".") + 1 if prefix else 0
        children: dict[str, list[str]] = {}
        for leaf in members:
            segment = leaf.split(".")[depth]
            child = f"{prefix}.{segment}" if prefix else segment
            if leaf != child:
                children.setdefault(child, []).append(leaf)
        for child, below in children.items():
            if child in leaf_set:
                continue
            can_fold = not pinned_at_or_below(child)
            deeper_fold_point = any(point.startswith(child + ".") for point in fold_points)
            if can_fold and (child in fold_points or not deeper_fold_point):
                for leaf in below:
                    roots[leaf] = child
                continue
            fold(child, below)

    fold("", [leaf for leaf in leaves if leaf])
    return roots


def build_json_value(
    column_name: str,
    paths: Iterable[str],
    *,
    preserved_values: dict[str, object] | None = None,
    property_paths: Mapping[str, str] | None = None,
) -> str:
    """The JSON template of one row: every path a ``${column.path}`` token.

    ``property_paths`` (``object_property_paths``' result) folds the leaves of
    a nested object into one token for the whole object:
    ``{"user": "${props.user}"}`` instead of ``{"user": {"id": "${props.user.id}"}}``.
    A preserved (kept) value is never folded.
    """
    json_obj: dict[str, object] = {}
    preserved_values = preserved_values or {}
    property_paths = property_paths or {}

    for path in sorted(paths):
        full_path = f"{column_name}.{path}"
        if full_path in preserved_values:
            set_nested_value(json_obj, path, preserved_values[full_path])
            continue
        path = property_paths.get(path, path)
        set_nested_value(json_obj, path, f"${{{column_name}.{path}}}")

    return json.dumps(json_obj, ensure_ascii=False, sort_keys=True)


def flatten_json_paths(
    value: object, *, prefix: str = "", include_objects: bool = False
) -> list[tuple[str, object]]:
    """``(dotted path, value)`` for every leaf of *value*.

    ``include_objects`` also reports every non-empty nested object at its own
    path, before its leaves — what an object property is sampled from (F23).
    """
    if isinstance(value, dict):
        flattened: list[tuple[str, object]] = []
        if include_objects and prefix and value:
            flattened.append((prefix, value))
        for key, nested_value in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(
                flatten_json_paths(
                    nested_value, prefix=next_prefix, include_objects=include_objects
                )
            )
        return flattened
    if not prefix:
        return []
    return [(prefix, value)]


def split_property_field(entry: str) -> tuple[str, str] | None:
    """``(json_column, path)`` of a property entry, ``None`` for a scalar column.

    A property entry is ``<json_column>.<path>`` — the ``json_value_paths``
    format — naming one JSON path of a nested column; anything without a dot is
    a plain column. Raises ``ValueError`` for a dotted entry that breaks the
    grammar (an empty segment, or one that is not an identifier), so no caller
    can hand a warehouse a segment it would have to quote.
    """
    if "." not in entry:
        return None
    column, path = entry.split(".", 1)
    segments = [column, *path.split(".")]
    if not all(_PROPERTY_SEGMENT_RE.match(segment) for segment in segments):
        msg = (
            f"Invalid property path {entry!r}: use <json_column>.<path>, where every "
            "segment is letters, digits and underscores and does not start with a digit"
        )
        raise ValueError(msg)
    return column, path


def is_property_field(entry: str) -> bool:
    return "." in entry


def extract_json_path(raw_value: object, path: str) -> object:
    """The value at dotted ``path`` of a JSON document, or ``None`` if it has none.

    The row-by-row counterpart of the adapters' SQL extraction, for the Python
    fallbacks: the document may arrive decoded or as JSON text.
    """
    value = decode_json_path_value(raw_value)
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def json_scalar_text(value: object) -> str | None:
    """A JSON value as the text SQL extraction yields: ``None`` for JSON null.

    Strings are unquoted and booleans are ``true``/``false`` (``->>`` and
    ``JSON_VALUE`` agree on both); containers are JSON text.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True)
