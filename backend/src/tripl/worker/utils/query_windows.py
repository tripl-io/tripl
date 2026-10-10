from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tripl.core.bucketing import to_utc

TimeWindow = tuple[datetime, datetime]


def resolve_lookback_window(
    *,
    time_column: str | None,
    lookback_hours: int | None,
    end: datetime | None = None,
) -> TimeWindow | None:
    if not time_column or lookback_hours is None:
        return None
    window_end = to_utc(end or datetime.now(UTC))
    return window_end - timedelta(hours=lookback_hours), window_end
