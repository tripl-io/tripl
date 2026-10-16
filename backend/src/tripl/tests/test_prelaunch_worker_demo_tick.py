"""Pre-launch fixes to the demo's worker locks and the demo recipe.

* The demo prune's advisory lock key is its own, apart from every other sweep's.
* The conversion metric is composed from its two event series by the real
  collector, so every value it stores is their ratio at that bucket.
* The feature branch's audit rows, its base revision and its comment share one
  timeline.

The synthetic tick these once pinned is gone: a recipe-5 demo is advanced by
the scheduler's ordinary collections over the demo warehouse
(``test_demo_v5_runtime``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.core.bucketing import to_utc
from tripl.models.audit_log import AuditLog
from tripl.models.event_metric import EventMetric
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.plan_branch_comment import PlanBranchComment
from tripl.models.plan_revision import PlanRevision
from tripl.models.project import Project
from tripl.services.demo import pipeline
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import demo_runtime
from tripl.worker.tasks.alert_flush import _ALERT_FLUSH_ADVISORY_LOCK_KEY
from tripl.worker.tasks.metrics.freshness_sweep import _FRESHNESS_SWEEP_ADVISORY_LOCK_KEY
from tripl.worker.tasks.metrics.schedule import (
    _DISPATCH_ADVISORY_LOCK_KEY,
    _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY,
)

_OPEN_BRANCH = "feature/checkout-funnel"


def test_the_prune_lock_key_is_its_own() -> None:
    keys = {
        _DISPATCH_ADVISORY_LOCK_KEY,
        _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY,
        _ALERT_FLUSH_ADVISORY_LOCK_KEY,
        _FRESHNESS_SWEEP_ADVISORY_LOCK_KEY,
        demo_runtime._PRUNE_DEMOS_ADVISORY_LOCK_KEY,
    }
    assert len(keys) == 5


# ── The demo recipe ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_demos_conversion_and_branch_timeline_are_what_they_claim(
    client: AsyncClient,
) -> None:
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    async with TestSessionLocal() as session:
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == slug))
        ).scalar_one()
        scan_config_id = await pipeline.demo_scan_config_id(session, project_id)

        # Purchase conversion is a ratio of two series the demo charts beside
        # it: a value a reader could not reproduce from them would be a lie.
        metric = (
            await session.execute(
                select(MetricDefinition).where(
                    MetricDefinition.project_id == project_id,
                    MetricDefinition.name == "purchase_conversion",
                )
            )
        ).scalar_one()
        values = {
            to_utc(bucket): value
            for bucket, value in (
                await session.execute(
                    select(MetricValue.bucket, MetricValue.value).where(
                        MetricValue.metric_definition_id == metric.id
                    )
                )
            ).tuples()
        }
        series: dict[uuid.UUID | None, dict[datetime, int]] = {}
        for event_id in (metric.numerator_event_id, metric.denominator_event_id):
            series[event_id] = {
                to_utc(bucket): count
                for bucket, count in (
                    await session.execute(
                        select(EventMetric.bucket, EventMetric.count).where(
                            EventMetric.scan_config_id == scan_config_id,
                            EventMetric.event_id == event_id,
                        )
                    )
                ).tuples()
            }

        branch = (
            await session.execute(
                select(PlanBranch).where(
                    PlanBranch.project_id == project_id,
                    PlanBranch.kind == BranchKind.working.value,
                    PlanBranch.name == _OPEN_BRANCH,
                )
            )
        ).scalar_one()
        assert branch.base_revision_id is not None
        revision = await session.get(PlanRevision, branch.base_revision_id)
        assert revision is not None
        comment = (
            await session.execute(
                select(PlanBranchComment).where(PlanBranchComment.branch_id == branch.id)
            )
        ).scalar_one()
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.project_id == project_id)))
            .scalars()
            .all()
        )

    numerator = series[metric.numerator_event_id]
    denominator = series[metric.denominator_event_id]
    assert values, "the collector stored no conversion"
    for bucket, value in values.items():
        assert denominator.get(bucket), bucket
        assert value == pytest.approx(numerator.get(bucket, 0) / denominator[bucket])

    # The audit log, Plan history and the branch list name the same instants:
    # opened (with its base revision), then edited, then discussed.
    created = next(
        row for row in rows if row.action == "plan_branch.create" and row.target_id == branch.id
    )
    edit = next(row for row in rows if row.action == "event.update" and row.branch_id == branch.id)
    opened = to_utc(branch.created_at)
    assert to_utc(created.created_at) == opened == to_utc(revision.created_at)
    assert opened < to_utc(edit.created_at) < to_utc(comment.created_at)
    assert to_utc(comment.created_at) <= datetime.now(UTC)
    # Connecting the warehouse and setting up the catalog and alerting came first.
    set_up = [
        row
        for row in rows
        if row.action
        in {
            "scan_config.create",
            "fact_table.create",
            "metric_definition.create",
            "alert_destination.create",
            "alert_rule.create",
        }
    ]
    assert set_up
    assert all(to_utc(row.created_at) < opened for row in set_up)
