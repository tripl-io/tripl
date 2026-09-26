"""Automatic "Release <version>" chart annotations.

After a scan stores its per-version series, every app version that was absent
at the start of the loaded slice and then ACTIVATED inside it
(:mod:`tripl.services.version_activation`'s gate) gets one project-wide chart
annotation at its activation bucket, with ``source = release``. Inert when the
scan has no ``app_version_column``.

Loads the same slice the release-regression pass does — anchored on the newest
STORED bucket, ``2 * window_days`` deep — and the same volume rows: event-level
counts, with "Other" in the traffic denominator but never a version of its own.

Idempotent by construction: the label is the de-dup key, held permanently by
the partial unique index ``uq_chart_annotation_release_label``, and the insert is
``ON CONFLICT DO NOTHING`` against it. So a re-run, a replay, or a second scan of
the same project seeing the same rollout adds nothing. A release marker a person
deletes comes back on the next scan while its activation is still inside the
loaded slice, as any re-derived row would.

Best-effort: each insert runs in its own SAVEPOINT, and a failure there is
logged and rolled back to it — one bad row cannot drop the other markers, nor
fail the collection that produced the data. A version whose label would exceed
the 200-character label column is skipped with a warning.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import String, select, text
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from tripl.core.analyzers.release_regression import DEFAULT_WINDOW_DAYS
from tripl.models.chart_annotation import RELEASE_ANNOTATION_UNIQUE_WHERE, ChartAnnotation
from tripl.models.domain_enums import ChartAnnotationSource
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.scan_config import ScanConfig
from tripl.services.release_annotations import (
    RELEASE_ANNOTATION_COLOR,
    release_annotation_label,
    releases_to_annotate,
)
from tripl.services.version_activation import compile_prerelease_pattern, resolve_share_min

logger = logging.getLogger(__name__)

RELEASE_ANNOTATION_LOOKBACK = timedelta(days=DEFAULT_WINDOW_DAYS * 2)

# ``chart_annotations.label`` is String(200); ``breakdown_value`` holds up to 500.
_LABEL_TYPE = ChartAnnotation.__table__.c.label.type
RELEASE_LABEL_MAX_LENGTH: int = (
    _LABEL_TYPE.length if isinstance(_LABEL_TYPE, String) and _LABEL_TYPE.length else 200
)


def _load_version_series(
    session: Session, config: ScanConfig
) -> tuple[dict[str, dict[datetime, int]], dict[datetime, int]]:
    """Per-version and total event-level traffic by bucket, for the loaded slice."""
    latest_bucket = session.execute(
        select(sa_func.max(EventMetricBreakdown.bucket)).where(
            EventMetricBreakdown.scan_config_id == config.id,
            EventMetricBreakdown.breakdown_column == config.app_version_column,
        )
    ).scalar()
    if latest_bucket is None:
        return {}, {}
    rows = session.execute(
        select(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
            sa_func.sum(EventMetricBreakdown.count),
        )
        .where(
            EventMetricBreakdown.scan_config_id == config.id,
            EventMetricBreakdown.breakdown_column == config.app_version_column,
            # Event-level rows only: the per-type rows re-count the same events.
            EventMetricBreakdown.event_id.is_not(None),
            EventMetricBreakdown.bucket >= latest_bucket - RELEASE_ANNOTATION_LOOKBACK,
        )
        .group_by(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
        )
    ).all()

    by_version: dict[str, dict[datetime, int]] = {}
    all_traffic: dict[datetime, int] = {}
    for version, is_other, bucket, count in rows:
        count = int(count or 0)
        all_traffic[bucket] = all_traffic.get(bucket, 0) + count
        if is_other or not version:
            continue
        version_buckets = by_version.setdefault(version, {})
        version_buckets[bucket] = version_buckets.get(bucket, 0) + count
    return by_version, all_traffic


def _sync_release_annotations(session: Session, config: ScanConfig) -> int:
    """Create the release markers this scan's version series calls for.

    Returns how many rows were actually inserted (a conflict inserts none).
    Never commits: it runs inside the collection's transaction.
    """
    if not config.app_version_column:
        return 0
    by_version, all_traffic = _load_version_series(session, config)
    if not by_version:
        return 0

    releases = releases_to_annotate(
        by_version,
        all_traffic,
        share_min=resolve_share_min(config.app_version_active_share_min),
        prerelease_pattern=compile_prerelease_pattern(config.app_version_prerelease_pattern),
    )
    if not releases:
        return 0
    return _insert_release_annotations(session, config, releases)


def _insert_release_annotations(
    session: Session, config: ScanConfig, releases: dict[str, datetime]
) -> int:
    """Insert one marker per release, each in its own SAVEPOINT.

    A failing row (a constraint the model does not know about, a driver error)
    is logged and rolled back to its own savepoint, so it can neither drop the
    markers around it nor fail the collection that produced the data.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert = sqlite_insert if dialect == "sqlite" else postgresql_insert
    created = 0
    for version, bucket in sorted(releases.items(), key=lambda item: item[1]):
        label = release_annotation_label(version)
        if len(label) > RELEASE_LABEL_MAX_LENGTH:
            # A version value may be up to 500 characters; the label column holds
            # 200. Truncating would turn two long versions into one marker, so
            # the release is skipped instead.
            logger.warning(
                "release annotation skipped, label too long: scan=%s version_length=%d",
                config.id,
                len(version),
            )
            continue
        statement = (
            insert(ChartAnnotation)
            .values(
                id=uuid.uuid4(),
                project_id=config.project_id,
                scope_type=None,
                scope_ref=None,
                bucket=bucket,
                label=label,
                description=(
                    f"App version {version} reached its active share of traffic "
                    f"({config.app_version_column})."
                ),
                color=RELEASE_ANNOTATION_COLOR,
                source=ChartAnnotationSource.release.value,
                url=None,
                created_by_user_id=None,
            )
            .on_conflict_do_nothing(
                index_elements=["project_id", "label"],
                index_where=text(RELEASE_ANNOTATION_UNIQUE_WHERE),
            )
            .returning(ChartAnnotation.id)
        )
        try:
            with session.begin_nested():
                inserted = session.execute(statement).scalar_one_or_none()
        except SQLAlchemyError:
            logger.exception("release annotation insert failed: scan=%s label=%r", config.id, label)
            continue
        if inserted is not None:
            created += 1
    return created
