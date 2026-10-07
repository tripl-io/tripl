"""The platform and app-version rows a demo stores beside its hourly volumes.

One function for both writers — the seeder's backfill
(``builders.warehouse``) and the runtime tick's append
(``worker.tasks.demo_runtime``) — so an hour holds the same rows whichever of
them wrote it. They are also the rows a scheduled collection writes back when
it re-reads that hour: the demo's synthetic source serves the same
:class:`~tripl.core.adapters.synthetic_traffic.DemoTraffic`.

Rows are plain dicts for a core ``insert(EventMetricBreakdown)``: one per event,
bucket and value, plus the per-event-type rollups a collection stores next to
them (``event_id`` NULL), which the event-type and project-total views read.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from tripl.core.adapters.synthetic_traffic import DemoTraffic

# The synthetic ``events`` table's reserved dimensions, which the demo scan
# designates as its platform and app-version columns.
PLATFORM_COLUMN = "platform"
APP_VERSION_COLUMN = "app_version"


@dataclass(frozen=True)
class EventVolume:
    """One event's stored volume in one hour.

    ``count`` is the stored total; ``excess`` is the part of it injected on top
    of the ordinary volume (the seeded spike, a promo send), split by
    ``excess_shares`` — or by the hour's own platform mix when that is ``None``.
    """

    event_id: uuid.UUID
    event_type_id: uuid.UUID
    name: str
    event_type: str
    base: int
    count: int
    excess: int = 0
    excess_shares: Mapping[str, float] | None = None


def breakdown_rows(
    traffic: DemoTraffic,
    bucket: datetime,
    volumes: Iterable[EventVolume],
    *,
    scan_config_id: uuid.UUID,
    platform_column: str | None = PLATFORM_COLUMN,
    version_column: str | None = APP_VERSION_COLUMN,
) -> list[dict[str, object]]:
    """Every breakdown row of one hour: per event first, then per event type.

    A ``None`` column is skipped, for a scan that no longer designates it.
    """
    event_rows: list[dict[str, object]] = []
    type_counts: dict[tuple[uuid.UUID, str, str], int] = {}
    for volume in volumes:
        splits: list[tuple[str, dict[str, int]]] = []
        if platform_column:
            splits.append(
                (
                    platform_column,
                    traffic.platform_counts(
                        bucket,
                        event_name=volume.name,
                        event_type=volume.event_type,
                        base=volume.base,
                        count=volume.count - volume.excess,
                        excess=volume.excess,
                        excess_shares=volume.excess_shares,
                    ),
                )
            )
        if version_column:
            splits.append(
                (
                    version_column,
                    traffic.version_counts(bucket, event_name=volume.name, count=volume.count),
                )
            )
        for column, counts in splits:
            for value, count in counts.items():
                event_rows.append(
                    _row(scan_config_id, bucket, column, value, count, event_id=volume.event_id)
                )
                key = (volume.event_type_id, column, value)
                type_counts[key] = type_counts.get(key, 0) + count
    type_rows = [
        _row(scan_config_id, bucket, column, value, count, event_type_id=event_type_id)
        for (event_type_id, column, value), count in type_counts.items()
    ]
    return event_rows + type_rows


def _row(
    scan_config_id: uuid.UUID,
    bucket: datetime,
    column: str,
    value: str,
    count: int,
    *,
    event_id: uuid.UUID | None = None,
    event_type_id: uuid.UUID | None = None,
) -> dict[str, object]:
    return {
        "scan_config_id": scan_config_id,
        "event_id": event_id,
        "event_type_id": event_type_id,
        "bucket": bucket,
        "breakdown_column": column,
        "breakdown_value": value,
        "is_other": False,
        "count": count,
    }
