"""A distribution drift the dispatcher would skip is not readiness.

``load_scope_readiness`` answers "has this project any source data for this scope
at all", and the distribution half answered it by counting ANY collected
``DistributionDrift`` row. Every scored bucket is persisted, stable and minor
included (``metric_rows`` bands each PSI and writes them all), nothing prunes
them, and the only candidate builder selects the significant band — so a project
that watched a column for months without ever crossing the threshold, then
cleared ``distribution_drift_fields``, was told its scope was fine while nothing
could ever feed it again. The value-drift half of the same query has mirrored its
builder's filters since tripl-wkwv.1; this is the missing symmetry.

Both ends are pinned here, because the mirror can be broken from either side:
the probe, through the endpoint the Alerting tab polls, and
``_get_active_distribution_drift_candidates``, called on a sync session the way
the worker calls it. Fixtures are shared with the tests that own them rather than
forked — ``test_alerting_scope_readiness`` for the async project/scan/readiness
helpers, ``test_metrics_tasks`` for the sync scan-config seed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import DistributionDriftBand
from tripl.tests.test_alerting_scope_readiness import (
    _add,
    _make_project,
    _make_scan,
    _readiness,
)
from tripl.tests.test_metrics_tasks import _create_scan_config
from tripl.worker.tasks.metrics import signals as metrics_signals

# Three consecutive daily buckets, oldest first. The band under test and the
# bucket are independent axes here: the collection order below always puts the
# significant row on the NEWEST bucket, so these tests keep passing if the probe
# is ever tightened to the builder's latest-bucket clause as well.
_OLDEST = datetime(2026, 8, 19, tzinfo=UTC)
_MIDDLE = _OLDEST + timedelta(days=1)
_NEWEST = _OLDEST + timedelta(days=2)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A sync sqlite session, as ``test_metrics_tasks`` builds it for the worker."""
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_readiness.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _drift_row(
    scan_config_id: uuid.UUID,
    *,
    band: DistributionDriftBand,
    bucket: datetime,
    psi: float,
    field_name: str = "platform",
) -> DistributionDrift:
    """One collected PSI bucket, with the band ``metric_rows`` derives from that PSI.

    ``psi`` and ``band`` are passed together and kept consistent on purpose: the
    stored band is what every reader filters on, and a row whose band disagreed
    with its score would prove nothing about either side of the mirror.
    """
    return DistributionDrift(
        id=uuid.uuid4(),
        scan_config_id=scan_config_id,
        event_type_id=None,
        field_name=field_name,
        bucket=bucket,
        psi=psi,
        band=band.value,
        baseline_total=1000,
        current_total=900,
        top_movers=[],
    )


@pytest.mark.asyncio
async def test_scope_readiness_ignores_distribution_drifts_the_dispatcher_would_skip(
    client: AsyncClient,
) -> None:
    """Stable and minor rows are collected data no dispatch can ever select.

    The distribution mirror of
    ``test_scope_readiness_ignores_value_drifts_the_dispatcher_would_skip``. The
    second act is not decoration: it proves this fixture can reach True at all,
    so the first assertion cannot pass on a mistyped slug or a scan that never
    got linked to the project.
    """
    # Arrange — the operator cleared the field list, so only collected rows can
    # carry readiness, and every row this project ever collected scored below
    # the band the alert pipeline acts on.
    await _make_project(client, "readiness-quiet-psi")
    scan_config_id = await _make_scan(client, "readiness-quiet-psi", distribution_drift_fields=[])
    await _add(
        _drift_row(scan_config_id, band=DistributionDriftBand.stable, bucket=_OLDEST, psi=0.03),
        _drift_row(scan_config_id, band=DistributionDriftBand.minor, bucket=_MIDDLE, psi=0.12),
    )

    # Act
    readiness = await _readiness(client, "readiness-quiet-psi")

    # Assert — drop the band clause from ``distribution_collected`` and these two
    # rows alone report a live scope, which is the bug this file exists for.
    assert readiness["distribution_drift"] is False

    # Act again — one row of the band the builder does select.
    await _add(
        _drift_row(
            scan_config_id,
            band=DistributionDriftBand.significant,
            bucket=_NEWEST,
            psi=0.41,
        )
    )

    # Assert
    assert (await _readiness(client, "readiness-quiet-psi"))["distribution_drift"] is True


def test_the_dispatcher_builds_no_candidate_from_stable_or_minor_rows(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The other end of the mirror: only a significant row at the newest bucket alerts.

    The probe above is only as right as the builder it copies, and the band
    filter can be broken from either side — widen this one and the readiness
    notice starts appearing on projects that do alert. The third act pins the
    clause the probe deliberately does NOT copy: a newer all-stable bucket
    retires the candidate, which is why "collected" cannot mean "will fire now".
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        session.add_all(
            [
                _drift_row(config.id, band=DistributionDriftBand.stable, bucket=_OLDEST, psi=0.03),
                _drift_row(config.id, band=DistributionDriftBand.minor, bucket=_MIDDLE, psi=0.12),
            ]
        )
        session.commit()

        # Act + Assert — a history that never crossed the threshold yields nothing.
        assert metrics_signals._get_active_distribution_drift_candidates(session, config) == {}

        # Act + Assert — the same field, now genuinely shifted.
        significant = _drift_row(
            config.id, band=DistributionDriftBand.significant, bucket=_NEWEST, psi=0.41
        )
        session.add(significant)
        session.commit()
        candidates = metrics_signals._get_active_distribution_drift_candidates(session, config)
        assert [candidate.id for candidate in candidates.values()] == [significant.id]

        # Act + Assert — one quiet bucket later the candidate is gone, because the
        # builder reads the latest bucket only.
        session.add(
            _drift_row(
                config.id,
                band=DistributionDriftBand.stable,
                bucket=_NEWEST + timedelta(days=1),
                psi=0.02,
            )
        )
        session.commit()
        assert metrics_signals._get_active_distribution_drift_candidates(session, config) == {}


@pytest.mark.asyncio
async def test_scope_readiness_asks_ever_not_now_about_an_older_significant_row(
    client: AsyncClient,
) -> None:
    """A significant row behind a quieter newest bucket still counts as readiness.

    This is the one place the probe knowingly outruns the builder, and it is the
    same "ever, not now" reading that leaves the value-drift snooze expiry
    unmirrored: the project has produced an alerting-grade drift, so the notice
    would be accusing it of something untrue, and asking otherwise would cost a
    per-scan-config correlated ``MAX(bucket)`` on a polled endpoint. If that
    trade is ever revisited, this assertion and the comment on
    ``distribution_collected`` that promises it move together.
    """
    # Arrange — the shape the sync test above ends on: a real drift, then a bucket
    # that settled back down.
    await _make_project(client, "readiness-settled-psi")
    scan_config_id = await _make_scan(client, "readiness-settled-psi", distribution_drift_fields=[])
    await _add(
        _drift_row(
            scan_config_id, band=DistributionDriftBand.significant, bucket=_MIDDLE, psi=0.41
        ),
        _drift_row(scan_config_id, band=DistributionDriftBand.stable, bucket=_NEWEST, psi=0.02),
    )

    # Act
    readiness = await _readiness(client, "readiness-settled-psi")

    # Assert
    assert readiness["distribution_drift"] is True
