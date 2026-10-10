"""One main-branch lookup for the worker and ``core``, and a metrics package without a fake API.

``core.plan_scope.main_branch_id`` replaced five copies of the same query: the
worker's own module, a second one inside the event generator (``core`` may not
import the worker) and three inline in the search reindex and the weekly
digest.

``tripl.worker.tasks.metrics`` re-exported dozens of private helpers nobody read
and lent ``send_alert_delivery`` to tests through ``__getattr__``. It now exposes
only the tasks importing it registers; everything else comes from its own module.
"""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tripl.core import plan_scope
from tripl.core.analyzers import _event_generator_variables, event_generator
from tripl.models import Base
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.worker import search_reindex
from tripl.worker.tasks import alerts_messages, metrics, scan, scan_dry_run
from tripl.worker.tasks.metrics import _helpers, catalog_sync, generation
from tripl.worker.tasks.metrics import tasks as metrics_tasks
from tripl.worker.utils import event_types, job_status, name_warnings


def test_main_branch_id_finds_main_and_ignores_working_branches(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'plan_scope.db'}")
    Base.metadata.create_all(engine)
    project_id = uuid.uuid4()
    main_id = uuid.uuid4()
    try:
        with Session(engine) as session:
            assert plan_scope.main_branch_id(session, project_id) is None

            # SQLite does not enforce the project foreign key here, so the
            # branches need no project row (which would need an organization).
            session.add_all(
                [
                    PlanBranch(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        name="feature",
                        kind=BranchKind.working.value,
                        status=BranchStatus.draft.value,
                        description="",
                    ),
                    PlanBranch(
                        id=main_id,
                        project_id=project_id,
                        name="main",
                        kind=BranchKind.main.value,
                        status=BranchStatus.merged.value,
                        description="",
                    ),
                ]
            )
            session.flush()

            assert plan_scope.main_branch_id(session, project_id) == main_id
    finally:
        engine.dispose()


def test_every_sync_caller_uses_the_one_lookup() -> None:
    for module in (
        scan,
        scan_dry_run,
        catalog_sync,
        generation,
        metrics_tasks,
        event_types,
        name_warnings,
        search_reindex,
    ):
        assert module.main_branch_id is plan_scope.main_branch_id, module.__name__
    # These two keep a local variable of that name, so they import the module.
    assert alerts_messages.plan_scope is plan_scope
    assert event_generator.plan_scope is plan_scope
    assert not hasattr(_event_generator_variables, "resolve_main_branch_id")
    assert not hasattr(event_generator, "_resolve_main_branch_id")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("tripl.worker.plan_scope")


def test_the_metrics_package_exposes_only_its_registered_tasks() -> None:
    assert metrics.__all__ == ["check_metrics_due", "collect_metrics"]
    assert metrics.collect_metrics.name == "tripl.worker.tasks.metrics.collect_metrics"
    assert metrics.check_metrics_due.name == "tripl.worker.tasks.metrics.check_metrics_due"
    # The alert task is imported from ``tasks.alerts``, never through metrics.
    assert not hasattr(metrics, "send_alert_delivery")
    assert not hasattr(metrics_tasks, "send_alert_delivery")
    # One terminal-status tuple, the one the tasks enforce.
    assert not hasattr(_helpers, "TERMINAL_SCAN_JOB_STATUSES")
    assert metrics_tasks.TERMINAL_SCAN_JOB_STATUSES is job_status.TERMINAL_SCAN_JOB_STATUSES
