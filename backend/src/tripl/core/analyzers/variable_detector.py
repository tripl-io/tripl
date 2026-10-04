"""Detect variable patterns in high-cardinality column values.

Given a sample of values from a high-cardinality column, detects patterns like:
- JSON objects with mixed high/low cardinality keys → template with ${key} placeholders
- URL/path segments with variable parts → /users/${user_id}/profile
- Purely numeric values → ${column_name}
- Generic strings with variable tokens
"""

from __future__ import annotations

import json
import re
import uuid as _uuid
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class DetectedVariable:
    name: str
    inferred_type: str  # string, number, json, ...
    distinct_count: int = 0
    values: list[str] = field(default_factory=list)


@dataclass
class DetectedPattern:
    template: str
    variables: list[DetectedVariable] = field(default_factory=list)
    coverage_pct: float = 0.0


_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _is_uuid(val: str) -> bool:
    try:
        _uuid.UUID(val)
        return True
    except ValueError, AttributeError:
        return False


def _is_numeric(val: str) -> bool:
    return bool(_NUMERIC_RE.match(val))


def _looks_like_json(val: str) -> bool:
    stripped = val.strip()
    return (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    )


def _distinct_values(values: list[str]) -> list[str]:
    return sorted({value for value in values if value != ""})


def detect_variables(
    column_name: str,
    values: list[str],
    cardinality_threshold: int = 100,
) -> DetectedPattern | None:
    """Detect variable patterns in a list of sample values for a high-cardinality column.

    Returns a DetectedPattern with a template and variable list, or None if
    the column should be treated as a plain variable (no structural pattern).
    """
    if not values:
        return None

    # 1. Try JSON detection
    json_result = _detect_json_pattern(column_name, values, cardinality_threshold)
    if json_result is not None:
        return json_result

    # 2. Try URL/path detection
    path_result = _detect_path_pattern(column_name, values, cardinality_threshold)
    if path_result is not None:
        return path_result

    # 3. Pure numeric → single variable
    if all(_is_numeric(v) for v in values if v):
        return DetectedPattern(
            template=f"${{{column_name}}}",
            variables=[
                DetectedVariable(
                    name=column_name,
                    inferred_type="number",
                    distinct_count=len(_distinct_values(values)),
                    values=_distinct_values(values),
                )
            ],
            coverage_pct=100.0,
        )

    # 4. Pure UUIDs → single variable
    if all(_is_uuid(v) for v in values if v):
        return DetectedPattern(
            template=f"${{{column_name}}}",
            variables=[
                DetectedVariable(
                    name=column_name,
                    inferred_type="string",
                    distinct_count=len(_distinct_values(values)),
                    values=_distinct_values(values),
                )
            ],
            coverage_pct=100.0,
        )

    # 5. Generic string with tokenization
    generic_result = _detect_generic_string_pattern(column_name, values, cardinality_threshold)
    if generic_result is not None:
        return generic_result

    # 6. Fallback: entire value is the variable
    return DetectedPattern(
        template=f"${{{column_name}}}",
        variables=[
            DetectedVariable(
                name=column_name,
                inferred_type="string",
                distinct_count=len(_distinct_values(values)),
                values=_distinct_values(values),
            )
        ],
        coverage_pct=100.0,
    )


def _detect_json_pattern(
    column_name: str,
    values: list[str],
    cardinality_threshold: int,
) -> DetectedPattern | None:
    """Detect patterns in JSON object values.

    For each key across all samples, collect values and compute cardinality.
    High-cardinality keys get ${key_name} placeholder, low-cardinality keys keep values.
    """
    parsed: list[dict[str, object]] = []
    for v in values:
        if not _looks_like_json(v):
            return None
        try:
            obj = json.loads(v)
        except json.JSONDecodeError, TypeError:
            return None
        if not isinstance(obj, dict):
            return None
        parsed.append(obj)

    if not parsed:
        return None

    # Collect all keys and their values
    key_values: dict[str, list[str]] = {}
    for obj in parsed:
        for k, val in obj.items():
            key_values.setdefault(k, []).append(str(val))

    # Classify each key
    template_obj: dict[str, str] = {}
    variables: list[DetectedVariable] = []

    for key, vals in key_values.items():
        unique_count = len(set(vals))
        if unique_count <= cardinality_threshold:
            # Low cardinality: NOT a variable, so the template carries one of the
            # observed values literally. It used to carry a ``${_low:<key>}``
            # token beside a comment promising "these will produce separate
            # events per value" — an expansion no production code performs.
            # ``event_plan`` stores ``meta['template']`` verbatim as the field
            # value and ``normalize_variable_tokens`` leaves an unresolved token
            # alone, so the token reached the plan as text and
            # ``event_service._attach_template_warnings`` reported it on every
            # save, naming a variable that can never exist.
            # First of the sorted distinct values so two scans over the same
            # data produce the same template.
            representative = _distinct_values(vals)
            template_obj[key] = representative[0] if representative else ""
        else:
            # High cardinality → variable
            var_name = key
            inferred_type = "number" if all(_is_numeric(v) for v in vals) else "string"
            template_obj[key] = f"${{{var_name}}}"
            distinct_values = _distinct_values(vals)
            variables.append(
                DetectedVariable(
                    name=var_name,
                    inferred_type=inferred_type,
                    distinct_count=len(distinct_values),
                    values=distinct_values,
                )
            )

    template = json.dumps(template_obj, ensure_ascii=False, sort_keys=True)
    return DetectedPattern(
        template=template,
        variables=variables,
        coverage_pct=100.0,
    )


def _detect_path_pattern(
    column_name: str,
    values: list[str],
    cardinality_threshold: int,
) -> DetectedPattern | None:
    """Detect patterns in URL/path values like /users/123/profile."""
    # Check if most values look like paths
    path_count = sum(1 for v in values if v.startswith("/") or v.startswith("http"))
    if path_count < len(values) * 0.7:
        return None

    # Normalize: strip protocol+host if present, keep path
    paths: list[list[str]] = []
    for v in values:
        path = v
        if path.startswith("http"):
            # Strip scheme + authority
            idx = path.find("/", path.find("//") + 2)
            path = path[idx:] if idx >= 0 else "/"
        segments = path.split("/")
        paths.append(segments)

    # Find most common segment count (structural similarity)
    length_counts = Counter(len(p) for p in paths)
    most_common_length = length_counts.most_common(1)[0][0]
    matching_paths = [p for p in paths if len(p) == most_common_length]

    if len(matching_paths) < len(paths) * 0.5:
        return None

    # Analyze per-position cardinality
    n_segments = most_common_length
    template_parts: list[str] = []
    variables: list[DetectedVariable] = []

    for i in range(n_segments):
        position_values = [p[i] for p in matching_paths]
        unique_count = len(set(position_values))

        if unique_count <= min(cardinality_threshold, 10):
            # Constant or near-constant segment
            most_common_val = Counter(position_values).most_common(1)[0][0]
            template_parts.append(most_common_val)
        else:
            # Variable segment — infer type
            if all(_is_numeric(v) for v in position_values if v):
                var_name = f"{column_name}_id" if i > 0 else column_name
                inferred_type = "number"
            elif all(_is_uuid(v) for v in position_values if v):
                var_name = f"{column_name}_uuid" if i > 0 else column_name
                inferred_type = "string"
            else:
                var_name = f"{column_name}_segment_{i}"
                inferred_type = "string"
            var_name = _position_unique_name(var_name, i, variables)
            template_parts.append(f"${{{var_name}}}")
            distinct_values = _distinct_values(position_values)
            variables.append(
                DetectedVariable(
                    name=var_name,
                    inferred_type=inferred_type,
                    distinct_count=len(distinct_values),
                    values=distinct_values,
                )
            )

    if not variables:
        return None

    template = "/".join(template_parts)
    coverage = len(matching_paths) / len(paths) * 100
    return DetectedPattern(template=template, variables=variables, coverage_pct=coverage)


def _position_unique_name(name: str, position: int, taken: list[DetectedVariable]) -> str:
    """Keep two variable positions of one pattern from sharing a name.

    ``/users/123/posts/456`` has two numeric positions, and both would be named
    ``{column}_id``. The planner dedups needs by name, so user ids and post ids
    became one variable with one merged value list. The first
    position keeps the plain name, so existing single-position variables are
    unchanged; a later clash gets its position index.
    """
    if any(variable.name == name for variable in taken):
        return f"{name}_{position}"
    return name


def _detect_generic_string_pattern(
    column_name: str,
    values: list[str],
    cardinality_threshold: int,
) -> DetectedPattern | None:
    """Detect patterns in generic strings by tokenizing on common delimiters."""
    # Try to find a common delimiter
    for delimiter in ["_", "-", ".", " "]:
        tokenized = [v.split(delimiter) for v in values]
        length_counts = Counter(len(t) for t in tokenized)
        most_common_length = length_counts.most_common(1)[0][0]

        if most_common_length < 2:
            continue

        matching = [t for t in tokenized if len(t) == most_common_length]
        if len(matching) < len(values) * 0.5:
            continue

        # Check per-position cardinality
        template_parts: list[str] = []
        variables: list[DetectedVariable] = []
        has_variable = False

        for i in range(most_common_length):
            position_values = [t[i] for t in matching]
            unique_count = len(set(position_values))

            if unique_count <= min(cardinality_threshold, 10):
                most_common_val = Counter(position_values).most_common(1)[0][0]
                template_parts.append(most_common_val)
            else:
                has_variable = True
                if all(_is_numeric(v) for v in position_values):
                    var_name = f"{column_name}_id"
                    inferred_type = "number"
                else:
                    var_name = f"{column_name}_part_{i}"
                    inferred_type = "string"
                var_name = _position_unique_name(var_name, i, variables)
                template_parts.append(f"${{{var_name}}}")
                distinct_values = _distinct_values(position_values)
                variables.append(
                    DetectedVariable(
                        name=var_name,
                        inferred_type=inferred_type,
                        distinct_count=len(distinct_values),
                        values=distinct_values,
                    )
                )

        if has_variable:
            template = delimiter.join(template_parts)
            coverage = len(matching) / len(values) * 100
            return DetectedPattern(template=template, variables=variables, coverage_pct=coverage)

    return None
