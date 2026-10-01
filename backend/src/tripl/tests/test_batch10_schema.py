"""Regression checks for the migration and ORM schema parity fixes."""

import runpy
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alembic import context
from alembic.config import Config
from sqlalchemy import DateTime

from tripl.models import Base

ALEMBIC_DIR = Path(__file__).resolve().parents[3] / "alembic"


def test_schema_indexes_match_migrated_contract() -> None:
    expected = {
        "search_documents": {"ix_search_documents_branch_id"},
        "event_photo_comments": {"ix_event_photo_comment_photo_created"},
        "chart_annotations": {
            "ix_chart_annotation_project_bucket",
            "ix_chart_annotation_scope",
        },
        "plan_branch_approvals": {"ix_plan_branch_approvals_branch_id"},
        "plan_branch_reviewers": {"ix_plan_branch_reviewers_branch_id"},
    }
    for table_name, names in expected.items():
        actual = {index.name for index in Base.metadata.tables[table_name].indexes}
        assert names <= actual, table_name

    removed = {
        "metric_anomalies": "ix_metric_anomaly_scope_bucket",
        "coverage_metrics": "ix_coverage_metric_config_bucket",
        "metric_breakdown_anomalies": "ix_metric_breakdown_anomaly_scope_bucket",
        "release_regressions": "ix_release_regression_scan_scope",
        "alert_pending_items": "ix_alert_pending_item_destination",
        "event_photo_comments": "ix_event_photo_comment_event",
        "variable_values": "ix_variable_values_variable",
    }
    for table_name, name in removed.items():
        actual = {index.name for index in Base.metadata.tables[table_name].indexes}
        assert name not in actual, table_name


def test_model_timestamps_match_migrated_timestamptz() -> None:
    for table_name in (
        "implementation_tickets",
        "project_branch_settings",
        "project_tracker_configs",
    ):
        for column_name in ("created_at", "updated_at"):
            column_type = Base.metadata.tables[table_name].c[column_name].type
            assert isinstance(column_type, DateTime)
            assert column_type.timezone is True, (table_name, column_name)


def test_alembic_env_accepts_percent_encoded_password() -> None:
    url = "postgresql+asyncpg://tripl:p%40ss@db/tripl"
    seen: list[str] = []
    with (
        patch("tripl.config.settings", SimpleNamespace(database_url=url)),
        patch.object(context, "config", Config(), create=True),
        patch.object(context, "is_offline_mode", return_value=True, create=True),
        patch.object(
            context,
            "configure",
            side_effect=lambda **kwargs: seen.append(kwargs["url"]),
            create=True,
        ),
        patch.object(context, "begin_transaction", return_value=nullcontext(), create=True),
        patch.object(context, "run_migrations", create=True),
    ):
        runpy.run_path(str(ALEMBIC_DIR / "env.py"))
    assert seen == [url]
