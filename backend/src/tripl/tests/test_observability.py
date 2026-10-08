"""Smoke tests for the Prometheus /metrics endpoint and registry."""

from __future__ import annotations

import importlib.util

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tripl.config import settings
from tripl.services import app_settings_service


@pytest.fixture
def metrics_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The app `tripl.main` builds with Prometheus metrics enabled.

    `tripl.main` decides whether to register `/metrics` at import time, so the
    fixture runs that module again into a module object of its own, with the
    flag set on the shared `settings`. Reloading `tripl.config` instead would
    replace `tripl.config.settings`: every test module that imported it would
    patch a stale object for the rest of the worker's run, while the code under
    test read the new one (the hosted warehouse-host checks among them).
    """
    monkeypatch.setattr(settings, "prometheus_metrics_enabled", True)
    # The flag above, not whatever overrides the test database holds.
    monkeypatch.setattr(app_settings_service, "apply_startup_service_overrides", lambda: [])
    spec = importlib.util.find_spec("tripl.main")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app: FastAPI = module.app
    return app


def test_metrics_app_replaces_neither_settings_nor_the_app(metrics_app: FastAPI) -> None:
    import tripl.config
    import tripl.main

    assert tripl.config.settings is settings
    assert tripl.main.app is not metrics_app


def test_metrics_endpoint_exposes_prometheus_text(metrics_app: FastAPI) -> None:
    client = TestClient(metrics_app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "tripl_celery_task_seconds" in body
    assert "tripl_anomalies_detected_total" in body
    # Counters incremented from worker tasks must surface in the same registry.
    assert "tripl_scan_runs_total" in body
    assert "tripl_alert_deliveries_total" in body
    assert "tripl_schema_drifts_detected_total" in body


def test_metrics_endpoint_disabled_by_default() -> None:
    from tripl.main import app

    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 404


def test_schema_drift_metric_records_per_drift_type() -> None:
    """_record_drift_metrics bumps schema_drifts_detected_total by row."""
    from tripl.observability.metrics import schema_drifts_detected_total
    from tripl.worker.tasks.metrics.schema_drift import _record_drift_metrics

    before = schema_drifts_detected_total.labels(drift_type="new_field")._value.get()
    _record_drift_metrics(
        [
            {"drift_type": "new_field"},
            {"drift_type": "new_field"},
            {"drift_type": "type_changed"},
        ]
    )
    assert schema_drifts_detected_total.labels(drift_type="new_field")._value.get() == before + 2
    assert schema_drifts_detected_total.labels(drift_type="type_changed")._value.get() >= 1


def test_tracing_setup_is_noop_when_endpoint_blank() -> None:
    """No otel endpoint env → setup_*_tracing return False without raising."""
    from tripl.observability.tracing import setup_api_tracing, setup_worker_tracing

    # The test runtime has no OTEL_EXPORTER_OTLP_ENDPOINT, so both paths
    # should short-circuit. We don't need a real FastAPI app — the helper
    # returns before touching `app`.
    assert setup_api_tracing(None) is False  # type: ignore[arg-type]
    assert setup_worker_tracing() is False
