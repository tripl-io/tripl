import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers._event_generator_variables import SCAN_PROVENANCE_DESCRIPTION
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.core.analyzers.preview import build_json_paths_payload, build_preview_payload
from tripl.models import Base, DataSource, Project, ScanConfig, ScanJob, ScanPreviewJob
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_job import ScanJobStatus
from tripl.models.variable import Variable
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import _errors as task_errors
from tripl.worker.tasks import metrics
from tripl.worker.tasks import scan as scan_tasks


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Scan Test", "slug": "scan-test", "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Test CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def event_type(client: AsyncClient, project: dict) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert resp.status_code == 201
    return resp.json()


class TestScanConfigsCRUD:
    async def test_create_scan_config(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Daily scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "metric_breakdown_columns": ["country", "platform"],
                "metric_breakdown_values_limit": 20,
                "distribution_drift_fields": ["platform"],
                "event_group_rules": [
                    {
                        "name": "product pages",
                        "condition_logic": "any",
                        "conditions": [{"field": "event_name", "pattern": "^product:"}],
                    }
                ],
                "cardinality_threshold": 50,
                "scan_lookback_hours": 24,
                "scan_row_limit": 60000,
                "metrics_row_limit": 120000,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Daily scan"
        assert data["base_query"] == "SELECT * FROM events"
        assert data["event_type_id"] == event_type["id"]
        assert data["cardinality_threshold"] == 50
        assert data["data_source_id"] == data_source["id"]
        assert data["project_id"] == project["id"]
        assert data["json_value_paths"] == []
        assert data["metric_breakdown_columns"] == ["country", "platform"]
        assert data["metric_breakdown_values_limit"] == 20
        assert data["distribution_drift_fields"] == ["platform"]
        assert data["scan_lookback_hours"] == 24
        assert data["scan_row_limit"] == 60000
        assert data["metrics_row_limit"] == 120000
        assert data["event_group_rules"] == [
            {
                "name": "product pages",
                "condition_logic": "any",
                "conditions": [{"field": "event_name", "pattern": "^product:"}],
            }
        ]
        assert data["replay_chunk_interval"] is None
        # App-version observation is off by default (inert when column unset).
        assert data["app_version_column"] is None
        assert data["app_version_keep_releases"] is None
        assert "anomaly_detection_enabled" not in data

    async def test_create_scan_config_with_app_version(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        policy_resp = await client.patch(
            f"/api/v1/projects/{project['slug']}",
            json={"app_version_keep_releases": 3},
        )
        assert policy_resp.status_code == 200
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Versioned scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "app_version_column": "app_version",
                "app_version_keep_releases": 5,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["app_version_column"] == "app_version"
        # The legacy field remains on the wire for rolling-deploy compatibility,
        # but it is mirrored from the project and cannot override project policy.
        assert data["app_version_keep_releases"] == 3

        scan_override = await client.patch(
            f"/api/v1/projects/{project['slug']}/scans/{data['id']}",
            json={"app_version_keep_releases": 7},
        )
        assert scan_override.status_code == 200
        assert scan_override.json()["app_version_keep_releases"] == 3

        project_update = await client.patch(
            f"/api/v1/projects/{project['slug']}",
            json={"app_version_keep_releases": 4},
        )
        assert project_update.status_code == 200
        refreshed = await client.get(f"/api/v1/projects/{project['slug']}/scans/{data['id']}")
        assert refreshed.json()["app_version_keep_releases"] == 4

    async def test_create_scan_config_rejects_app_version_as_breakdown(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Version clash scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "app_version_column": "app_version",
                "metric_breakdown_columns": ["app_version"],
            },
        )
        assert resp.status_code == 422

    async def test_create_scan_config_with_platform(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Platform scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "platform_column": "platform",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["platform_column"] == "platform"

    async def test_create_scan_config_rejects_platform_as_breakdown(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        # platform_column is collected on its own path, so listing it as a generic
        # breakdown column too would double-collect it.
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Platform clash scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "platform_column": "platform",
                "metric_breakdown_columns": ["platform"],
            },
        )
        assert resp.status_code == 422

    async def test_create_scan_config_rejects_platform_as_time_column(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        # The platform column cannot double as another reserved role.
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Platform role clash scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "time_column": "ts",
                "platform_column": "ts",
            },
        )
        assert resp.status_code == 422

    async def test_create_scan_config_rejects_invalid_group_regex(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Bad group regex",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "event_group_rules": [
                    {
                        "name": "broken",
                        "conditions": [{"field": "event_name", "pattern": "["}],
                    }
                ],
            },
        )
        assert resp.status_code == 422

    async def test_apply_group_rules_dispatches_worker_job(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        event_type: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        create_scan_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Grouped scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "event_group_rules": [
                    {
                        "name": "button events",
                        "condition_logic": "all",
                        "conditions": [{"field": "action", "pattern": "^button:"}],
                    }
                ],
            },
        )
        assert create_scan_resp.status_code == 201
        scan_id = create_scan_resp.json()["id"]
        dispatched: list[tuple[str, str]] = []

        def fake_delay(scan_config_id: str, scan_job_id: str) -> None:
            dispatched.append((scan_config_id, scan_job_id))

        monkeypatch.setattr(scan_tasks.apply_event_groups, "delay", fake_delay)

        apply_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/{scan_id}/event-groups/apply"
        )
        assert apply_resp.status_code == 201
        body = apply_resp.json()
        assert body["scan_config_id"] == scan_id
        assert body["status"] == "pending"
        assert dispatched == [(scan_id, body["id"])]

    async def test_cancel_scan_job_marks_cancelled_then_rejects_inactive(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        event_type: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        create_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Cancellable scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
            },
        )
        assert create_resp.status_code == 201
        scan_id = create_resp.json()["id"]

        # Don't actually run the worker; we only need a pending job to cancel.
        monkeypatch.setattr(scan_tasks.run_scan, "delay", lambda *a, **k: None)

        run_resp = await client.post(f"/api/v1/projects/{project['slug']}/scans/{scan_id}/run")
        assert run_resp.status_code == 201
        job_id = run_resp.json()["id"]
        assert run_resp.json()["status"] == "pending"

        cancel_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/{scan_id}/jobs/{job_id}/cancel"
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

        # A cancelled job is not live, so the config is runnable again.
        again = await client.post(f"/api/v1/projects/{project['slug']}/scans/{scan_id}/run")
        assert again.status_code == 201

        # ...but a SECOND run while that one is live is refused. The scheduler
        # already skipped dispatch in this situation; the manual trigger had no
        # guard, so a double-click ran two collections over the same metric
        # windows — and collection deletes a window before rewriting it.
        concurrent = await client.post(f"/api/v1/projects/{project['slug']}/scans/{scan_id}/run")
        assert concurrent.status_code == 409, concurrent.text
        assert "already running" in concurrent.json()["detail"]

        # Cancelling an already-terminal job is rejected.
        again = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/{scan_id}/jobs/{job_id}/cancel"
        )
        assert again.status_code == 409

    async def test_create_scan_config_with_replay_chunk_interval(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Chunked scan",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "time_column": "time",
                "interval": "1h",
                "replay_chunk_interval": "1d",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["replay_chunk_interval"] == "1d"

    async def test_replay_chunk_interval_must_not_be_finer_than_interval(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Bad chunk",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "time_column": "time",
                "interval": "1d",
                "replay_chunk_interval": "1h",
            },
        )
        assert resp.status_code == 422

    async def test_replay_chunk_interval_requires_interval(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "No interval chunk",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "replay_chunk_interval": "1d",
            },
        )
        assert resp.status_code == 422

    async def test_update_rejects_replay_chunk_finer_than_existing_interval(
        self, client: AsyncClient, project: dict, data_source: dict, event_type: dict
    ):
        create_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Updatable",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "time_column": "time",
                "interval": "1d",
            },
        )
        assert create_resp.status_code == 201
        scan_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/projects/{project['slug']}/scans/{scan_id}",
            json={"replay_chunk_interval": "1h"},
        )
        assert resp.status_code == 422

    async def test_list_scan_configs(self, client: AsyncClient, project: dict, data_source: dict):
        for i in range(3):
            await client.post(
                f"/api/v1/projects/{project['slug']}/scans",
                json={
                    "data_source_id": data_source["id"],
                    "name": f"Scan {i}",
                    "base_query": f"SELECT * FROM t{i}",
                },
            )
        resp = await client.get(f"/api/v1/projects/{project['slug']}/scans")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_update_scan_config(self, client: AsyncClient, project: dict, data_source: dict):
        create_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={"data_source_id": data_source["id"], "name": "Old", "base_query": "SELECT 1"},
        )
        scan_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/projects/{project['slug']}/scans/{scan_id}",
            json={
                "name": "New",
                "cardinality_threshold": 200,
                "metric_breakdown_columns": ["country"],
                "metric_breakdown_values_limit": None,
                "distribution_drift_fields": ["country"],
                "scan_lookback_hours": 48,
                "scan_row_limit": 70000,
                "metrics_row_limit": 150000,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"
        assert resp.json()["cardinality_threshold"] == 200
        assert resp.json()["metric_breakdown_columns"] == ["country"]
        assert resp.json()["metric_breakdown_values_limit"] is None
        assert resp.json()["distribution_drift_fields"] == ["country"]
        assert resp.json()["scan_lookback_hours"] == 48
        assert resp.json()["scan_row_limit"] == 70000
        assert resp.json()["metrics_row_limit"] == 150000

    async def test_delete_scan_config(self, client: AsyncClient, project: dict, data_source: dict):
        create_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={"data_source_id": data_source["id"], "name": "DelMe", "base_query": "SELECT 1"},
        )
        scan_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/projects/{project['slug']}/scans/{scan_id}")
        assert resp.status_code == 204

    async def test_scan_config_not_found(self, client: AsyncClient, project: dict):
        resp = await client.get(
            f"/api/v1/projects/{project['slug']}/scans/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404

    async def test_duplicate_name_conflict(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        payload = {"data_source_id": data_source["id"], "name": "Same", "base_query": "SELECT 1"}
        base = f"/api/v1/projects/{project['slug']}/scans"
        r1 = await client.post(base, json=payload)
        assert r1.status_code == 201
        r2 = await client.post(base, json=payload)
        assert r2.status_code == 409

    async def test_replay_scan_metrics_dispatches_collection_job(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        event_type: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        create_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Hourly metrics",
                "base_query": "SELECT * FROM events",
                "event_type_id": event_type["id"],
                "time_column": "created_at",
                "interval": "1h",
            },
        )
        assert create_resp.status_code == 201
        scan_id = create_resp.json()["id"]
        dispatched: list[tuple[str, str, str, str]] = []

        def fake_delay(
            scan_config_id: str,
            scan_job_id: str,
            time_from: str,
            time_to: str,
        ) -> None:
            dispatched.append((scan_config_id, scan_job_id, time_from, time_to))

        monkeypatch.setattr(metrics.collect_metrics, "delay", fake_delay)

        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/{scan_id}/metrics/replay",
            json={
                "time_from": "2026-04-01T00:00:00Z",
                "time_to": "2026-04-02T00:00:00Z",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["scan_config_id"] == scan_id
        assert body["status"] == "pending"
        assert dispatched == [
            (
                scan_id,
                body["id"],
                "2026-04-01T00:00:00+00:00",
                "2026-04-02T00:00:00+00:00",
            )
        ]

    async def test_replay_scan_metrics_requires_monitoring_fields(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
    ) -> None:
        create_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Catalog only",
                "base_query": "SELECT * FROM events",
            },
        )
        assert create_resp.status_code == 201
        scan_id = create_resp.json()["id"]

        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/{scan_id}/metrics/replay",
            json={
                "time_from": "2026-04-01T00:00:00Z",
                "time_to": "2026-04-02T00:00:00Z",
            },
        )

        assert resp.status_code == 400

    def test_build_preview_payload(self) -> None:
        class FakeAdapter:
            def test_connection(self) -> bool:
                return True

            def get_columns(self, base_query: str) -> list[ColumnInfo]:
                return [
                    ColumnInfo(name="event_name", type_name="String"),
                    ColumnInfo(name="created_at", type_name="DateTime"),
                    ColumnInfo(name="payload", type_name="JSON"),
                ]

            def get_preview_rows(
                self,
                base_query: str,
                limit: int = 10,
                **_kwargs: object,
            ) -> tuple[list[str], list[tuple[object, ...]]]:
                return (
                    ["event_name", "created_at", "payload"],
                    [
                        (
                            "purchase",
                            datetime(2026, 4, 12, 10, 30),
                            {"extra": {"key": "TASK-123"}, "locale": "en"},
                        ),
                    ],
                )

            def close(self) -> None:
                return None

        payload = build_preview_payload(FakeAdapter(), "SELECT * FROM events", 5)

        assert [column["name"] for column in payload["columns"]] == [
            "event_name",
            "created_at",
            "payload",
        ]
        assert payload["rows"][0]["event_name"] == "purchase"
        assert payload["rows"][0]["payload"]["extra"]["key"] == "TASK-123"
        # The fast preview lists JSON-typed columns but does NOT discover their
        # nested paths — that is the separate build_json_paths_payload step.
        assert payload["json_columns"] == [{"column": "payload", "paths": []}]

    def test_build_preview_payload_prefers_varied_rows(self) -> None:
        class FakeAdapter:
            def test_connection(self) -> bool:
                return True

            def get_columns(self, base_query: str) -> list[ColumnInfo]:
                return [
                    ColumnInfo(name="created_at", type_name="DateTime"),
                    ColumnInfo(name="page", type_name="String"),
                    ColumnInfo(name="event_type", type_name="String"),
                ]

            def get_preview_rows(
                self,
                base_query: str,
                limit: int = 10,
                **_kwargs: object,
            ) -> tuple[list[str], list[tuple[object, ...]]]:
                assert limit >= 16
                return (
                    ["created_at", "page", "event_type"],
                    [
                        (datetime(2026, 4, 12, 10, 0), "main", "pv"),
                        (datetime(2026, 4, 12, 10, 1), "main", "pv"),
                        (datetime(2026, 4, 12, 10, 2), "pricing", "pv"),
                        (datetime(2026, 4, 12, 10, 3), "main", "signup"),
                    ],
                )

            def close(self) -> None:
                return None

        payload = build_preview_payload(FakeAdapter(), "SELECT * FROM events", 2)

        assert len(payload["rows"]) == 2
        assert {row["page"] for row in payload["rows"]} == {"main", "pricing"}

    def test_build_json_paths_payload_discovers_json_paths_separately(self) -> None:
        class FakeAdapter:
            def test_connection(self) -> bool:
                return True

            def get_columns(self, base_query: str) -> list[ColumnInfo]:
                return [
                    ColumnInfo(name="event_name", type_name="String"),
                    ColumnInfo(name="payload", type_name="JSON"),
                ]

            def get_preview_rows(
                self,
                base_query: str,
                limit: int = 10,
                **_kwargs: object,
            ) -> tuple[list[str], list[tuple[object, ...]]]:
                return (
                    ["event_name", "payload"],
                    [
                        ("purchase", {"locale": "en"}),
                    ],
                )

            def get_json_path_samples(
                self,
                base_query: str,
                json_columns: list[str],
                *,
                time_column: str | None = None,
                time_from: datetime | None = None,
                time_to: datetime | None = None,
                path_limit: int,
                sample_limit: int,
                sample_row_limit: int,
            ) -> dict[str, dict[str, list[object]]]:
                assert json_columns == ["payload"]
                assert path_limit > 10
                assert sample_limit == 3
                assert sample_row_limit > 10
                return {
                    "payload": {
                        "extra.key": ['"TASK-999"'],
                        "hidden.flag": [True],
                        "locale": ['"en"'],
                    }
                }

            def close(self) -> None:
                return None

        payload = build_json_paths_payload(FakeAdapter(), "SELECT * FROM events", [])

        assert payload["json_columns"] == [
            {
                "column": "payload",
                "paths": [
                    {
                        "full_path": "payload.extra.key",
                        "path": "extra.key",
                        "sample_values": ["TASK-999"],
                    },
                    {
                        "full_path": "payload.hidden.flag",
                        "path": "hidden.flag",
                        "sample_values": ["true"],
                    },
                    {
                        "full_path": "payload.locale",
                        "path": "locale",
                        "sample_values": ["en"],
                    },
                ],
            }
        ]

    def test_build_json_paths_payload_keeps_selected_json_paths_visible(self) -> None:
        class FakeAdapter:
            def test_connection(self) -> bool:
                return True

            def get_columns(self, base_query: str) -> list[ColumnInfo]:
                return [
                    ColumnInfo(name="event_name", type_name="String"),
                    ColumnInfo(name="payload", type_name="JSON"),
                ]

            def get_preview_rows(
                self,
                base_query: str,
                limit: int = 10,
                **_kwargs: object,
            ) -> tuple[list[str], list[tuple[object, ...]]]:
                return (
                    ["event_name", "payload"],
                    [
                        ("purchase", {"locale": "en"}),
                    ],
                )

            def get_json_path_samples(
                self,
                base_query: str,
                json_columns: list[str],
                *,
                time_column: str | None = None,
                time_from: datetime | None = None,
                time_to: datetime | None = None,
                path_limit: int,
                sample_limit: int,
                sample_row_limit: int,
            ) -> dict[str, dict[str, list[object]]]:
                return {"payload": {"locale": ['"en"']}}

            def close(self) -> None:
                return None

        payload = build_json_paths_payload(
            FakeAdapter(), "SELECT * FROM events", ["payload.saved.key"]
        )

        assert payload["json_columns"] == [
            {
                "column": "payload",
                "paths": [
                    {
                        "full_path": "payload.locale",
                        "path": "locale",
                        "sample_values": ["en"],
                    },
                    {
                        "full_path": "payload.saved.key",
                        "path": "saved.key",
                        "sample_values": [],
                    },
                ],
            }
        ]

    async def test_preview_dispatches_worker_job(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dispatched: list[str] = []

        def fake_delay(job_id: str) -> None:
            dispatched.append(job_id)

        monkeypatch.setattr(scan_tasks.preview_scan_config_async, "delay", fake_delay)

        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/preview",
            json={
                "data_source_id": data_source["id"],
                "base_query": "SELECT * FROM events",
                "limit": 5,
            },
        )

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        assert body["result_summary"] is None
        assert dispatched == [body["id"]]

        poll = await client.get(
            f"/api/v1/projects/{project['slug']}/scans/preview-jobs/{body['id']}"
        )
        assert poll.status_code == 200
        assert poll.json()["status"] == "pending"

    async def test_preview_job_not_found(self, client: AsyncClient, project: dict) -> None:
        resp = await client.get(
            f"/api/v1/projects/{project['slug']}/scans/preview-jobs/"
            "00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404

    def test_preview_scan_config_async_completes_job(self, tmp_path, monkeypatch) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'preview.db'}")
        try:
            Base.metadata.create_all(engine)
            sync_session_factory = sessionmaker(engine, expire_on_commit=False)

            project_id = uuid.uuid4()
            data_source_id = uuid.uuid4()
            job_id = uuid.uuid4()
            with sync_session_factory() as session:
                session.add_all(
                    [
                        Project(id=project_id, name="P", slug="p", description=""),
                        DataSource(
                            id=data_source_id,
                            name="DS",
                            db_type="clickhouse",
                            host="localhost",
                            port=8123,
                            database_name="default",
                            username="default",
                            password_encrypted="",
                        ),
                        ScanPreviewJob(
                            id=job_id,
                            project_id=project_id,
                            data_source_id=data_source_id,
                            base_query="SELECT * FROM events",
                            json_value_paths=[],
                            row_limit=5,
                            time_column="created_at",
                            scan_lookback_hours=24,
                            status="pending",
                        ),
                    ]
                )
                session.commit()

            preview_kwargs: dict[str, object] = {}

            class FakeAdapter:
                def test_connection(self) -> bool:
                    return True

                def get_columns(self, base_query: str) -> list[ColumnInfo]:
                    return [
                        ColumnInfo(name="event_name", type_name="String"),
                        ColumnInfo(name="payload", type_name="JSON"),
                    ]

                def get_preview_rows(
                    self,
                    base_query: str,
                    limit: int = 10,
                    **_kwargs: object,
                ) -> tuple[list[str], list[tuple[object, ...]]]:
                    preview_kwargs.update(_kwargs)
                    return (["event_name", "payload"], [("purchase", {"locale": "en"})])

                def get_json_path_samples(
                    self,
                    base_query: str,
                    json_columns: list[str],
                    *,
                    time_column: str | None = None,
                    time_from: datetime | None = None,
                    time_to: datetime | None = None,
                    path_limit: int,
                    sample_limit: int,
                    sample_row_limit: int,
                ) -> dict[str, dict[str, list[object]]]:
                    return {"payload": {"locale": ['"en"']}}

                def close(self) -> None:
                    return None

            monkeypatch.setitem(
                scan_tasks.preview_scan_config_async.run.__globals__,
                "_get_sync_session",
                sync_session_factory,
            )
            monkeypatch.setitem(
                scan_tasks.preview_scan_config_async.run.__globals__,
                "_build_adapter",
                lambda ds: FakeAdapter(),
            )

            result = scan_tasks.preview_scan_config_async.run(str(job_id))

            assert [column["name"] for column in result["columns"]] == ["event_name", "payload"]
            assert result["rows"][0]["payload"] == {"locale": "en"}
            assert preview_kwargs["time_column"] == "created_at"
            assert preview_kwargs["time_from"] is not None
            assert preview_kwargs["time_to"] is not None

            with sync_session_factory() as session:
                job = session.get(ScanPreviewJob, job_id)
                assert job.status == "completed"
                assert job.started_at is not None
                assert job.completed_at is not None
                assert job.error_message is None
                assert job.result_summary["rows"][0]["event_name"] == "purchase"
        finally:
            engine.dispose()

    def test_preview_scan_config_async_discovers_json_paths_when_flagged(
        self, tmp_path, monkeypatch
    ) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'preview_json.db'}")
        try:
            Base.metadata.create_all(engine)
            sync_session_factory = sessionmaker(engine, expire_on_commit=False)

            project_id = uuid.uuid4()
            data_source_id = uuid.uuid4()
            job_id = uuid.uuid4()
            with sync_session_factory() as session:
                session.add_all(
                    [
                        Project(id=project_id, name="P", slug="p", description=""),
                        DataSource(
                            id=data_source_id,
                            name="DS",
                            db_type="clickhouse",
                            host="localhost",
                            port=8123,
                            database_name="default",
                            username="default",
                            password_encrypted="",
                        ),
                        ScanPreviewJob(
                            id=job_id,
                            project_id=project_id,
                            data_source_id=data_source_id,
                            base_query="SELECT * FROM events",
                            json_value_paths=[],
                            row_limit=5,
                            time_column="created_at",
                            scan_lookback_hours=24,
                            include_json_paths=True,
                            status="pending",
                        ),
                    ]
                )
                session.commit()

            discovery_kwargs: dict[str, object] = {}
            preview_rows_called = False

            class FakeAdapter:
                def test_connection(self) -> bool:
                    return True

                def get_columns(self, base_query: str) -> list[ColumnInfo]:
                    return [
                        ColumnInfo(name="event_name", type_name="String"),
                        ColumnInfo(name="payload", type_name="JSON"),
                    ]

                def get_preview_rows(
                    self,
                    base_query: str,
                    limit: int = 10,
                    **_kwargs: object,
                ) -> tuple[list[str], list[tuple[object, ...]]]:
                    nonlocal preview_rows_called
                    preview_rows_called = True
                    return (["event_name", "payload"], [("purchase", {"locale": "en"})])

                def get_json_path_samples(
                    self,
                    base_query: str,
                    json_columns: list[str],
                    **kwargs: object,
                ) -> dict[str, dict[str, list[object]]]:
                    discovery_kwargs.update(kwargs)
                    assert json_columns == ["payload"]
                    return {"payload": {"locale": ['"en"']}}

                def close(self) -> None:
                    return None

            monkeypatch.setitem(
                scan_tasks.preview_scan_config_async.run.__globals__,
                "_get_sync_session",
                sync_session_factory,
            )
            monkeypatch.setitem(
                scan_tasks.preview_scan_config_async.run.__globals__,
                "_build_adapter",
                lambda ds: FakeAdapter(),
            )

            result = scan_tasks.preview_scan_config_async.run(str(job_id))

            # Discovery mode returns only json_columns (no columns/rows preview).
            assert "columns" not in result
            assert "rows" not in result
            assert result["json_columns"] == [
                {
                    "column": "payload",
                    "paths": [
                        {
                            "full_path": "payload.locale",
                            "path": "locale",
                            "sample_values": ["en"],
                        },
                    ],
                }
            ]
            # The native discovery path is used; we do not fall back to sampling rows.
            assert preview_rows_called is False
            assert discovery_kwargs["time_column"] == "created_at"
            assert discovery_kwargs["time_from"] is not None
            assert discovery_kwargs["time_to"] is not None

            with sync_session_factory() as session:
                job = session.get(ScanPreviewJob, job_id)
                assert job.status == "completed"
                assert job.result_summary["json_columns"][0]["column"] == "payload"
        finally:
            engine.dispose()

    def test_preview_scan_config_async_marks_job_failed(self, tmp_path, monkeypatch) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'preview_fail.db'}")
        try:
            Base.metadata.create_all(engine)
            sync_session_factory = sessionmaker(engine, expire_on_commit=False)

            project_id = uuid.uuid4()
            data_source_id = uuid.uuid4()
            job_id = uuid.uuid4()
            with sync_session_factory() as session:
                session.add_all(
                    [
                        Project(id=project_id, name="P", slug="p", description=""),
                        DataSource(
                            id=data_source_id,
                            name="DS",
                            db_type="clickhouse",
                            host="localhost",
                            port=8123,
                            database_name="default",
                            username="default",
                            password_encrypted="",
                        ),
                        ScanPreviewJob(
                            id=job_id,
                            project_id=project_id,
                            data_source_id=data_source_id,
                            base_query="SELECT * FROM events",
                            json_value_paths=[],
                            row_limit=5,
                            status="pending",
                        ),
                    ]
                )
                session.commit()

            class BrokenAdapter:
                def test_connection(self) -> bool:
                    raise RuntimeError("connection refused")

                def close(self) -> None:
                    return None

            monkeypatch.setitem(
                scan_tasks.preview_scan_config_async.run.__globals__,
                "_get_sync_session",
                sync_session_factory,
            )
            monkeypatch.setitem(
                scan_tasks.preview_scan_config_async.run.__globals__,
                "_build_adapter",
                lambda ds: BrokenAdapter(),
            )

            with pytest.raises(RuntimeError):
                scan_tasks.preview_scan_config_async.run(str(job_id))

            with sync_session_factory() as session:
                job = session.get(ScanPreviewJob, job_id)
                assert job.status == "failed"
                # The raw driver text never reaches the user-facing field; it is
                # mapped to a friendly, category-specific summary instead.
                assert job.error_message == "Scan failed: could not connect to the data source."
                assert "connection refused" not in job.error_message
                assert job.result_summary is None
        finally:
            engine.dispose()

    def test_user_facing_error_sanitizes_internals_but_keeps_curated_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Curated ScanError messages are user-actionable and surfaced verbatim,
        # under the "Scan failed" prefix the UI matches on. The sanitiser adds it
        # so no raise site has to remember to — before that, all
        # sixty-odd curated messages reached the browser and were thrown away
        # there, one layer past where tripl-embs had rescued them.
        curated = scan_tasks.ScanError(
            "The scan query reached the configured row limit (60000); increase scan_row_limit"
        )
        assert scan_tasks._user_facing_error(curated) == f"Scan failed: {curated}"

        # The metrics row-limit guards are the real producers of that shape, and
        # they must raise ScanError — as bare ValueErrors their (already
        # user-written) text was thrown away and replaced by the generic internal
        # -error line, which is why a real failing scan told the operator nothing.
        # Exercise the guard itself rather than a hand-written
        # string so a regression to ValueError fails here.
        from tripl.worker.tasks.metrics import metric_collect

        monkeypatch.setattr(metric_collect, "METRIC_QUERY_ROW_LIMIT", 2)
        with pytest.raises(scan_tasks.ScanError) as excinfo:
            metric_collect._reject_truncated_rows([1, 2, 3], what="Fact metric aggregate")
        row_limit_message = scan_tasks._user_facing_error(excinfo.value)
        assert row_limit_message == f"Scan failed: {excinfo.value}"
        assert "Fact metric aggregate reached the metric query row limit (2)" in row_limit_message
        assert row_limit_message != "Scan failed due to an internal error."

        # A ClickHouse read-timeout carries host/port — replace with a summary.
        timeout = TimeoutError(
            "HTTPConnectionPool(host='warehouse.internal', port=8123): Read timed out."
        )
        timeout_msg = scan_tasks._user_facing_error(timeout)
        assert timeout_msg == "Scan failed: the data source did not respond in time."
        assert "warehouse.internal" not in timeout_msg
        assert "8123" not in timeout_msg

        # Celery's soft/hard time-limit exceptions carry no "timeout" word, but a
        # scan that hits the task time limit is a data-source timeout in effect —
        # it must categorize as one, not fall through to the generic message.
        for limit_text in (
            "SoftTimeLimitExceeded()",
            "Task handler raised error: TimeLimitExceeded(3600.0,)",
            "soft time limit (300s) exceeded",
        ):
            assert (
                scan_tasks._user_facing_error(Exception(limit_text))
                == "Scan failed: the data source did not respond in time."
            )

        # A connection failure (ClickHouse driver text) -> friendly summary.
        refused = ConnectionError("clickhouse-connect: Connection refused to 10.0.0.4:9000")
        refused_msg = scan_tasks._user_facing_error(refused)
        assert refused_msg == "Scan failed: could not connect to the data source."
        assert "10.0.0.4" not in refused_msg
        assert "clickhouse" not in refused_msg.lower()

        # A SQLAlchemy autoflush hint (ORM internals) -> generic summary only.
        orm = RuntimeError(
            "This Session's transaction has been rolled back due to a previous "
            "exception during flush. (Background on this error at: "
            "https://sqlalche.me/e/20/7s2a)"
        )
        orm_msg = scan_tasks._user_facing_error(orm)
        # Self-hosted: the operator is support, so the message drops the
        # "contact support" line and keeps only the actionable fact.
        assert orm_msg == "Scan failed due to an internal error."
        assert "contact support" not in orm_msg.lower()
        assert "sqlalche" not in orm_msg.lower()
        assert "flush" not in orm_msg.lower()

    def test_every_branch_returns_the_prefix_the_ui_matches_on(self) -> None:
        """The invariant the entire curated set rests on, checked on every branch.

        ``frontend/src/lib/scanError.ts`` shows a backend message verbatim only
        when it starts with "scan failed", and collapses everything else into a
        bare "Scan failed." A curated message without the prefix is therefore not
        styled differently — it is deleted before anyone reads it. That was true
        of every ``ScanError`` in this repository until tripl-7bol, which is why
        this asserts the property rather than four specific strings.
        """
        from tripl.core.name_template import NameFormatError

        for exc in (
            scan_tasks.ScanError("the scan config has no event group rules"),
            NameFormatError("Scan failed: the event name format references unknown keys: action"),
            TimeoutError("HTTPConnectionPool(host='wh.internal', port=8123): Read timed out"),
            ConnectionError("clickhouse-connect: Connection refused to 10.0.0.4:9000"),
            RuntimeError("sqlalchemy rolled the transaction back"),
        ):
            message = scan_tasks._user_facing_error(exc)
            assert message.startswith(task_errors.SCAN_FAILED_PREFIX), (
                f"{type(exc).__name__}: {message}"
            )

    def test_the_prefix_is_added_once_and_the_cap_bounds_the_final_string(self) -> None:
        """Idempotence and where the 500-char cap lands, which interact.

        ``_apply_name_format`` spells the prefix itself because it budgets its
        "Available keys" list against that cap and the arithmetic has to include
        the prefix — it cannot import the constant, since ``core`` must never
        import ``worker``. Doubling the prefix there would silently spend 13
        characters of a budget that was computed without them.
        """
        already = scan_tasks.ScanError("Scan failed: the event name format references unknown keys")
        assert scan_tasks._user_facing_error(already) == str(already)

        # Capped AFTER prefixing: the cap bounds what a UI row renders, and that
        # is the final string rather than the body it was assembled from.
        assert len(scan_tasks._user_facing_error(scan_tasks.ScanError("x" * 600))) == (
            task_errors._MAX_CURATED_LEN
        )

    def test_the_prefix_is_the_one_the_frontend_actually_matches(self) -> None:
        """A cross-language contract, pinned the only way two languages allow.

        The repo already accepts this shape: ``_NAME_FORMAT_ERROR_BUDGET`` is
        declared in ``core`` as a mirror of the worker's cap and pinned by
        ``test_name_format_errors``, because ``core`` cannot import ``worker``.
        TypeScript cannot import Python at all, so the pin reads the file. Both
        suites run on every PR, so either side moving alone fails here.
        """
        repo_root = Path(__file__).resolve().parents[4]
        source = (repo_root / "frontend" / "src" / "lib" / "scanError.ts").read_text(
            encoding="utf-8"
        )
        assert f"startsWith('{task_errors.SCAN_FAILED_PREFIX.lower()}')" in source, (
            "scanError.ts no longer matches the prefix _errors.py guarantees"
        )

    def test_apply_event_groups_reindexes_after_the_merge_commits(
        self, tmp_path, monkeypatch
    ) -> None:
        """The one catalog-mutating task that never rebuilt the search index.

        A group event this task creates carried no search document until some
        unrelated later task happened to reindex the branch. The
        ORDER is the substance here, not decoration: the reindex opens its own
        connection and cannot see this session's uncommitted work, so calling it
        before the commit would index the pre-merge state and still look correct
        from the outside.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'apply_groups.db'}")
        try:
            Base.metadata.create_all(engine)
            sync_session_factory = sessionmaker(engine, expire_on_commit=False)

            project_id = uuid.uuid4()
            data_source_id = uuid.uuid4()
            config_id = uuid.uuid4()
            job_id = uuid.uuid4()
            with sync_session_factory() as session:
                session.add_all(
                    [
                        Project(id=project_id, name="P", slug="p-groups", description=""),
                        DataSource(
                            id=data_source_id,
                            name="DS",
                            db_type="clickhouse",
                            host="localhost",
                            port=8123,
                            database_name="default",
                            username="default",
                            password_encrypted="",
                        ),
                        ScanConfig(
                            id=config_id,
                            project_id=project_id,
                            data_source_id=data_source_id,
                            name="Daily",
                            base_query="SELECT * FROM events",
                            event_group_rules=[
                                {
                                    "name": "click events",
                                    "condition_logic": "all",
                                    "conditions": [{"field": "__event_name", "pattern": "^click:"}],
                                }
                            ],
                        ),
                        ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
                    ]
                )
                session.flush()
                # A catalog for the rule to actually act on: the task derives
                # its event types from existing events, and the merge skips an
                # event type with no field definitions.
                event_type = EventType(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    name="pv",
                    display_name="Page View",
                    description="",
                )
                session.add(event_type)
                session.flush()
                field = FieldDefinition(
                    id=uuid.uuid4(),
                    event_type_id=event_type.id,
                    name="action",
                    display_name="Action",
                    field_type="string",
                    order=0,
                )
                session.add(field)
                session.flush()
                session.add(
                    Event(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        event_type_id=event_type.id,
                        name="click:one",
                        source_name="click:one",
                        order=0,
                        status="implemented",
                    )
                )
                session.commit()

            observed: list[bool] = []

            def fake_reindex(session: object, project_id_arg: object) -> None:
                # Reads through a SEPARATE session, the way the real reindex
                # does — it opens its own engine, so anything still uncommitted
                # in the task's session is invisible to it.
                with sync_session_factory() as fresh:
                    names = set(fresh.execute(select(Event.source_name)).scalars().all())
                observed.append("click events" in names)

            monkeypatch.setitem(
                scan_tasks.apply_event_groups.run.__globals__,
                "_get_sync_session",
                lambda: sync_session_factory(),
            )
            monkeypatch.setitem(
                scan_tasks.apply_event_groups.run.__globals__,
                "reindex_main_branch_from_worker",
                fake_reindex,
            )

            scan_tasks.apply_event_groups.run(str(config_id), str(job_id))

            assert observed == [True], (
                "the reindex must run exactly once, and only after the merge is committed"
            )
        finally:
            engine.dispose()

    def test_run_scan_persists_friendly_error_and_hides_internals(
        self, tmp_path, monkeypatch
    ) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'run_scan_fail.db'}")
        try:
            Base.metadata.create_all(engine)
            sync_session_factory = sessionmaker(engine, expire_on_commit=False)

            project_id = uuid.uuid4()
            data_source_id = uuid.uuid4()
            config_id = uuid.uuid4()
            job_id = uuid.uuid4()
            with sync_session_factory() as session:
                session.add_all(
                    [
                        Project(id=project_id, name="P", slug="p", description=""),
                        DataSource(
                            id=data_source_id,
                            name="DS",
                            db_type="clickhouse",
                            host="warehouse.internal",
                            port=8123,
                            database_name="default",
                            username="default",
                            password_encrypted="",
                        ),
                        ScanConfig(
                            id=config_id,
                            project_id=project_id,
                            data_source_id=data_source_id,
                            name="Daily",
                            base_query="SELECT * FROM events",
                        ),
                        ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
                    ]
                )
                session.commit()

            class TimingOutAdapter:
                def test_connection(self) -> bool:
                    raise TimeoutError(
                        "clickhouse-connect: HTTPConnectionPool("
                        "host='warehouse.internal', port=8123): Read timed out. "
                        "(read timeout=30)"
                    )

                def close(self) -> None:
                    return None

            monkeypatch.setitem(
                scan_tasks.run_scan.run.__globals__,
                "_get_sync_session",
                sync_session_factory,
            )
            monkeypatch.setitem(
                scan_tasks.run_scan.run.__globals__,
                "_build_adapter",
                lambda ds: TimingOutAdapter(),
            )

            with pytest.raises(TimeoutError):
                scan_tasks.run_scan.run(str(config_id), str(job_id))

            with sync_session_factory() as session:
                job = session.get(ScanJob, job_id)
                assert job.status == "failed"
                assert job.completed_at is not None
                # User sees a friendly summary; raw driver detail goes to logs only.
                assert job.error_message == "Scan failed: the data source did not respond in time."
                assert "warehouse.internal" not in job.error_message
                assert "8123" not in job.error_message
                assert "clickhouse" not in job.error_message.lower()
                assert "read timed out" not in job.error_message.lower()
        finally:
            engine.dispose()

    @staticmethod
    def _seed_manual_scan_catalog(
        sync_session_factory,
        *,
        slug: str,
        with_fossil: bool,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]:
        """A project a manual scan can run over, optionally holding one fossil.

        The "fossil" is a variable in exactly the state a scan leaves behind and
        nothing else — the provenance description is the only marker the model
        carries, so a row without it reads as ``user_edited`` and can never be
        swept. The referenced variable beside it is what keeps the assertions
        about a PREDICATE rather than a blanket delete.
        """
        project_id = uuid.uuid4()
        data_source_id = uuid.uuid4()
        config_id = uuid.uuid4()
        job_id = uuid.uuid4()
        event_type_id = uuid.uuid4()
        fossil_id = uuid.uuid4() if with_fossil else None

        with sync_session_factory() as session:
            session.add_all(
                [
                    Project(id=project_id, name="P", slug=slug, description=""),
                    DataSource(
                        id=data_source_id,
                        name="DS",
                        db_type="clickhouse",
                        host="localhost",
                        port=8123,
                        database_name="default",
                        username="default",
                        password_encrypted="",
                    ),
                    ScanConfig(
                        id=config_id,
                        project_id=project_id,
                        data_source_id=data_source_id,
                        event_type_id=event_type_id,
                        name="Daily",
                        base_query="SELECT * FROM events",
                        scan_lookback_hours=24,
                    ),
                    ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
                    EventType(
                        id=event_type_id,
                        project_id=project_id,
                        name="pv",
                        display_name="Page View",
                        description="",
                    ),
                ]
            )
            session.flush()

            # Referenced: a stored ``${plan}`` keeps this row whatever else runs.
            field = FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=event_type_id,
                name="tier",
                display_name="Tier",
                field_type="string",
                order=0,
            )
            event = Event(
                id=uuid.uuid4(),
                project_id=project_id,
                event_type_id=event_type_id,
                name="Uses plan",
                description="",
                status="implemented",
            )
            session.add_all([field, event])
            session.flush()
            session.add_all(
                [
                    EventFieldValue(
                        id=uuid.uuid4(),
                        event_id=event.id,
                        field_definition_id=field.id,
                        value="${plan}",
                    ),
                    Variable(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        name="plan",
                        source_name="payload.user.plan",
                        description=SCAN_PROVENANCE_DESCRIPTION,
                        variable_type="string",
                        bindings=["payload.user.plan"],
                    ),
                ]
            )
            if fossil_id is not None:
                session.add(
                    Variable(
                        id=fossil_id,
                        project_id=project_id,
                        name="adana",
                        source_name="payload.user.adana",
                        description=SCAN_PROVENANCE_DESCRIPTION,
                        variable_type="string",
                        bindings=["payload.user.adana"],
                    )
                )
            session.commit()

        return config_id, job_id, fossil_id

    @staticmethod
    def _stub_manual_scan_pipeline(monkeypatch, sync_session_factory) -> None:
        """Everything ``run_scan`` needs except the sweep, which is under test."""

        class _CatalogAdapter:
            def test_connection(self) -> bool:
                return True

            def get_columns(self, base_query: str) -> list[ColumnInfo]:
                return [ColumnInfo(name="event_name", type_name="String")]

            def close(self) -> None:
                return None

        class _EmptyAnalysis:
            row_limit_reached = False
            rows: list[object] = []

        for name, value in (
            ("_get_sync_session", sync_session_factory),
            ("_build_adapter", lambda ds: _CatalogAdapter()),
            ("analyze_cardinality", lambda *a, **k: _EmptyAnalysis()),
            ("generate_events", lambda *a, **k: GenerationResult(columns_analyzed=1)),
            ("reindex_main_branch_from_worker", lambda session, project_id: None),
        ):
            monkeypatch.setitem(scan_tasks.run_scan.run.__globals__, name, value)

    def test_run_scan_summary_carries_variables_retired_even_when_it_took_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        """The manual run's summary must EMIT the count, and 0 is a real answer.

        ``run_scan`` computed ``variables_retired`` and appended the details
        sentence, but its ``result_summary`` literal never carried the key — so
        the frontend's "Variables retired" card, guarded on
        ``summary.variables_retired != null``, was structurally unreachable for
        the one run type a user triggers deliberately, and the docs telling the
        reader to read it against "Variables created" silently failed there.

        The key is emitted UNCONDITIONALLY because the manual sweep is itself
        unconditional: no ``is_replay`` gate, no declared-window gate, so a 0
        here honestly means "swept, found nothing". That is the opposite of
        ``collect_metrics``, where the key is ABSENT on a run that did not sweep.

        DISABLE-THE-FIX: drop ``"variables_retired"`` from ``run_scan``'s
        ``result_summary`` literal and the presence assertion below goes red —
        the count assertion alone would not, since the key would simply be gone.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'run_scan_sweep_none.db'}")
        try:
            Base.metadata.create_all(engine)
            sync_session_factory = sessionmaker(engine, expire_on_commit=False)
            config_id, job_id, _ = self._seed_manual_scan_catalog(
                sync_session_factory, slug="p-sweep-none", with_fossil=False
            )
            self._stub_manual_scan_pipeline(monkeypatch, sync_session_factory)

            summary = scan_tasks.run_scan.run(str(config_id), str(job_id))

            assert "variables_retired" in summary, (
                "a manual scan always sweeps, so it must always report what the sweep took"
            )
            assert summary["variables_retired"] == 0
            # Silent in the details list when there was nothing to say — the
            # counter is where "swept, found nothing" is stated.
            details = summary["details"]
            assert isinstance(details, list)
            assert not [line for line in details if "Retired" in line]
        finally:
            engine.dispose()

    def test_run_scan_summary_reports_the_rows_the_sweep_deleted(
        self, tmp_path, monkeypatch
    ) -> None:
        """And the count is the real one, beside ``variables_created`` where it is read.

        DISABLE-THE-FIX: drop ``"variables_retired"`` from ``run_scan``'s
        ``result_summary`` literal and this raises ``KeyError``.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'run_scan_sweep_one.db'}")
        try:
            Base.metadata.create_all(engine)
            sync_session_factory = sessionmaker(engine, expire_on_commit=False)
            config_id, job_id, fossil_id = self._seed_manual_scan_catalog(
                sync_session_factory, slug="p-sweep-one", with_fossil=True
            )
            self._stub_manual_scan_pipeline(monkeypatch, sync_session_factory)

            summary = scan_tasks.run_scan.run(str(config_id), str(job_id))

            assert summary["variables_retired"] == 1
            # The counter and the sentence agree, and both are on the same run.
            details = summary["details"]
            assert isinstance(details, list)
            assert "Retired 1 unused variables no event refers to" in details
            with sync_session_factory() as session:
                assert session.get(Variable, fossil_id) is None
                # The predicate, not a blanket delete: a live ``${token}`` keeps
                # its row, so "Variables retired" can be read against
                # "Variables created" without reading as a purge.
                surviving = set(session.execute(select(Variable.name)).scalars().all())
            assert surviving == {"plan"}
        finally:
            engine.dispose()

    def test_scan_error_and_user_facing_error_are_shared_from_errors_module(self) -> None:
        # scan.py re-exports the canonical implementations from worker.tasks._errors
        # so the metrics task sanitises with identical logic.
        assert scan_tasks.ScanError is task_errors.ScanError
        assert scan_tasks._user_facing_error is task_errors.user_facing_error


async def _create_scan(client: AsyncClient, project: dict, data_source: dict, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/scans",
        json={
            "data_source_id": data_source["id"],
            "name": name,
            "base_query": "SELECT * FROM events",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestFailedScanRuns:
    """Failed-run affordances backing the scans settings page:

    the underlying error is surfaced on the run, the jobs feed carries enough
    history to show a failing streak, and "Run again" reuses the manual trigger.
    """

    async def test_run_marks_job_failed_and_exposes_error_when_broker_down(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scan_id = await _create_scan(client, project, data_source, "Broker-down scan")

        # Dispatch raises → trigger_scan flips the job to failed and records a
        # short, safe reason on the user-facing error_message field.
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("celery broker gone")

        monkeypatch.setattr(scan_tasks.run_scan, "delay", boom)

        run_resp = await client.post(f"/api/v1/projects/{project['slug']}/scans/{scan_id}/run")
        assert run_resp.status_code == 201
        body = run_resp.json()
        assert body["status"] == "failed"
        assert body["error_message"]  # non-null: the reason is surfaced
        assert "broker" in body["error_message"].lower()

    async def test_failed_job_error_message_surfaced_via_jobs_list(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
    ) -> None:
        scan_id = await _create_scan(client, project, data_source, "Errored scan")
        curated = "Scan failed: the data source did not respond in time."

        async with TestSessionLocal() as session:
            session.add(
                ScanJob(
                    scan_config_id=uuid.UUID(scan_id),
                    status=ScanJobStatus.failed.value,
                    error_message=curated,
                )
            )
            await session.commit()

        jobs_resp = await client.get(f"/api/v1/projects/{project['slug']}/scans/{scan_id}/jobs")
        assert jobs_resp.status_code == 200
        jobs = jobs_resp.json()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "failed"
        msg = jobs[0]["error_message"]
        # The stored reason round-trips verbatim and stays short enough to render
        # in a row (curated messages are capped at ~500 chars at write time).
        assert msg == curated
        assert len(msg) <= 500

    async def test_jobs_history_supports_failing_streak_count(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
    ) -> None:
        scan_id = await _create_scan(client, project, data_source, "Streaking scan")
        config_id = uuid.UUID(scan_id)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        failed_msg = "Scan failed: could not connect to the data source."

        # One old success followed by three consecutive recent failures.
        async with TestSessionLocal() as session:
            session.add_all(
                [
                    ScanJob(
                        scan_config_id=config_id,
                        status=ScanJobStatus.completed.value,
                        created_at=base,
                    ),
                    ScanJob(
                        scan_config_id=config_id,
                        status=ScanJobStatus.failed.value,
                        error_message=failed_msg,
                        created_at=base + timedelta(hours=1),
                    ),
                    ScanJob(
                        scan_config_id=config_id,
                        status=ScanJobStatus.failed.value,
                        error_message=failed_msg,
                        created_at=base + timedelta(hours=2),
                    ),
                    ScanJob(
                        scan_config_id=config_id,
                        status=ScanJobStatus.failed.value,
                        error_message=failed_msg,
                        created_at=base + timedelta(hours=3),
                    ),
                ]
            )
            await session.commit()

        jobs_resp = await client.get(f"/api/v1/projects/{project['slug']}/scans/{scan_id}/jobs")
        assert jobs_resp.status_code == 200
        jobs = jobs_resp.json()
        # Newest-first ordering is the contract the settings page relies on to
        # compute "failed last N runs" from leading consecutive failures.
        assert [j["status"] for j in jobs] == [
            "failed",
            "failed",
            "failed",
            "completed",
        ]
        streak = 0
        for job in jobs:
            if job["status"] != ScanJobStatus.failed.value:
                break
            streak += 1
        assert streak == 3

    async def test_run_again_dispatches_manual_scan(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scan_id = await _create_scan(client, project, data_source, "Rerun scan")
        dispatched: list[tuple[str, str]] = []

        def fake_delay(scan_config_id: str, scan_job_id: str) -> None:
            dispatched.append((scan_config_id, scan_job_id))

        monkeypatch.setattr(scan_tasks.run_scan, "delay", fake_delay)

        # "Run again" on the settings page reuses this manual-scan trigger.
        run_resp = await client.post(f"/api/v1/projects/{project['slug']}/scans/{scan_id}/run")
        assert run_resp.status_code == 201
        body = run_resp.json()
        assert body["status"] == "pending"
        assert dispatched == [(scan_id, body["id"])]
