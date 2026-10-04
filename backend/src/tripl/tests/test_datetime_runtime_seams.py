"""Regression tests for batch 10 datetime and runtime seams."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

from tripl.db_config import postgres_connect_args
from tripl.models.base import UtcDateTime
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.services import migration_status_service


@pytest.mark.parametrize(
    "model",
    [
        EventMetric,
        EventMetricBreakdown,
        MetricValue,
        MetricValueBreakdown,
        MetricAnomaly,
        MetricBreakdownAnomaly,
    ],
)
def test_metric_buckets_restore_utc_on_sqlite(model: type) -> None:
    column_type = model.__table__.c.bucket.type
    assert isinstance(column_type, UtcDateTime)
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert column_type.process_result_value(naive, sqlite_dialect()) == naive.replace(tzinfo=UTC)


def test_postgres_sessions_are_pinned_to_utc() -> None:
    assert postgres_connect_args("postgresql+asyncpg://u:p@localhost/db") == {
        "server_settings": {"TimeZone": "UTC"}
    }
    assert postgres_connect_args("postgresql+psycopg://u:p@localhost/db") == {
        "options": "-c TimeZone=UTC"
    }
    assert postgres_connect_args("sqlite+aiosqlite:///:memory:") == {}


@pytest.mark.asyncio
async def test_migration_head_read_runs_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    async def fake_to_thread(function: object) -> str:
        calls.append(function)
        return "head"

    class FakeSession:
        async def run_sync(self, function: object) -> str:
            return "head"

    monkeypatch.setattr(migration_status_service.asyncio, "to_thread", fake_to_thread)
    status = await migration_status_service.get_migration_status(FakeSession())  # type: ignore[arg-type]
    assert calls == [migration_status_service.head_revision]
    assert status.up_to_date is True
