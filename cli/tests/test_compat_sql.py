from datetime import date

import pytest

from tripl_cli.compat_sql import compile_model, compile_preview

RULE = {"event": "old", "platform": "ios", "min_build": 1, "max_build": 2}
MAPPING = {
    "table": "events",
    "event_column": "name",
    "build_column": "build",
    "platform_column": "os",
    "time_column": "ts",
    "rules": [RULE],
}


@pytest.mark.parametrize(
    "patch",
    [
        {"table": "db.events WHERE 1"},
        {"event_column": "name AS secret"},
        {"time_column": "time()"},
        {"rules": []},
        {"rules": [{**RULE, "max_build": False}]},
        {"rules": [{**RULE, "max_build": 0}]},
        {"rules": [{**RULE, "min_version": 1}]},
        {"unknown_setting": 1},
    ],
)
def test_invalid_mapping(patch: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        compile_model({**MAPPING, **patch}, "order")


@pytest.mark.parametrize("end", [date(2026, 1, 1), date(2026, 2, 2)])
def test_bounded_preview(end: date) -> None:
    with pytest.raises(ValueError):
        compile_preview(MAPPING, "order", date(2026, 1, 1), end)


def test_mixed_rollout_on_clickhouse() -> None:
    """Runs on the CI ClickHouse service or the developer's clickhouse-local binary."""
    import json
    import os
    import shutil
    import subprocess
    from urllib.request import Request, urlopen

    endpoint = os.environ.get("TRIPL_COMPAT_CLICKHOUSE_URL")
    binary = shutil.which("clickhouse")
    if not endpoint and not binary:
        pytest.skip("requires clickhouse-local or TRIPL_COMPAT_CLICKHOUSE_URL")
    source = """WITH events AS (
        SELECT * FROM values('name String,os String,build Int64,ts DateTime,id Nullable(String)',
        ('old', 'ios', 1, '2026-01-01 08:00:00', NULL),
        ('old', 'ios', 2, '2026-01-01 08:01:00', 'A'),
        ('new', 'ios', 2, '2026-01-01 08:02:00', 'A'),
        ('new', 'ios', 2, '2026-01-01 08:03:00', ''),
        ('new', 'ios', 3, '2026-01-01 08:04:00', NULL),
        ('old', 'ios', 4, '2026-01-01 08:05:00', 'B'),
        ('other', 'ios', 1, '2026-01-01 08:06:00', NULL),
        ('old', 'ios', 1, '2026-01-02 12:00:00', NULL))
    )"""

    def rows(sql: str) -> list[dict[str, object]]:
        query = f"{source} SELECT * FROM ({sql}) FORMAT JSONEachRow"
        if endpoint:
            with urlopen(Request(endpoint, data=query.encode()), timeout=45) as response:
                data = response.read().decode()
        else:
            assert binary is not None
            data = subprocess.check_output(
                [binary, "local", "--session_timezone", "UTC", "--max_threads", "1", "-q", query],
                text=True,
                timeout=45,
            )
        return [json.loads(line) for line in data.splitlines()]

    mapping = {**MAPPING, "rules": [RULE, {**RULE, "event": "new", "min_build": 2, "max_build": 3}]}
    start, end = date(2026, 1, 1), date(2026, 1, 2)
    keyed = {**mapping, "dedup_column": "id"}
    assert len(rows(compile_model(keyed, "order"))) == 4
    preview = rows(compile_preview(keyed, "order", start, end))
    assert [int(str(row["logical_rows"])) for row in preview] == [1, 1, 1, 0]
    assert [int(str(row["physical_rows"])) for row in preview] == [1, 3, 1, 1]
    assert int(str(preview[1]["duplicate_rows"])) == 1
    assert int(str(preview[1]["ambiguous_rows"])) == 1
    assert int(str(preview[3]["unmatched_rows"])) == 1
    unkeyed = rows(compile_preview(mapping, "order", start, end))
    assert int(str(unkeyed[1]["ambiguous_rows"])) == 3
    assert int(str(unkeyed[1]["logical_rows"])) == 0
