"""Celery startup and schedule regression tests."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest
from celery.schedules import crontab

from tripl.worker import celery_app as module


def test_beat_schedule_uses_fixed_utc_boundaries() -> None:
    assert module.celery_app.conf.timezone == "UTC"
    schedule = module.celery_app.conf.beat_schedule
    # The audit webhook poller (F20) is the one interval entry: it has no
    # wall-clock boundary, and a minute of lag shows in a SIEM.
    assert all(
        isinstance(entry["schedule"], crontab)
        for name, entry in schedule.items()
        if name != "deliver-audit-webhooks"
    )
    assert schedule["deliver-audit-webhooks"]["schedule"] == timedelta(seconds=30)
    assert schedule["check-metrics-due"]["schedule"].minute == set(range(0, 60, 5))
    assert schedule["flush-due-alert-digests"]["schedule"].minute == set(range(60))
    assert schedule["cleanup-scan-jobs"]["task"] == (
        "tripl.worker.tasks.maintenance.cleanup_scan_jobs"
    )
    assert schedule["cleanup-distribution-drifts"]["task"] == (
        "tripl.worker.tasks.maintenance.cleanup_distribution_drifts"
    )


def test_default_task_retry_delay_is_applied_to_tasks() -> None:
    assert module.celery_app.Task.default_retry_delay == 30
    assert "task_default_retry_delay" not in module.celery_app.conf


def test_worker_startup_applies_overrides_before_instrumentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(module, "apply_startup_service_overrides", lambda: calls.append("settings"))
    monkeypatch.setattr(module, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(module.settings, "prometheus_metrics_enabled", True)
    monkeypatch.setattr(module, "install_celery_instrumentation", lambda: calls.append("metrics"))
    monkeypatch.setattr(module, "setup_worker_tracing", lambda: calls.append("tracing"))
    module._configure_worker_runtime()
    assert calls == ["settings", "logging", "metrics", "tracing"]


def test_beat_startup_applies_overrides_and_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(module, "apply_startup_service_overrides", lambda: calls.append("settings"))
    monkeypatch.setattr(module, "configure_logging", lambda: calls.append("logging"))
    module._configure_beat_runtime()
    assert calls == ["settings", "logging"]


def test_celery_logging_signal_configures_application_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure = Mock()
    monkeypatch.setattr(module, "configure_logging", configure)
    module._configure_celery_logging()
    configure.assert_called_once_with()
