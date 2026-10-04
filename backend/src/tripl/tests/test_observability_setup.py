"""Regression tests for cross-process metrics and optional tracing setup."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from celery.signals import task_failure, task_postrun, task_prerun

from tripl.observability.metrics import (
    celery_tasks_total,
    install_celery_instrumentation,
    render_metrics,
)
from tripl.observability.tracing import _trace_export_endpoint


def test_worker_counter_is_visible_to_api_process(tmp_path: Path, monkeypatch) -> None:
    """The API scrape must include samples written by a separate worker process."""
    env = os.environ.copy()
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from tripl.observability.metrics import scan_runs_total; "
            "scan_runs_total.labels(status='batch10_worker').inc(3)",
        ],
        env=env,
        check=True,
    )
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

    body, _ = render_metrics()

    assert b'tripl_scan_runs_total{status="batch10_worker"} 3.0' in body


def test_multiprocess_shard_names_carry_the_hostname(tmp_path: Path) -> None:
    """Containers share the directory but not PIDs, so shards must not be named by PID alone."""
    env = os.environ.copy()
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, socket; "
            "from tripl.observability.metrics import scan_runs_total; "
            "scan_runs_total.labels(status='batch10_shard').inc(); "
            "print(f'{socket.gethostname()}-{os.getpid()}')",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    identifier = completed.stdout.strip()

    assert [path.name for path in tmp_path.iterdir()] == [f"counter_{identifier}.db"]


def test_failed_celery_task_is_counted_once() -> None:
    install_celery_instrumentation()
    install_celery_instrumentation()

    class Task:
        name = "batch10.failure_count"

    task = Task()
    before = celery_tasks_total.labels(task=task.name, status="failure")._value.get()

    task_prerun.send(sender=task, task_id="batch10-failure", task=task)
    task_failure.send(sender=task, task_id="batch10-failure")
    task_postrun.send(sender=task, task_id="batch10-failure", task=task, state="FAILURE")

    assert celery_tasks_total.labels(task=task.name, status="failure")._value.get() == before + 1


def test_otlp_http_base_url_gains_trace_path() -> None:
    assert _trace_export_endpoint("http://collector:4318") == "http://collector:4318/v1/traces"
    assert _trace_export_endpoint("http://collector:4318/") == "http://collector:4318/v1/traces"
    assert _trace_export_endpoint("http://collector:4318/custom") == "http://collector:4318/custom"
