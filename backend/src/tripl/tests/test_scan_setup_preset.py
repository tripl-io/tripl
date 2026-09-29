"""The "event + properties" scan setup preset (F23.4c, #306).

The preset is a mapping onto the ordinary config fields, so most of what it
does is already covered by the single-event-type tests. These pin what is new:
the mapping and its validation, the event type the API files a preset scan
under, the columns a run reads, the fields it ensures, and what the preview and
the dry run say about it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.preview import build_preview_payload, summarize_event_properties
from tripl.core.scan_setup_preset import PRESET_EVENT_TYPE_NAME, apply_setup_preset
from tripl.models import Base, DataSource, Project, ScanConfig, ScanJob
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.variable import Variable
from tripl.worker.tasks import scan as scan_tasks
from tripl.worker.tasks import scan_dry_run as dry_run_tasks
from tripl.worker.tasks._errors import ScanError
from tripl.worker.utils.scan_preset import preset_scan_columns

# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def _preset_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "setup_preset": "event_properties",
        "event_name_column": "event",
        "properties_column": "properties",
        "event_type_column": None,
        "event_name_format": None,
        "json_value_paths": [],
        "event_group_rules": [],
        "time_column": "ts",
        "app_version_column": None,
        "platform_column": None,
    }
    fields.update(overrides)
    return fields


class TestApplySetupPreset:
    def test_preset_derives_the_naming_fields(self) -> None:
        derived = apply_setup_preset(_preset_fields(), explicit=set())
        assert derived == {
            "event_name_column": "event",
            "properties_column": "properties",
            "event_type_column": None,
            "event_name_format": "{event}",
            "json_value_paths": [],
            "event_group_rules": [],
        }

    def test_merged_custom_values_are_overwritten_not_refused(self) -> None:
        # Switching a saved custom scan to the preset: its old format, paths and
        # group rules come from the stored config, not from the caller.
        derived = apply_setup_preset(
            _preset_fields(
                event_type_column="category",
                event_name_format="{action}",
                json_value_paths=["properties.plan"],
                event_group_rules=[{"name": "x"}],
            ),
            explicit={"setup_preset", "event_name_column", "properties_column"},
        )
        assert derived["event_name_format"] == "{event}"
        assert derived["json_value_paths"] == []
        assert derived["event_type_column"] is None
        assert derived["event_group_rules"] == []

    @pytest.mark.parametrize(
        ("overrides", "explicit", "message"),
        [
            ({"properties_column": None}, set(), "needs properties_column"),
            ({"event_name_column": "  "}, set(), "needs event_name_column"),
            ({"properties_column": "event"}, set(), "must be different"),
            ({"time_column": "event"}, set(), "cannot also be the time_column"),
            (
                {"json_value_paths": ["properties.plan"]},
                {"json_value_paths"},
                "json_value_paths must be empty",
            ),
            (
                {"event_type_column": "category"},
                {"event_type_column"},
                "event_type_column must be empty",
            ),
            (
                {"event_name_format": "{action}"},
                {"event_name_format"},
                "derives event_name_format",
            ),
        ],
    )
    def test_refusals(self, overrides: dict[str, object], explicit: set[str], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            apply_setup_preset(_preset_fields(**overrides), explicit=explicit)

    def test_custom_clears_the_preset_columns(self) -> None:
        derived = apply_setup_preset(
            _preset_fields(setup_preset="custom"), explicit={"setup_preset"}
        )
        assert derived == {"event_name_column": None, "properties_column": None}

    def test_custom_refuses_a_preset_column_it_was_sent(self) -> None:
        with pytest.raises(ValueError, match="only used by the event_properties"):
            apply_setup_preset(
                _preset_fields(setup_preset="custom"), explicit={"properties_column"}
            )


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Preset", "slug": "preset", "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Preset CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201
    return resp.json()


def _preset_body(data_source: dict, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "data_source_id": data_source["id"],
        "name": "Events",
        "base_query": "SELECT * FROM analytics.events",
        "setup_preset": "event_properties",
        "event_name_column": "event",
        "properties_column": "properties",
        "time_column": "ts",
    }
    body.update(overrides)
    return body


class TestPresetApi:
    async def test_create_derives_fields_and_files_events_under_the_preset_type(
        self, client: AsyncClient, project: dict, data_source: dict
    ) -> None:
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans", json=_preset_body(data_source)
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["setup_preset"] == "event_properties"
        assert data["event_name_column"] == "event"
        assert data["properties_column"] == "properties"
        assert data["event_name_format"] == "{event}"
        assert data["json_value_paths"] == []
        assert data["event_type_column"] is None
        assert data["event_type_id"] is not None

        types = await client.get(f"/api/v1/projects/{project['slug']}/event-types")
        by_id = {et["id"]: et for et in types.json()}
        assert by_id[data["event_type_id"]]["name"] == PRESET_EVENT_TYPE_NAME

        # A second preset scan reuses the same event type rather than a twin.
        second = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json=_preset_body(data_source, name="More events"),
        )
        assert second.status_code == 201, second.text
        assert second.json()["event_type_id"] == data["event_type_id"]

    async def test_create_keeps_a_chosen_event_type(
        self, client: AsyncClient, project: dict, data_source: dict
    ) -> None:
        et = await client.post(
            f"/api/v1/projects/{project['slug']}/event-types",
            json={"name": "web", "display_name": "Web"},
        )
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json=_preset_body(data_source, event_type_id=et.json()["id"]),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["event_type_id"] == et.json()["id"]

    async def test_create_refuses_json_value_paths(
        self, client: AsyncClient, project: dict, data_source: dict
    ) -> None:
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json=_preset_body(data_source, json_value_paths=["properties.plan"]),
        )
        assert resp.status_code == 422
        assert "json_value_paths must be empty" in resp.text

    async def test_create_refuses_a_missing_properties_column(
        self, client: AsyncClient, project: dict, data_source: dict
    ) -> None:
        body = _preset_body(data_source)
        del body["properties_column"]
        resp = await client.post(f"/api/v1/projects/{project['slug']}/scans", json=body)
        assert resp.status_code == 422
        assert "needs properties_column" in resp.text

    async def test_update_switches_a_custom_scan_to_the_preset_and_back(
        self, client: AsyncClient, project: dict, data_source: dict
    ) -> None:
        et = await client.post(
            f"/api/v1/projects/{project['slug']}/event-types",
            json={"name": "web", "display_name": "Web"},
        )
        created = await client.post(
            f"/api/v1/projects/{project['slug']}/scans",
            json={
                "data_source_id": data_source["id"],
                "name": "Custom",
                "base_query": "SELECT * FROM analytics.events",
                "event_type_id": et.json()["id"],
                "event_name_format": "{action}",
                "json_value_paths": ["properties.plan"],
            },
        )
        assert created.status_code == 201, created.text
        scan_url = f"/api/v1/projects/{project['slug']}/scans/{created.json()['id']}"

        to_preset = await client.patch(
            scan_url,
            json={
                "setup_preset": "event_properties",
                "event_name_column": "event",
                "properties_column": "properties",
            },
        )
        assert to_preset.status_code == 200, to_preset.text
        data = to_preset.json()
        assert data["event_name_format"] == "{event}"
        assert data["json_value_paths"] == []
        assert data["event_type_id"] == et.json()["id"]

        # An edit that leaves the preset alone keeps it.
        renamed = await client.patch(scan_url, json={"name": "Renamed"})
        assert renamed.json()["setup_preset"] == "event_properties"
        assert renamed.json()["properties_column"] == "properties"

        conflicting = await client.patch(scan_url, json={"json_value_paths": ["properties.x"]})
        assert conflicting.status_code == 422

        back = await client.patch(scan_url, json={"setup_preset": "custom"})
        assert back.status_code == 200, back.text
        assert back.json()["setup_preset"] == "custom"
        assert back.json()["event_name_column"] is None
        assert back.json()["properties_column"] is None

    async def test_dry_run_draft_accepts_a_preset_without_an_event_type(
        self, client: AsyncClient, project: dict, data_source: dict, monkeypatch
    ) -> None:
        monkeypatch.setattr(dry_run_tasks.dry_run_scan_config_async, "delay", lambda *a: None)
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={
                "data_source_id": data_source["id"],
                "base_query": "SELECT * FROM analytics.events",
                "setup_preset": "event_properties",
                "event_name_column": "event",
                "properties_column": "properties",
            },
        )
        assert resp.status_code == 202, resp.text


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

_COLUMNS = [
    ColumnInfo(name="event", type_name="String"),
    ColumnInfo(name="properties", type_name="JSON"),
    ColumnInfo(name="user_id", type_name="String"),
    ColumnInfo(name="country", type_name="String"),
]


def _config(**overrides: object) -> ScanConfig:
    config = ScanConfig(
        project_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(),
        name="preset",
        base_query="SELECT * FROM events",
        setup_preset="event_properties",
        event_name_column="event",
        properties_column="properties",
        event_name_format="{event}",
        json_value_paths=[],
        event_group_rules=[],
        metric_breakdown_columns=[],
        distribution_drift_fields=[],
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class TestPresetScanColumns:
    def test_reads_only_the_preset_columns_and_what_other_settings_name(self) -> None:
        kept = preset_scan_columns(_config(platform_column="country"), list(_COLUMNS))
        assert [column.name for column in kept] == ["event", "properties", "country"]

    def test_custom_config_reads_every_column(self) -> None:
        config = _config(setup_preset="custom")
        assert preset_scan_columns(config, list(_COLUMNS)) == _COLUMNS

    def test_refuses_a_properties_column_that_is_not_json(self) -> None:
        with pytest.raises(ScanError, match="not a JSON column"):
            preset_scan_columns(_config(properties_column="user_id"), list(_COLUMNS))

    def test_refuses_a_column_missing_from_the_query(self) -> None:
        with pytest.raises(ScanError, match="not in the base query"):
            preset_scan_columns(_config(event_name_column="name"), list(_COLUMNS))


class _PresetAdapter:
    """``event`` + ``properties`` rows; records what the breakdown was asked for.

    Row layout is ``get_full_breakdown``'s: regular values, the JSON path arrays,
    then ``_cnt``.
    """

    def __init__(self) -> None:
        self.breakdown_calls: list[tuple[list[str], list[str]]] = []

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [*_COLUMNS, ColumnInfo(name="ts", type_name="DateTime")]

    def get_full_breakdown(
        self,
        base_query: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None = None,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        limit: int = 50000,
    ) -> tuple[list[str], list[str], list[str], list[tuple[object, ...]]]:
        self.breakdown_calls.append((list(regular_columns), list(json_columns)))
        rows: list[tuple[object, ...]] = [
            ("page_view", ["url", "referrer"], 10),
            ("page_view", ["url"], 5),
            ("signup", ["plan"], 3),
        ]
        return (["event"], ["properties"], [], rows[:limit])

    def close(self) -> None:
        return None


def _seed_preset_scan(session_factory, *, with_event_type: bool) -> tuple[uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    data_source_id = uuid.uuid4()
    config_id = uuid.uuid4()
    job_id = uuid.uuid4()
    event_type_id = uuid.uuid4()
    with session_factory() as session:
        session.add_all(
            [
                Project(id=project_id, name="P", slug="p-preset", description=""),
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
            ]
        )
        if with_event_type:
            session.add(
                EventType(
                    id=event_type_id,
                    project_id=project_id,
                    name="web",
                    display_name="Web",
                    description="",
                )
            )
        session.flush()
        session.add_all(
            [
                ScanConfig(
                    id=config_id,
                    project_id=project_id,
                    data_source_id=data_source_id,
                    event_type_id=event_type_id if with_event_type else None,
                    name="Preset",
                    base_query="SELECT * FROM events",
                    time_column="ts",
                    setup_preset="event_properties",
                    event_name_column="event",
                    properties_column="properties",
                    event_name_format="{event}",
                ),
                ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
            ]
        )
        session.commit()
    return config_id, job_id


class TestPresetRun:
    @pytest.mark.parametrize("with_event_type", [True, False])
    def test_run_names_events_by_the_event_column_and_catalogues_every_key(
        self, tmp_path, monkeypatch, with_event_type: bool
    ) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'preset_run.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            config_id, job_id = _seed_preset_scan(factory, with_event_type=with_event_type)
            adapter = _PresetAdapter()
            for name, value in (
                ("_get_sync_session", factory),
                ("_build_adapter", lambda ds: adapter),
                ("reindex_main_branch_from_worker", lambda session, project_id: None),
            ):
                monkeypatch.setitem(scan_tasks.run_scan.run.__globals__, name, value)

            summary = scan_tasks.run_scan.run(str(config_id), str(job_id))

            # The JSON column no longer splits events, and the other columns of
            # ``SELECT *`` never reach the breakdown.
            assert adapter.breakdown_calls == [(["event"], ["properties"])]
            assert summary["events_created"] == 2
            with factory() as session:
                config = session.get(ScanConfig, config_id)
                assert config is not None and config.event_type_id is not None
                event_type = session.get(EventType, config.event_type_id)
                assert event_type is not None
                assert event_type.name == ("web" if with_event_type else PRESET_EVENT_TYPE_NAME)
                fields = {
                    fd.name: fd.field_type
                    for fd in session.scalars(
                        select(FieldDefinition).where(
                            FieldDefinition.event_type_id == event_type.id
                        )
                    )
                }
                assert fields == {"event": "string", "properties": "json"}
                names = set(
                    session.scalars(select(Event.name).where(Event.event_type_id == event_type.id))
                )
                assert names == {"page_view", "signup"}
                variables = set(session.scalars(select(Variable.source_name)))
                assert {
                    "properties.url",
                    "properties.referrer",
                    "properties.plan",
                } <= variables
        finally:
            engine.dispose()

    def test_run_fails_with_a_readable_message_on_a_string_properties_column(
        self, tmp_path, monkeypatch
    ) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'preset_bad.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            config_id, job_id = _seed_preset_scan(factory, with_event_type=True)
            with factory() as session:
                config = session.get(ScanConfig, config_id)
                assert config is not None
                config.properties_column = "user_id"
                session.commit()
            for name, value in (
                ("_get_sync_session", factory),
                ("_build_adapter", lambda ds: _PresetAdapter()),
            ):
                monkeypatch.setitem(scan_tasks.run_scan.run.__globals__, name, value)

            with pytest.raises(ScanError):
                scan_tasks.run_scan.run(str(config_id), str(job_id))
            with factory() as session:
                job = session.get(ScanJob, job_id)
                assert job is not None and job.status == "failed"
                assert job.error_message is not None
                assert "not a JSON column" in job.error_message
        finally:
            engine.dispose()


class TestPresetDryRun:
    def test_draft_without_an_event_type_plans_the_preset_type_and_two_fields(
        self, tmp_path, monkeypatch
    ) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'preset_dry.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            project_id = uuid.uuid4()
            data_source_id = uuid.uuid4()
            job_id = uuid.uuid4()
            with factory() as session:
                session.add_all(
                    [
                        Project(id=project_id, name="P", slug="p-dry", description=""),
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
                    ]
                )
                session.flush()
                session.add(
                    ScanDryRunJob(
                        id=job_id,
                        project_id=project_id,
                        data_source_id=data_source_id,
                        base_query="SELECT * FROM events",
                        time_column="ts",
                        setup_preset="event_properties",
                        event_name_column="event",
                        properties_column="properties",
                        event_name_format="{event}",
                        status="pending",
                    )
                )
                session.commit()

            with factory() as session:
                job = session.get(ScanDryRunJob, job_id)
                assert job is not None
                config = dry_run_tasks._dry_run_config(session, job)
                payload = dry_run_tasks.build_dry_run_payload(
                    session, _PresetAdapter(), config, sample_row_limit=5000
                )

            assert {event["name"] for event in payload["events"]} == {"page_view", "signup"}
            assert {event["event_type"] for event in payload["events"]} == {PRESET_EVENT_TYPE_NAME}
            assert {(field["name"], field["type"]) for field in payload["fields"]} == {
                ("event", "string"),
                ("properties", "json"),
            }
            assert any("would be added" in warning for warning in payload["warnings"])
            assert payload["unmapped_columns"] == []
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


class TestPresetPreview:
    def test_summary_lists_each_event_with_its_keys_presence_and_type(self) -> None:
        rows: list[dict[str, object]] = [
            {"event": "page_view", "properties": '{"url": "/a", "ms": 12}'},
            {"event": "page_view", "properties": {"url": "/b"}},
            {"event": "signup", "properties": {"plan": "pro", "seats": 3}},
            {"event": None, "properties": {"ignored": True}},
        ]
        summary = summarize_event_properties(
            _COLUMNS, rows, event_name_column="event", properties_column="properties"
        )
        assert summary["error"] is None
        assert summary["sample_rows"] == 3
        events = summary["events"]
        assert isinstance(events, list)
        assert [event["name"] for event in events] == ["page_view", "signup"]
        page_view = {prop["path"]: prop for prop in events[0]["properties"]}
        assert page_view["url"]["presence"] == 1.0
        assert page_view["url"]["type"] == "string"
        assert page_view["url"]["sample_values"] == ["/a", "/b"]
        assert page_view["ms"]["presence"] == 0.5
        assert page_view["ms"]["type"] == "number"

    def test_summary_reports_a_non_json_properties_column(self) -> None:
        summary = summarize_event_properties(
            _COLUMNS, [], event_name_column="event", properties_column="user_id"
        )
        assert summary["events"] == []
        assert "not a JSON column" in str(summary["error"])

    def test_preview_payload_carries_the_summary_only_when_asked(self) -> None:
        class _Adapter:
            def test_connection(self) -> bool:
                return True

            def get_columns(self, base_query: str) -> list[ColumnInfo]:
                return list(_COLUMNS)

            def get_preview_rows(self, base_query: str, **_: object):
                return (
                    ["event", "properties", "user_id", "country"],
                    [("page_view", '{"url": "/a"}', "u1", "de")],
                )

        plain = build_preview_payload(_Adapter(), "SELECT 1", 10)  # type: ignore[arg-type]
        assert "event_properties" not in plain
        with_preset = build_preview_payload(
            _Adapter(),  # type: ignore[arg-type]
            "SELECT 1",
            10,
            event_name_column="event",
            properties_column="properties",
        )
        summary = with_preset["event_properties"]
        assert isinstance(summary, dict)
        assert summary["events"][0]["name"] == "page_view"
