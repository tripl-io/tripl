"""One UTC normalizer: ``core.bucketing.to_utc``, plus ``optional_to_utc`` for a nullable column.

About twenty modules carried a local copy of "a naive datetime is UTC, an aware
one is converted", under eight names, and several only stamped a naive value
and kept any other offset as it was. These pin the shared helpers, a few of the
readers that now use them, and that no local copy comes back.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import tripl
from tripl.core.analyzers.attribution import release_line
from tripl.core.bucketing import optional_to_utc
from tripl.models.alert_rule import AlertRule
from tripl.schemas.scan_config import ScanMetricsReplayRequest
from tripl.schemas.time_guards import require_future_instant
from tripl.worker.tasks.alert_owner_notify import _rule_is_muted
from tripl.worker.tasks.metrics._helpers import _parse_task_datetime

PLUS_TWO = timezone(timedelta(hours=2))
TEN_UTC = datetime(2026, 9, 1, 10, tzinfo=UTC)


def test_optional_to_utc_passes_none_through_and_normalizes_the_rest() -> None:
    assert optional_to_utc(None) is None
    assert optional_to_utc(datetime(2026, 9, 1, 10)) == TEN_UTC
    converted = optional_to_utc(datetime(2026, 9, 1, 12, tzinfo=PLUS_TWO))
    assert converted == TEN_UTC
    assert converted is not None
    assert converted.tzinfo is UTC


def test_a_task_datetime_parses_to_utc_whatever_its_offset() -> None:
    for text in ("2026-09-01T10:00:00Z", "2026-09-01T12:00:00+02:00", "2026-09-01T10:00:00"):
        parsed = _parse_task_datetime(text)
        assert parsed == TEN_UTC
        assert parsed.tzinfo is UTC
    with pytest.raises(ValueError):
        _parse_task_datetime("not a time")


def test_a_replay_window_arrives_in_utc() -> None:
    body = ScanMetricsReplayRequest.model_validate(
        {"time_from": "2026-09-01T12:00:00+02:00", "time_to": "2026-09-01T13:00:00"}
    )
    assert body.time_from == TEN_UTC
    assert body.time_from.tzinfo is UTC
    assert body.time_to == datetime(2026, 9, 1, 13, tzinfo=UTC)


def test_a_future_instant_comes_back_in_utc() -> None:
    ahead = datetime.now(PLUS_TWO) + timedelta(days=1)

    instant = require_future_instant(ahead, field_name="muted_until")

    assert instant == ahead
    assert instant.tzinfo is UTC


def test_the_owner_notifier_reads_a_naive_rule_mute_as_utc() -> None:
    now = datetime.now(UTC)
    later = (now + timedelta(hours=1)).replace(tzinfo=None)
    earlier = (now - timedelta(hours=1)).replace(tzinfo=None)

    assert _rule_is_muted(AlertRule(muted_until=later), now)
    assert not _rule_is_muted(AlertRule(muted_until=earlier), now)
    assert not _rule_is_muted(AlertRule(muted_until=None), now)


def test_a_release_lead_is_measured_in_utc_and_garbage_drops_the_line() -> None:
    def payload(reached_at: object) -> dict[str, object]:
        return {
            "delta": -100,
            "release": {"version": "4.12", "reached_at": reached_at, "share": 0.38},
        }

    expected = "Release 4.12 reached 38% of traffic 3h before the drop"
    for reached_at in ("2026-09-01T07:00:00Z", "2026-09-01T09:00:00+02:00"):
        assert release_line(payload(reached_at), anomaly_bucket=TEN_UTC) == expected
    assert release_line(payload("yesterday"), anomaly_bucket=TEN_UTC) is None


# The bodies the retired copies were written with, the parameter renamed ``v``.
_COPY_BODIES = frozenset(
    {
        "return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)",
        "if v.tzinfo is None:\n    return v.replace(tzinfo=UTC)\nreturn v.astimezone(UTC)",
        "return v if v.tzinfo is not None else v.replace(tzinfo=UTC)",
        "return v if v.tzinfo else v.replace(tzinfo=UTC)",
        "return v.replace(tzinfo=UTC) if v.tzinfo is None else v",
        "if v is None:\n    return None\nif v.tzinfo is None:\n    return v.replace(tzinfo=UTC)\n"
        "return v.astimezone(UTC)",
    }
)

# ``to_utc`` itself, and the copy in a module that may import nothing from
# ``tripl`` (``test_monitors_summary.test_monitoring_utils_is_a_pure_leaf``).
_ALLOWED = {"core/bucketing.py::to_utc", "services/monitoring_utils.py::_utc_bucket"}


def test_no_module_keeps_its_own_utc_normalizer() -> None:
    root = Path(tripl.__file__).resolve().parent
    found: set[str] = set()
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            params = [arg.arg for arg in node.args.args if arg.arg not in ("self", "cls")]
            if len(params) != 1:
                continue
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            text = "\n".join(ast.unparse(statement) for statement in body)
            if re.sub(rf"\b{params[0]}\b", "v", text) in _COPY_BODIES:
                found.add(f"{path.relative_to(root).as_posix()}::{node.name}")

    # The walk cannot silently stop finding files.
    assert scanned > 200, scanned
    assert found == _ALLOWED
