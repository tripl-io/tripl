"""Scan preview: nested warehouse values and JSON path discovery.

A preview payload must be ``json.dumps``-able with the
stdlib encoder, because ``ScanPreviewJob.result_summary`` is ``sa.JSON`` and the
worker's sync engine registers no ``json_serializer``. Only the TOP level of
each cell was ever checked, so a ``datetime`` inside a ClickHouse
``Array(DateTime)`` or ``Map(String, DateTime)`` reached ``json.dumps`` intact
and raised ``TypeError`` at commit — surfacing to the operator as "Scan failed
due to an internal error." There are two distinct routes into that failure
(with and without the diversity pass) and one test per route below.

JSON path discovery used to wrap the adapter call in
``except AttributeError`` and fall back to sampling rows locally. Every adapter
inherits a concrete ``BaseAdapter.get_json_path_samples``, so the only thing
that except could still catch was an AttributeError raised inside an adapter,
which it answered with a different, worse result reported as success.

No live warehouse and no database: the adapters are hand-written fakes and the
payload builders are called directly.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.preview import build_json_paths_payload, build_preview_payload
from tripl.json_paths import format_json_path_value

SAMPLED_AT = datetime(2026, 4, 12, 10, 30)


class _NestedValuesAdapter:
    """One row whose cells hide non-JSON-native values inside containers.

    ``Array(DateTime)`` is deliberately NOT a complex type by
    ``warehouse_types.classify_complex`` (only json/object/record/struct/tuple/
    map are), so it reaches the payload as a raw driver list — which is exactly
    how the nested ``datetime`` used to escape every type check.
    """

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="created_at", type_name="DateTime"),
            ColumnInfo(name="ids", type_name="Array(DateTime)"),
            ColumnInfo(name="tags", type_name="Map(String, DateTime)"),
            ColumnInfo(name="amounts", type_name="Array(Decimal(18, 2))"),
            ColumnInfo(name="trace", type_name="UUID"),
            ColumnInfo(name="location", type_name="point"),
            ColumnInfo(name="payload", type_name="JSON"),
        ]

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        **_kwargs: object,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return (
            ["created_at", "ids", "tags", "amounts", "trace", "location", "payload"],
            [
                (
                    SAMPLED_AT,
                    [SAMPLED_AT],
                    {"a": SAMPLED_AT},
                    [Decimal("1.50")],
                    uuid.UUID("2f1d1b1e-0000-4000-8000-000000000001"),
                    (55.7, 37.6),
                    '{"extra": {"key": "TASK-123"}}',
                ),
            ],
        )

    def close(self) -> None:
        return None


def test_build_preview_payload_serializes_nested_warehouse_values() -> None:
    # One row and a limit of 5, so `_select_diverse_preview_rows` returns early
    # and this exercises the plain serialization route only.
    payload = build_preview_payload(_NestedValuesAdapter(), "SELECT * FROM events", 5)

    # The invariant the job depends on: SQLAlchemy encodes this with bare
    # `json.dumps` when it writes `ScanPreviewJob.result_summary`.
    json.dumps(payload)

    row = payload["rows"][0]
    # Asserting the rendered text, not just "dumps did not raise": a
    # `json.dumps(..., default=str)` patch would also stop the crash but would
    # spell the instant "2026-04-12 10:30:00", diverging from the isoformat the
    # preview has always emitted for a top-level datetime.
    assert row["created_at"] == "2026-04-12T10:30:00"
    assert row["ids"] == ["2026-04-12T10:30:00"]
    assert row["tags"] == {"a": "2026-04-12T10:30:00"}
    # Decimal keeps the `str()` rendering a top-level Decimal has always had;
    # coercing it to a float here would change a displayed value, not fix a crash.
    assert row["amounts"] == ["1.50"]
    assert row["trace"] == "2f1d1b1e-0000-4000-8000-000000000001"
    # A tuple becomes an array rather than the Python repr "(55.7, 37.6)" it
    # used to render as. That is forced by the renderer: `json.dumps` already
    # writes a NESTED tuple as an array, so stringifying tuples instead would
    # have changed `format_json_path_value` output for values that work today.
    assert row["location"] == [55.7, 37.6]
    # A JSON column is still decoded into real nested data, not re-stringified.
    assert row["payload"]["extra"]["key"] == "TASK-123"


class _DiverseNestedValuesAdapter:
    """Five rows over two pages, each carrying an ``Array(DateTime)``.

    More rows than the limit, so `_select_diverse_preview_rows` runs its feature
    pass and renders every cell through ``format_json_path_value`` — the second,
    independent place a nested ``datetime`` used to abort the job.
    """

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="created_at", type_name="DateTime"),
            ColumnInfo(name="page", type_name="String"),
            ColumnInfo(name="ids", type_name="Array(DateTime)"),
        ]

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        **_kwargs: object,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        pages = ["main", "main", "pricing", "main", "main"]
        return (
            ["created_at", "page", "ids"],
            [
                (datetime(2026, 4, 12, 10, index), page, [SAMPLED_AT])
                for index, page in enumerate(pages)
            ],
        )

    def close(self) -> None:
        return None


def test_build_preview_payload_survives_nested_values_in_the_diversity_pass() -> None:
    payload = build_preview_payload(_DiverseNestedValuesAdapter(), "SELECT * FROM events", 2)

    json.dumps(payload)

    assert len(payload["rows"]) == 2
    # The diversity pass still does its job: both pages are represented.
    assert {row["page"] for row in payload["rows"]} == {"main", "pricing"}
    assert [row["ids"] for row in payload["rows"]] == [["2026-04-12T10:30:00"]] * 2


def test_format_json_path_value_renders_a_container_with_non_string_keys() -> None:
    """The case ``json.dumps(..., default=str)`` would NOT have fixed.

    ``default`` is consulted for values only, never for dict keys, so a
    ClickHouse ``Map(Date, String)`` would still raise ``TypeError: keys must be
    str, int, float, bool or None``. This pins the chosen design (rebuild the
    tree) rather than the symptom.
    """
    rendered = format_json_path_value({date(2026, 1, 1): SAMPLED_AT})

    assert rendered == '{"2026-01-01": "2026-04-12T10:30:00"}'


def test_format_json_path_value_still_renders_json_native_values_unchanged() -> None:
    """The claim the fix rests on: it turns crashes into text and nothing else.

    These renderings feed variable values and metric sample values, which the
    drift detector compares across runs — two spellings of one value would read
    as drift forever.
    """
    assert format_json_path_value({"b": 1, "a": [1, 2]}) == '{"a": [1, 2], "b": 1}'
    assert format_json_path_value('{"b": 1, "a": [1, 2]}') == '{"a": [1, 2], "b": 1}'
    assert format_json_path_value([1, "x", True, None]) == '[1, "x", true, null]'
    assert format_json_path_value(SAMPLED_AT) == "2026-04-12 10:30:00"


class _BrokenDiscoveryAdapter:
    """Native JSON path discovery fails the way a broken adapter fails.

    An ``AttributeError`` from inside ``get_json_path_samples`` (a client that
    was never connected, a renamed attribute) is indistinguishable from a
    missing method to ``except AttributeError`` — which is why the old fallback
    could not tell "this adapter cannot discover paths" from "this adapter is
    broken" and answered both with a local sample.
    """

    def __init__(self) -> None:
        self.preview_row_calls: list[int] = []

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="event_name", type_name="String"),
            ColumnInfo(name="payload", type_name="JSON"),
        ]

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        **_kwargs: object,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self.preview_row_calls.append(limit)
        return (["event_name", "payload"], [("purchase", {"locale": "en"})])

    def get_json_path_samples(
        self,
        base_query: str,
        json_columns: list[str],
        **_kwargs: object,
    ) -> dict[str, dict[str, list[object]]]:
        msg = "'NoneType' object has no attribute 'query'"
        raise AttributeError(msg)

    def close(self) -> None:
        return None


def test_json_path_discovery_does_not_swallow_an_adapter_error() -> None:
    adapter = _BrokenDiscoveryAdapter()

    with pytest.raises(AttributeError):
        build_json_paths_payload(adapter, "SELECT * FROM events", [])

    # The stronger half of the assertion: the local row sampling must not have
    # run either. Reinstating the fallback with a log would still fail here,
    # whereas "it raises" alone could be satisfied by re-raising after sampling.
    assert adapter.preview_row_calls == []
