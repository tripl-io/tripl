from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tripl_cli.install.shell import Command, CommandResult

BASE_URL = "http://tripl.test"
API_BASE = f"{BASE_URL}/api/v1"
API_KEY = "tk_r_test-key"

# backend/src/tripl/worker/tasks/metrics/tasks.py::METRICS_COLLECTION_MODE.
DISPATCHER_MODE = "metrics_collection"
# ...and METRICS_REPLAY_MODE, the one that carries chunk progress.
REPLAY_MODE = "metrics_replay"

_UNSET = object()


def api_time(moment: datetime) -> str:
    """Spell a timestamp the way the API spells it: aware UTC, ``Z``.

    Pydantic v2 serialises an aware UTC datetime as ``...T00:00:00Z``, so a
    fixture built with ``.isoformat()`` pins ``+00:00`` — bytes no tripl instance
    ever sends. That makes the golden samples, the documented samples and real
    output disagree three ways, on the exact field an operator string-compares.

    Deliberately NOT used inside ``replay_summary``: that block is
    ``ScanJob.result_summary``, a JSON blob the worker writes with
    ``datetime.isoformat()`` and no response model in between, so ``+00:00`` is
    what really arrives there. The two spellings coexisting is a documented fact
    about the API (website/docs/run/cli.md), not a defect to normalise away.
    """
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Cut every test off from the developer's own environment and home dir.

    Autouse because "remember to request the fixture" is precisely the rule that
    gets forgotten in the second test file. Without it a maintainer with
    TRIPL_API_KEY exported gets results CI does not, and a test that resolves
    config could read their real credentials out of ~/.config/tripl/config.toml.
    """
    for name in ("TRIPL_BASE_URL", "TRIPL_API_KEY", "TRIPL_URL", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    # Both, so Path.home() lands in the sandbox on POSIX and on Windows.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    yield


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the connection settings back, in the environment, after isolation.

    Lets `main(["doctor"])` resolve with no config file on disk, which is how
    every end-to-end test reaches the command without also testing config.py.
    """
    monkeypatch.setenv("TRIPL_BASE_URL", BASE_URL)
    monkeypatch.setenv("TRIPL_API_KEY", API_KEY)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    """Write a config.toml under tmp_path and hand back its path."""

    def _write(body: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(body, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


def make_project(slug: str = "prod", **overrides: Any) -> dict[str, Any]:
    project: dict[str, Any] = {
        "id": f"pid-{slug}",
        "slug": slug,
        "name": slug.title(),
        "description": "",
        "is_demo": False,
        "generation_status": "ready",
        "generation_stage": None,
        "generation_error": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "summary": make_summary(),
    }
    project.update(overrides)
    return project


def make_summary(**overrides: Any) -> dict[str, Any]:
    """A ProjectSummary as the backend serialises it (see openapi.json)."""
    summary: dict[str, Any] = {
        "event_count": 412,
        "active_event_count": 388,
        "archived_event_count": 24,
        "implemented_event_count": 300,
        "review_pending_event_count": 0,
        "event_type_count": 17,
        "scan_count": 4,
        "failing_scan_config_count": 0,
        "firing_monitor_count": 0,
        "monitoring_signal_count": 0,
        "alert_rule_count": 0,
        "alert_destination_count": 0,
        "variable_count": 0,
        "latest_scan_job": None,
        "latest_signal": None,
    }
    summary.update(overrides)
    return summary


def make_scan_config(
    config_id: str = "scan-1",
    *,
    name: str = "prod events",
    interval: str | None = "1h",
    time_column: str | None = "event_time",
    data_source_id: str = "ds-1",
    created_at: datetime | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    created = created_at if created_at is not None else datetime.now(UTC) - timedelta(days=30)
    config: dict[str, Any] = {
        "id": config_id,
        "project_id": "pid-prod",
        "name": name,
        "interval": interval,
        "time_column": time_column,
        "data_source_id": data_source_id,
        "base_query": "select 1",
        "created_at": api_time(created),
        "updated_at": api_time(created),
    }
    config.update(overrides)
    return config


def make_job(
    *,
    job_id: str = "job-1",
    status: str = "completed",
    at: datetime | None = None,
    result_summary: Any = _UNSET,
    error_message: str | None = None,
    time_to: datetime | None = None,
) -> dict[str, Any]:
    """One ScanJobResponse row. Dispatcher-stamped unless told otherwise."""
    stamp = at if at is not None else datetime.now(UTC)
    if result_summary is _UNSET:
        summary: Any = {"mode": DISPATCHER_MODE}
        if time_to is not None:
            # The worker's own spelling inside result_summary. See api_time.
            summary["time_to"] = time_to.isoformat()
    else:
        summary = result_summary
    return {
        "id": job_id,
        "scan_config_id": "scan-1",
        "status": status,
        "error_message": error_message,
        "result_summary": summary,
        "created_at": api_time(stamp),
        "started_at": api_time(stamp),
        "completed_at": api_time(stamp) if status in ("completed", "failed") else None,
        "updated_at": api_time(stamp),
    }


def replay_summary(
    *,
    chunks_total: int = 18,
    chunks_completed: int = 3,
    phase: str = "collecting",
    current_chunk_index: int | None = None,
    chunk_from: datetime | None = None,
    chunk_to: datetime | None = None,
    chunk_interval: str | None = "1d",
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> dict[str, Any]:
    """``_build_replay_progress_summary``'s payload, key for key.

    Reproduced rather than approximated - including the clamping and the
    100.0-when-total-is-zero rule - because the whole point of the progress lines
    is that they carry the worker's own numbers under the worker's own names.
    """
    safe_total = max(chunks_total, 0)
    safe_completed = min(max(chunks_completed, 0), safe_total)
    start = time_from if time_from is not None else datetime(2026, 7, 1, tzinfo=UTC)
    end = time_to if time_to is not None else datetime(2026, 7, 31, tzinfo=UTC)
    return {
        "mode": REPLAY_MODE,
        "time_from": start.isoformat(),
        "time_to": end.isoformat(),
        "catalog_sync_skipped": True,
        "replay_chunk_interval": chunk_interval or "whole-window",
        "replay_chunks_total": safe_total,
        "replay_chunks_completed": safe_completed,
        "replay_progress_percent": (
            round((safe_completed / safe_total) * 100, 1) if safe_total else 100.0
        ),
        "replay_progress_phase": phase,
        "replay_current_chunk_index": current_chunk_index,
        "replay_current_chunk_from": chunk_from.isoformat() if chunk_from else None,
        "replay_current_chunk_to": chunk_to.isoformat() if chunk_to else None,
    }


def make_replay_job(
    *,
    job_id: str = "job-91c2",
    status: str = "running",
    at: datetime | None = None,
    updated_at: datetime | None = None,
    error_message: str | None = None,
    **summary_overrides: Any,
) -> dict[str, Any]:
    """A ScanJobResponse carrying a real replay progress block."""
    stamp = at if at is not None else datetime.now(UTC)
    touched = updated_at if updated_at is not None else stamp
    return {
        "id": job_id,
        "scan_config_id": "scan-1",
        "status": status,
        "error_message": error_message,
        "result_summary": replay_summary(**summary_overrides),
        "created_at": api_time(stamp),
        "started_at": api_time(stamp),
        "completed_at": api_time(touched) if status in ("completed", "failed") else None,
        "updated_at": api_time(touched),
    }


def make_signal(
    *,
    scope_type: str = "project_total",
    scope_ref: str = "prod",
    state: str = "open",
    bucket: datetime | None = None,
    actual_count: float = 412.0,
    expected_count: float = 1180.0,
    stddev: float = 120.0,
    z_score: float = -6.1,
    direction: str = "drop",
    event_id: str | None = None,
    event_type_id: str | None = None,
    scan_config_id: str | None = "scan-1",
    incident_child: bool = False,
) -> dict[str, Any]:
    """One MetricSignalResponse. Note: no id, and no detection timestamp."""
    at = bucket if bucket is not None else datetime(2026, 7, 31, 19, 0, tzinfo=UTC)
    return {
        "scope_type": scope_type,
        "scope_ref": scope_ref,
        "state": state,
        "bucket": api_time(at),
        "actual_count": actual_count,
        "expected_count": expected_count,
        "stddev": stddev,
        "z_score": z_score,
        "direction": direction,
        "event_id": event_id,
        "event_type_id": event_type_id,
        "scan_config_id": scan_config_id,
        "incident_child": incident_child,
    }


def make_delivery(
    *,
    delivery_id: str = "del-4f21",
    status: str = "failed",
    channel: str = "slack",
    destination_name: str = "oncall",
    rule_name: str = "Checkout drop",
    scan_name: str = "prod events",
    error_message: str | None = "channel_not_found",
    at: datetime | None = None,
) -> dict[str, Any]:
    stamp = at if at is not None else datetime.now(UTC)
    return {
        "id": delivery_id,
        "project_id": "pid-prod",
        "scan_config_id": "scan-1",
        "scan_job_id": "job-91c2",
        "destination_id": "dest-1",
        "rule_id": "rule-1",
        "destination_name": destination_name,
        "rule_name": rule_name,
        "scan_name": scan_name,
        "status": status,
        "channel": channel,
        "matched_count": 3,
        "payload_snapshot": None,
        "error_message": error_message,
        "is_local": False,
        "is_simulated": False,
        "created_at": api_time(stamp),
        "updated_at": api_time(stamp),
        "sent_at": api_time(stamp) if status == "sent" else None,
    }


def make_data_source(
    source_id: str = "ds-1",
    *,
    name: str = "warehouse-prod",
    last_test_status: str | None = "success",
    last_test_at: datetime | None = None,
    last_test_message: str | None = None,
) -> dict[str, Any]:
    tested = last_test_at if last_test_at is not None else datetime.now(UTC)
    return {
        "id": source_id,
        "name": name,
        "db_type": "postgres",
        "host": "warehouse.internal",
        "port": 5432,
        "database_name": "analytics",
        "username": "tripl",
        "password_set": True,
        "is_synthetic": False,
        "last_test_status": last_test_status,
        "last_test_at": api_time(tested) if last_test_status is not None else None,
        "last_test_message": last_test_message,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def make_event_type(type_id: str = "et-1", name: str = "app.screen_view") -> dict[str, Any]:
    return {
        "id": type_id,
        "project_id": "pid-prod",
        "name": name,
        "display_name": name,
        "description": "",
        "color": "#000000",
        "order": 0,
        "field_definitions": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def make_event(
    event_id: str = "evt-1",
    *,
    name: str = "app.screen_view.viewed",
    status: str = "live",
    last_seen_at: datetime | None = None,
    drift_count: int = 0,
    field_values: list[dict[str, Any]] | None = None,
    meta_values: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """One ``EventListItemResponse`` / ``EventResponse`` row.

    The two schemas differ only in that the detail one embeds ``event_type``
    (an ``EventTypeBrief``) where the list one carries ``monitored``. One
    builder covers both because every key ``tripl events`` reads is in the
    intersection, and a second fixture would be a second place for the catalog's
    shape to drift.
    """
    return {
        "id": event_id,
        "project_id": "pid-prod",
        "event_type_id": "et-1",
        "event_type": {
            "id": "et-1",
            "name": "app.screen_view",
            "display_name": "Screen View",
            "color": "#000000",
        },
        "name": name,
        "description": "",
        "status": status,
        "reviewed": True,
        "order": 0,
        "owner_id": None,
        "sunset_at": None,
        "last_seen_at": api_time(last_seen_at) if last_seen_at else None,
        "drift_count": drift_count,
        "tags": [{"id": f"tag-{tag}", "name": tag} for tag in tags or []],
        "field_values": field_values or [],
        "meta_values": meta_values or [],
        "metric_breakdown_columns": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def make_field(
    field_id: str = "fd-1",
    *,
    name: str = "screen_name",
    field_type: str = "string",
    is_required: bool = True,
    sensitivity: str = "none",
    enum_options: list[Any] | None = None,
    order: int = 0,
) -> dict[str, Any]:
    """One ``FieldDefinitionResponse``. ``enum_options`` is ``array[Any] | null``."""
    return {
        "id": field_id,
        "event_type_id": "et-1",
        "name": name,
        "display_name": name.replace("_", " ").title(),
        "description": "",
        "field_type": field_type,
        "is_required": is_required,
        "enum_options": enum_options,
        "order": order,
        "sensitivity": sensitivity,
        "contract_regex": None,
        "contract_min_value": None,
        "contract_max_value": None,
        "contract_max_bad_rate": 0.0,
        "contract_required_max_null_rate": None,
    }


def make_variable(
    variable_id: str = "var-1",
    *,
    name: str = "cart_value",
    variable_type: str = "number",
    event_count: int = 12,
    open_drift_count: int = 0,
) -> dict[str, Any]:
    """One ``VariableResponse``."""
    return {
        "id": variable_id,
        "project_id": "pid-prod",
        "name": name,
        "description": "",
        "variable_type": variable_type,
        "allowed_values": [],
        "bindings": [],
        "sample_values": [],
        "event_names": [],
        "event_count": event_count,
        "context_count": 0,
        "high_context_count": 0,
        "low_context_count": 0,
        "open_drift_count": open_drift_count,
        "excluded_from_scans": False,
        "source_name": None,
    }


def make_branch(
    branch_id: str = "b-9f21",
    *,
    name: str = "checkout-redesign",
    kind: str = "working",
    status: str = "draft",
    ahead: int | None = 3,
    behind_base: bool | None = False,
) -> dict[str, Any]:
    """One ``PlanBranchResponse``. ``ahead``/``behind_base`` are nullable on the wire."""
    return {
        "id": branch_id,
        "project_id": "pid-prod",
        "name": name,
        "description": "",
        "kind": kind,
        "status": status,
        "base_revision_id": None,
        "created_by": None,
        "merged_by": None,
        "merged_at": None,
        "ahead": ahead,
        "behind_base": behind_base,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def make_search_result(
    entity_type: str = "event",
    *,
    entity_id: str = "evt-1",
    title: str = "app.screen_view.viewed",
    subtitle: str = "Screen View",
    confidence: float = 0.92,
    score: float = 12.5,
) -> dict[str, Any]:
    """One ``SearchResult``."""
    return {
        "id": entity_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": title,
        "subtitle": subtitle,
        "description": "",
        "snippet": "",
        "route_path": f"/projects/prod/events/{entity_id}",
        "score": score,
        "confidence": confidence,
        "highlights": [],
        "event_id": entity_id if entity_type == "event" else None,
        "parent_event_id": None,
        "name": title,
        "implemented": None,
        "semantic_used": False,
        "variable_values": [],
    }


def make_drift(
    *,
    drift_id: str = "drift-1",
    field_name: str = "user_id",
    drift_type: str = "missing_field",
    status: str = "open",
    detected_at: datetime | None = None,
    resolved_at: datetime | None = None,
    resolved_by: str | None = None,
    snoozed_until: datetime | None = None,
) -> dict[str, Any]:
    detected = detected_at if detected_at is not None else datetime.now(UTC)
    return {
        "id": drift_id,
        "event_type_id": "et-1",
        "field_name": field_name,
        "drift_type": drift_type,
        "status": status,
        "detected_at": api_time(detected),
        "resolved_at": api_time(resolved_at) if resolved_at else None,
        "resolved_by": resolved_by,
        "snoozed_until": api_time(snoozed_until) if snoozed_until else None,
        "declared_type": None,
        "observed_type": None,
        "sample_value": None,
        "resolution_note": None,
        "scan_config_id": None,
    }


def make_annotation(
    annotation_id: str = "ann-1",
    *,
    label: str = "Deployed web 2026.09.25",
    bucket: str = "2026-09-25T14:02:00Z",
    source: str = "api",
    url: str | None = "https://github.com/acme/web/releases/tag/2026.09.25",
) -> dict[str, Any]:
    """``ChartAnnotationResponse``: a project-level marker posted by CI."""
    return {
        "id": annotation_id,
        "project_id": "pid-1",
        "scope_type": None,
        "scope_ref": None,
        "bucket": bucket,
        "label": label,
        "description": None,
        "color": "#ef4444",
        "source": source,
        "url": url,
        "created_by_user_id": "uid-1",
        "created_at": bucket,
    }


class FakeInstance:
    """A coherent, HEALTHY tripl instance, with one override method per endpoint.

    Registering the whole instance up front and overriding one endpoint per test
    is what keeps a doctor test about the thing it is testing: a test for the
    scans check should not have to remember that doctor also reads /auth/me.

    Routes are held as objects and re-``mock()``ed rather than re-registered —
    respx resolves in registration order, so adding a second route for the same
    URL would leave the first one winning.
    """

    def __init__(self, router: respx.MockRouter, moment: datetime) -> None:
        self.router = router
        self.now = moment
        self._routes: dict[str, respx.Route] = {}
        self.health()
        self.auth()
        self.auth_status()
        self.projects([make_project()])
        self.project("prod", make_project())
        self.data_sources([make_data_source(last_test_at=moment)])
        self.scans("prod", [make_scan_config()])
        self.jobs("prod", "scan-1", [make_job(at=moment, time_to=moment)])
        self.event_types("prod", [make_event_type()])
        self.drifts("prod", "et-1", [])
        self.events("prod", [make_event(last_seen_at=moment)])
        self.event("prod", "evt-1", make_event(last_seen_at=moment))
        self.fields("prod", "et-1", [make_field()])
        self.variables("prod", [make_variable()])
        self.branches("prod", [make_branch("b-0001", name="main", kind="main", ahead=None)])
        self.search("prod", [make_search_result()])
        self.coverage("prod")
        self.signals("prod", [])
        self.deliveries("prod", [])

    def _route(self, url: str, method: str = "GET") -> respx.Route:
        """One route per (method, url).

        Keyed by both since `tripl scans run` POSTs to a URL nothing GETs and
        `scans cancel` POSTs to one that does not exist as a GET at all - a
        method-blind key would have silently answered a POST from a GET mock.
        """
        key = f"{method} {url}"
        if key not in self._routes:
            self._routes[key] = self.router.request(method, url)
        return self._routes[key]

    def _respond(self, url: str, status: int, payload: Any, method: str = "GET") -> respx.Route:
        route = self._route(url, method)
        route.mock(return_value=httpx.Response(status, json=payload))
        return route

    # --- evolving answers, for the follow-mode tests ----------------------
    def handler(
        self,
        url: str,
        respond: Callable[[httpx.Request], httpx.Response],
        method: str = "GET",
    ) -> respx.Route:
        """Answer this URL from a callable, so a test can vary it per request."""
        route = self._route(url, method)
        route.mock(side_effect=respond)
        return route

    def each(self, url: str, entries: Sequence[tuple[int, Any]]) -> respx.Route:
        """Answer with each (status, payload) in turn; the LAST one then repeats.

        Repeating rather than raising StopIteration is what lets a test script
        three interesting polls and then let the loop run to its --duration
        without also having to script the boring remainder.
        """
        state = {"index": 0}

        def respond(request: httpx.Request) -> httpx.Response:
            index = min(state["index"], len(entries) - 1)
            state["index"] += 1
            status, payload = entries[index]
            return httpx.Response(status, json=payload)

        return self.handler(url, respond)

    # --- URLs, so a test can point `each`/`handler` at one --------------------
    @staticmethod
    def jobs_url(slug: str, scan_id: str) -> str:
        return f"{API_BASE}/projects/{slug}/scans/{scan_id}/jobs"

    @staticmethod
    def signals_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/anomalies/signals"

    @staticmethod
    def deliveries_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/alert-deliveries"

    @staticmethod
    def scans_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/scans"

    @staticmethod
    def run_url(slug: str, scan_id: str) -> str:
        return f"{API_BASE}/projects/{slug}/scans/{scan_id}/run"

    @staticmethod
    def cancel_url(slug: str, scan_id: str, job_id: str) -> str:
        return f"{API_BASE}/projects/{slug}/scans/{scan_id}/jobs/{job_id}/cancel"

    @staticmethod
    def events_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/events"

    @staticmethod
    def event_url(slug: str, event_id: str) -> str:
        return f"{API_BASE}/projects/{slug}/events/{event_id}"

    @staticmethod
    def fields_url(slug: str, type_id: str) -> str:
        return f"{API_BASE}/projects/{slug}/event-types/{type_id}/fields"

    @staticmethod
    def variables_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/variables"

    @staticmethod
    def branches_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/branches"

    @staticmethod
    def search_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/search"

    @staticmethod
    def drifts_url(slug: str, type_id: str) -> str:
        return f"{API_BASE}/projects/{slug}/event-types/{type_id}/drifts"

    @staticmethod
    def annotations_url(slug: str) -> str:
        return f"{API_BASE}/projects/{slug}/annotations"

    @staticmethod
    def drift_action_url(slug: str, drift_id: str) -> str:
        """NOT nested under the event type - the action route takes the drift id alone."""
        return f"{API_BASE}/projects/{slug}/event-types/drifts/{drift_id}/actions"

    # --- endpoints -------------------------------------------------------
    def health(self, status: int = 200, payload: Any = _UNSET) -> respx.Route:
        body = {"status": "ok"} if payload is _UNSET else payload
        return self._respond(f"{BASE_URL}/health", status, body)

    def health_raises(self, exc: Exception) -> respx.Route:
        route = self._route(f"{BASE_URL}/health")
        route.mock(side_effect=exc)
        return route

    def auth_status(self, status: int = 200, payload: Any = _UNSET) -> respx.Route:
        """``GET /auth/status`` — UNAUTHENTICATED, like ``/health``.

        A brand-new instance by default: no accounts, registration open, which
        is the state ``tripl install`` finds and the one whose next steps say
        "the first account you create becomes the owner".
        """
        body = {"has_users": False, "registration_enabled": True} if payload is _UNSET else payload
        return self._respond(f"{API_BASE}/auth/status", status, body)

    def auth(self, status: int = 200, payload: Any = _UNSET) -> respx.Route:
        body = (
            {
                "id": "uid-1",
                "email": "operator@example.com",
                "name": "Operator",
                "role": "owner",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            if payload is _UNSET
            else payload
        )
        return self._respond(f"{API_BASE}/auth/me", status, body)

    def projects(self, payload: Any, status: int = 200) -> respx.Route:
        return self._respond(f"{API_BASE}/projects", status, payload)

    def project(self, slug: str, payload: Any = None, status: int = 200) -> respx.Route:
        return self._respond(f"{API_BASE}/projects/{slug}", status, payload)

    def data_sources(self, payload: Any, status: int = 200) -> respx.Route:
        return self._respond(f"{API_BASE}/data-sources", status, payload)

    def scans(self, slug: str, payload: Any, status: int = 200) -> respx.Route:
        return self._respond(f"{API_BASE}/projects/{slug}/scans", status, payload)

    def jobs(self, slug: str, scan_id: str, payload: Any, status: int = 200) -> respx.Route:
        return self._respond(f"{API_BASE}/projects/{slug}/scans/{scan_id}/jobs", status, payload)

    def event_types(self, slug: str, payload: Any, status: int = 200) -> respx.Route:
        return self._respond(f"{API_BASE}/projects/{slug}/event-types", status, payload)

    def drifts(
        self, slug: str, type_id: str, items: Any, status: int = 200, payload: Any = _UNSET
    ) -> respx.Route:
        body = payload if payload is not _UNSET else {"items": items, "total": len(items or [])}
        return self._respond(self.drifts_url(slug, type_id), status, body)

    # --- mutations, for the `scans run|cancel` / `drifts dismiss` tests -------
    def scan_run(
        self, slug: str, scan_id: str, payload: Any = _UNSET, status: int = 201
    ) -> respx.Route:
        """``POST .../run`` -> 201 ScanJobResponse. Note 201, not 200."""
        body = make_job(job_id="job-91c2", status="pending") if payload is _UNSET else payload
        return self._respond(self.run_url(slug, scan_id), status, body, method="POST")

    def job_cancel(
        self,
        slug: str,
        scan_id: str,
        job_id: str,
        payload: Any = _UNSET,
        status: int = 200,
    ) -> respx.Route:
        body = make_job(job_id=job_id, status="cancelled") if payload is _UNSET else payload
        return self._respond(self.cancel_url(slug, scan_id, job_id), status, body, method="POST")

    def drift_action(
        self, slug: str, drift_id: str, payload: Any = _UNSET, status: int = 200
    ) -> respx.Route:
        """``POST .../drifts/{id}/actions`` -> 200 SchemaDriftResponse."""
        body = (
            make_drift(drift_id=drift_id, status="false_positive") if payload is _UNSET else payload
        )
        return self._respond(self.drift_action_url(slug, drift_id), status, body, method="POST")

    def annotate(self, slug: str, payload: Any = _UNSET, status: int = 201) -> respx.Route:
        """``POST .../annotations`` -> 201 new, or 200 when the API de-duplicated."""
        body = make_annotation() if payload is _UNSET else payload
        return self._respond(self.annotations_url(slug), status, body, method="POST")

    def signals(self, slug: str, payload: Any, status: int = 200) -> respx.Route:
        return self._respond(self.signals_url(slug), status, payload)

    def deliveries(self, slug: str, items: Any, status: int = 200) -> respx.Route:
        """``AlertDeliveryListResponse``: {items, total}, not a bare list."""
        body = {"items": items, "total": len(items or [])} if isinstance(items, list) else items
        return self._respond(self.deliveries_url(slug), status, body)

    # --- the plan and catalog reads, for the `events` / `plan` tests ----------
    def events(
        self,
        slug: str,
        items: Any,
        total: int | None = None,
        status: int = 200,
        payload: Any = _UNSET,
    ) -> respx.Route:
        """``EventListResponse``: ``{items, total}``, not a bare list.

        ``total`` defaults to ``len(items)`` and is settable independently on
        purpose: a page smaller than the total is exactly the condition
        `truncated` exists for, and a fixture that could not express it could
        not test the one line that says rows were left behind.

        ``payload`` overrides the envelope entirely, which is how an error body
        is registered — the same escape hatch ``drifts`` carries.
        """
        rows = items or []
        body = (
            payload
            if payload is not _UNSET
            else {"items": rows, "total": len(rows) if total is None else total}
        )
        return self._respond(self.events_url(slug), status, body)

    def event(self, slug: str, event_id: str, payload: Any, status: int = 200) -> respx.Route:
        return self._respond(self.event_url(slug, event_id), status, payload)

    def fields(self, slug: str, type_id: str, payload: Any, status: int = 200) -> respx.Route:
        """``GET .../fields`` -> a bare array of FieldDefinitionResponse."""
        return self._respond(self.fields_url(slug, type_id), status, payload)

    def variables(
        self,
        slug: str,
        items: Any,
        total: int | None = None,
        status: int = 200,
        payload: Any = _UNSET,
    ) -> respx.Route:
        rows = items or []
        body = (
            payload
            if payload is not _UNSET
            else {"items": rows, "total": len(rows) if total is None else total}
        )
        return self._respond(self.variables_url(slug), status, body)

    def branches(self, slug: str, items: Any, status: int = 200) -> respx.Route:
        """``PlanBranchList``: ``{items, total}``."""
        rows = items or []
        return self._respond(self.branches_url(slug), status, {"items": rows, "total": len(rows)})

    def search(
        self,
        slug: str,
        items: Any,
        semantic_used: bool = False,
        truncated: bool | None = None,
        status: int = 200,
    ) -> respx.Route:
        """``SearchResponse``: ``{items, total, truncated, semantic_used}``.

        No ``total`` parameter, deliberately. ``search_service`` answers
        ``total=len(items)`` AFTER trimming to the limit — it never reports a
        pre-paging count — so a double that could be told otherwise let the CLI
        grow a truncation line no real response could trigger, and let two tests
        assert that line's wording while proving only that the fake could lie
        (found reviewing tripl-3ixs).

        ``truncated`` IS a parameter, for the opposite reason: the route now
        answers it authoritatively, so a double that could not say so left the
        CLI's new top rung unreachable — it would only ever be exercised by a
        test hand-building the body (tripl-wkwv.3).

        It defaults to ``None`` meaning OMIT THE KEY, because the CLI ships
        separately from the instance it talks to and both worlds are real: an
        older instance sends no such key and must still get the page-fullness
        guess. A default of ``False`` would have quietly retired that case and
        with it the test that pins it.
        """
        rows = items or []
        body: dict[str, Any] = {
            "items": rows,
            "total": len(rows),
            "semantic_used": semantic_used,
        }
        if truncated is not None:
            body["truncated"] = truncated
        return self._respond(self.search_url(slug), status, body)

    def coverage(self, slug: str, payload: Any = _UNSET, status: int = 200) -> respx.Route:
        body = (
            {
                "days": 7,
                "items": [],
                "summary": {"coverage_pct": 91.4, "matched_count": 388, "total_count": 425},
            }
            if payload is _UNSET
            else payload
        )
        return self._respond(f"{API_BASE}/projects/{slug}/reconciliation/coverage", status, body)


@pytest.fixture
def tripl_api(now: datetime) -> Iterator[FakeInstance]:
    with respx.mock(assert_all_called=False) as router:
        yield FakeInstance(router, now)


class FakeClock:
    """Virtual time. ``sleep`` returns instantly and advances the clock instead.

    This is what lets a 20-tick follow-mode test run in zero wall-clock time on a
    box where the suite may only be invoked once, and it is why no test in this
    repository ever calls ``time.sleep``.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start
        self._monotonic = 0.0
        self.sleeps: list[float] = []
        # One entry per sleep, consumed in order. A non-None entry is raised
        # INSTEAD of sleeping, which is how a test delivers a Ctrl-C to the Nth
        # tick without a real signal.
        self.script: list[BaseException | None] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._monotonic += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.script:
            failure = self.script.pop(0)
            if failure is not None:
                raise failure
        self.advance(seconds)


@pytest.fixture
def fake_clock(now: datetime, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Inject virtual time into ``tripl watch``.

    Patches the binding in the command module rather than the loop's, the same
    way ``tracking_pool`` patches ``runner.create_http_client`` — the command is
    the only place a clock is constructed.
    """
    clock = FakeClock(now)
    from tripl_cli.commands import watch

    monkeypatch.setattr(watch, "default_clock", lambda: clock)
    return clock


class FakeRunner:
    """Every subprocess ``tripl install``/``upgrade`` would run, recorded not run.

    THIS BOX NEVER STARTS A CONTAINER. Not "the tests avoid docker" — there is no
    code path from the test suite to ``subprocess`` at all, because the command
    modules resolve their runner through ``default_runner()`` and the fixture
    below replaces that binding (tripl-ey6j.3).

    Exit codes are scripted by a short key (``version``, ``info``, ``pull``,
    ``up``) rather than by call index: a test about a failing ``up -d`` should
    not have to know how many probes ran before it.
    """

    def __init__(self, **codes: int) -> None:
        self.calls: list[Command] = []
        self.captured: list[bool] = []
        self.codes = codes
        # Plausible defaults, so the happy path needs no scripting at all.
        self.stdout: dict[str, str] = {"version": "v2.29.1\n", "info": "27.1.1\n"}
        self.stderr: dict[str, str] = {}

    @staticmethod
    def key(command: Command) -> str:
        argv = command.argv
        if argv[:3] == ("docker", "compose", "version"):
            return "version"
        if argv[:2] == ("docker", "info"):
            return "info"
        if argv[:3] == ("docker", "compose", "pull"):
            return "pull"
        if argv[:3] == ("docker", "compose", "up"):
            return "up"
        return " ".join(argv)

    def __call__(self, command: Command, capture: bool) -> CommandResult:
        self.calls.append(command)
        self.captured.append(capture)
        key = self.key(command)
        code = self.codes.get(key, 0)
        return CommandResult(
            argv=command.argv,
            returncode=code,
            stdout=self.stdout.get(key, "") if capture else "",
            stderr=self.stderr.get(key, "") if capture else "",
        )

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls]


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Replace the subprocess seam in BOTH command modules.

    Both, because ``monkeypatch.setattr`` replaces an attribute on one module
    object: patching only ``install`` would leave ``upgrade`` shelling out for
    real, which on this box is exactly the accident that must be impossible.
    """
    from tripl_cli.commands import install, upgrade

    runner = FakeRunner()
    for module in (install, upgrade):
        monkeypatch.setattr(module, "default_runner", lambda: runner)
    return runner


@pytest.fixture
def install_dir(tmp_path: Path) -> Iterator[Path]:
    """An empty target directory, with a KNOWN umask.

    ``os.open`` masks the mode it is given, so ``0644`` under a umask of ``077``
    lands as ``0600``. Pinning the umask here is what lets the mode assertions be
    exact numbers instead of bit tests, without the production code ever calling
    ``chmod`` — which it must not, for ``.env`` (see install/files.py).
    """
    previous = os.umask(0o022)
    try:
        yield tmp_path / "stack"
    finally:
        os.umask(previous)


@pytest.fixture
def tracking_pool(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Record every connection pool ``runner.run_async`` opens.

    The direct analogue of mcp-server's test_stdio_lifespan_reuses_one_http_client:
    one pool per invocation, closed on the way out, however the body ended.
    """
    from tripl_cli import runner

    created: list[httpx.AsyncClient] = []
    real = runner.create_http_client

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = real(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(runner, "create_http_client", factory)
    return created
