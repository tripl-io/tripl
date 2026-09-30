"""Normalized classification of warehouse column types.

Each warehouse spells its types differently — and, critically, *cases* them
differently. ClickHouse reports ``JSON`` / ``Map(String, String)``, BigQuery
reports ``JSON`` / ``RECORD``, and psycopg reports PostgreSQL's ``json`` / ``jsonb``
in lowercase. A case-sensitive substring match for ``"JSON"`` therefore silently
classifies every PostgreSQL JSON column as a plain scalar, which is why JSON
preview, discovery and path extraction never activated on PostgreSQL at all.

Classification here is case-insensitive, so each dialect's spelling is matched
explicitly rather than by lucky substring.
"""

from __future__ import annotations

import re
from enum import StrEnum

#: ``Nullable(JSON)``, ``LowCardinality(String)`` — strip ClickHouse's decorations
#: down to the type that actually matters.
_CH_WRAPPER_RE = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)$")


class ComplexKind(StrEnum):
    """How a column's nested values must be addressed."""

    #: Schemaless document: paths are discovered from the data (CH ``JSON``,
    #: BigQuery ``JSON``, PostgreSQL ``json``/``jsonb``).
    json = "json"
    #: Fixed nested schema: paths come from the declared schema, not the rows
    #: (BigQuery ``RECORD``/``STRUCT``, ClickHouse ``Tuple``).
    struct = "struct"
    #: Key/value container (ClickHouse ``Map``).
    map = "map"


class TimeKind(StrEnum):
    """What a time column represents, which decides how it can be bucketed."""

    #: An absolute instant. Bucketable and comparable against a UTC literal.
    timestamp = "timestamp"
    #: Wall-clock date+time with no zone. Bucketable, but the literal must be
    #: zone-less too, or the warehouse rejects the comparison.
    datetime = "datetime"
    #: Date only. Bucketable at 1d/1w; finer intervals are meaningless.
    date = "date"
    #: Time-of-day, or a type we cannot bucket. Must be rejected at configuration
    #: time with an actionable error, not at collection time inside a worker.
    unsupported = "unsupported"


def _normalize(type_name: str) -> str:
    """Lowercase, and unwrap ClickHouse's Nullable/LowCardinality decorations."""
    stripped = type_name.strip()
    match = _CH_WRAPPER_RE.match(stripped)
    while match:
        stripped = match.group(1).strip()
        match = _CH_WRAPPER_RE.match(stripped)
    return stripped.lower()


def _is_array(normalized: str) -> bool:
    """Whether a NORMALIZED type name names an array rather than one value.

    PostgreSQL is the only dialect that spells this as a suffix: psycopg reports an
    array column as ``jsonb[]`` / ``int4[]`` / ``timestamptz[]``. ClickHouse's
    ``Array(JSON)`` and BigQuery's REPEATED mode are already handled by falling off
    the ends of both classifiers, so the suffix is all that is missing.
    """
    return normalized.endswith("[]")


def classify_complex(type_name: str) -> ComplexKind | None:
    """Classify a column as a complex/nested type, or ``None`` if it is scalar.

    Case-insensitive across every dialect: ClickHouse ``JSON``/``Object(...)``/
    ``Tuple(...)``/``Map(...)``, BigQuery ``JSON``/``RECORD``/``STRUCT``, and
    PostgreSQL ``json``/``jsonb``.

    An ARRAY of any of those is NOT complex. Every classifier here is a prefix
    test, so ``jsonb[]`` would otherwise answer :attr:`ComplexKind.json` and be
    routed into the JSON path walk, which casts the column ``::jsonb`` — an error
    Postgres raises for an array, taking the whole scan with it. Treating the
    array as an opaque scalar is what the other two dialects already do
    (ClickHouse ``Array(JSON)`` matches no prefix here; BigQuery rejects REPEATED
    in its own adapter), and it works on Postgres: array types have a default
    btree opclass, so the column still groups and still renders through
    ``::text``.
    """
    name = _normalize(type_name)
    if _is_array(name):
        return None
    if name.startswith(("json", "object(")):
        # Covers CH `JSON`/`Object('json')`, BQ `JSON`, PG `json`/`jsonb`.
        return ComplexKind.json
    if name.startswith(("record", "struct", "tuple(")):
        return ComplexKind.struct
    if name.startswith("map("):
        return ComplexKind.map
    return None


def is_complex_type(type_name: str) -> bool:
    """Whether a column holds nested values that need path extraction."""
    return classify_complex(type_name) is not None


def is_string_type(type_name: str) -> bool:
    """Whether a column holds plain text a scan may be asked to parse as JSON.

    ClickHouse ``String`` / ``FixedString(N)`` (under any ``Nullable`` /
    ``LowCardinality`` wrapper) and BigQuery ``STRING``: the two dialects whose
    adapters implement ``json_string_source`` (F23.9, #306).
    """
    name = _normalize(type_name)
    return name == "string" or name.startswith("fixedstring(")


def classify_time(type_name: str) -> TimeKind:
    """Classify a column's suitability as a scan/metric time column.

    Returns :attr:`TimeKind.unsupported` for anything that cannot carry a date —
    notably BigQuery ``TIME`` and PostgreSQL ``time``/``timetz``, which have no
    date part and so cannot be bucketed into a window at all, and an ARRAY of a
    time type: ``timestamptz[]`` names many instants, so no window bound compares
    against it and no bucket expression accepts it. The prefix tests below would
    otherwise read it as a single ``timestamp`` and let it be picked as a scan's
    time column, which fails inside a worker instead of at configuration time.
    """
    name = _normalize(type_name)
    if _is_array(name):
        return TimeKind.unsupported
    # Order matters: `timestamptz` must be seen before the bare `time` fallthrough,
    # and `datetime64(3)` before `date`.
    if name.startswith("timestamp"):
        return TimeKind.timestamp
    if name.startswith("datetime"):
        # ClickHouse DateTime/DateTime64 are absolute instants (they carry a
        # timezone); BigQuery DATETIME is a zone-less wall clock. The dialect
        # adapter decides the literal form — both are bucketable.
        return TimeKind.datetime
    if name.startswith("date"):
        # `date`, `Date32`.
        return TimeKind.date
    return TimeKind.unsupported
