"""Backend asks from design-review batches 13-20.

JR-2 (``ProjectSummary.metric_count``), MT-25 (metric list ``reviewed`` /
``owner_id`` filters), MT-30 (fact-table list rollups), AL-14 (inbox
``status_counts``), ST-39 (reset ``dry_run``), B15 (split 24h scan rows, the
readable row-limit defaults), DA-40 (data-source usage counts), AU-37 (meta
field usage), ST-24 (``email_configured`` on ``/auth/status``), ST-30 (AI prompt
defaults), AU-29 (variable ``event_refs``), AU-13 (``PATCH`` a relation) and
AL-30 (test-send ``error_kind``).
"""

from __future__ import annotations

import smtplib
import socket
import ssl
import urllib.error
import uuid
from datetime import UTC, datetime, timedelta
from email.message import Message

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.audit_log import AuditLog
from tripl.models.fact_table import FactTable
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_job import ScanJob
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.services import ai_defaults, datasource_service
from tripl.services._alerting_test_send import classify_test_send_error
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alerting import _inbox_item, _seed_inbox_delivery, _seed_inbox_fixture
from tripl.tests.test_design_review_b5_12 import _project_with_scan
from tripl.tests.test_projects import (
    _IN_PERIOD,
    _RESET_BEFORE,
    _anomaly,
    _setup_reset_project,
)

# --------------------------------------------------------------------------- #
# metric_count on the project summary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_project_summary_counts_metrics(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "Empty", "slug": "jr2-empty"})
    empty = await client.get("/api/v1/projects/jr2-empty")
    assert empty.json()["summary"]["metric_count"] == 0

    await _setup_reset_project(client, "jr2-metrics")
    detail = await client.get("/api/v1/projects/jr2-metrics")
    assert detail.status_code == 200, detail.text
    assert detail.json()["summary"]["metric_count"] == 1
    listed = await client.get("/api/v1/projects")
    by_slug = {item["slug"]: item for item in listed.json()}
    assert by_slug["jr2-metrics"]["summary"]["metric_count"] == 1
    assert by_slug["jr2-empty"]["summary"]["metric_count"] == 0


# --------------------------------------------------------------------------- #
# reviewed / owner_id filters on the metrics list
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_metric_list_filters_by_reviewed_and_owner(client: AsyncClient) -> None:
    ids = await _setup_reset_project(client, "mt25")
    me = (await client.get("/api/v1/auth/me")).json()
    async with TestSessionLocal() as session:
        session.add(
            MetricDefinition(
                id=uuid.uuid4(),
                project_id=uuid.UUID(ids["project_id"]),
                name="reviewed_metric",
                display_name="Reviewed metric",
                kind="sql",
                config={},
                status="active",
                reviewed=True,
                owner_id=uuid.UUID(me["id"]),
            )
        )
        await session.commit()

    reviewed = await client.get("/api/v1/projects/mt25/metrics", params={"reviewed": "true"})
    assert reviewed.status_code == 200, reviewed.text
    assert [item["name"] for item in reviewed.json()["items"]] == ["reviewed_metric"]
    assert reviewed.json()["total"] == 1

    unreviewed = await client.get("/api/v1/projects/mt25/metrics", params={"reviewed": "false"})
    assert [item["name"] for item in unreviewed.json()["items"]] == ["conversion_rate"]

    owned = await client.get("/api/v1/projects/mt25/metrics", params={"owner_id": me["id"]})
    assert [item["name"] for item in owned.json()["items"]] == ["reviewed_metric"]

    everything = await client.get("/api/v1/projects/mt25/metrics")
    assert everything.json()["total"] == 2


# --------------------------------------------------------------------------- #
# fact-table list rollups
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fact_table_list_reports_metric_column_and_identifier_counts(
    client: AsyncClient,
) -> None:
    ids = await _setup_reset_project(client, "mt30")
    project_id = uuid.UUID(ids["project_id"])
    used_id, denominator_id, unused_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as session:
        for fact_id, name in (
            (used_id, "orders"),
            (denominator_id, "sessions"),
            (unused_id, "refunds"),
        ):
            session.add(
                FactTable(
                    id=fact_id,
                    project_id=project_id,
                    name=name,
                    display_name=name.title(),
                    sql=f"SELECT ts, amount, user_id FROM {name}",
                    timestamp_column="ts",
                    columns=[
                        {"name": "ts", "type": "timestamp"},
                        {"name": "amount", "type": "number"},
                        {"name": "user_id", "type": "string"},
                    ],
                    identifier_columns=["user_id"],
                    row_filters=[],
                )
            )
        await session.flush()
        session.add(
            MetricDefinition(
                id=uuid.uuid4(),
                project_id=project_id,
                name="order_total",
                display_name="Order total",
                kind="fact",
                config={},
                fact_table_id=used_id,
            )
        )
        # A cross-table ratio names its denominator's table only inside
        # ``config``; that table is still in use (review F7).
        session.add(
            MetricDefinition(
                id=uuid.uuid4(),
                project_id=project_id,
                name="orders_per_session",
                display_name="Orders per session",
                kind="fact",
                config={
                    "numerator": {"fact_table_id": str(used_id)},
                    "denominator": {"fact_table_id": str(denominator_id)},
                },
                fact_table_id=used_id,
            )
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/mt30/fact-tables")
    assert resp.status_code == 200, resp.text
    rows = {item["name"]: item for item in resp.json()["items"]}
    assert rows["orders"]["metric_count"] == 2
    assert rows["sessions"]["metric_count"] == 1
    assert rows["refunds"]["metric_count"] == 0
    assert rows["orders"]["column_count"] == 3
    assert rows["orders"]["identifier_count"] == 1


# --------------------------------------------------------------------------- #
# status_counts on the inbox
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_alert_inbox_always_reports_status_counts(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "Inbox", "slug": "al14"})
    for params in ({}, {"status": "muted"}):
        resp = await client.get("/api/v1/projects/al14/alert-inbox", params=params)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status_counts"] == {
            "open": 0,
            "acknowledged": 0,
            "muted": 0,
            "resolved": 0,
            "false_positive": 0,
        }


@pytest.mark.asyncio
async def test_alert_inbox_status_counts_follow_the_other_filters(client: AsyncClient) -> None:
    """Counts respect every filter but ``status``, include rescued silenced
    orphans, and a ``status`` param only narrows the item list (review F50)."""
    project = await client.post("/api/v1/projects", json={"name": "Counts", "slug": "al14-n"})
    project_id = uuid.UUID(project.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    now = datetime.now(UTC)
    aged = now - timedelta(days=45)
    fresh = now - timedelta(hours=1)
    open_drop, acked_spike, muted_drop, resolved_spike = (uuid.uuid4() for _ in range(4))
    for group_id, at, delta in (
        (open_drop, fresh, -50.0),
        (acked_spike, fresh, 100.0),
        # Both aged out of the window: only the silenced-orphan rescue lists them.
        (muted_drop, aged, -50.0),
        (resolved_spike, aged, 100.0),
    ):
        await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            created_at=at,
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=at,
                    percent_delta=delta,
                    correlation_group_id=group_id,
                )
            ],
        )
    base = "/api/v1/projects/al14-n/alert-inbox"
    for group_id, body in (
        (acked_spike, {"action": "acknowledge"}),
        (muted_drop, {"action": "mute", "muted_until": None}),
        (resolved_spike, {"action": "resolve"}),
    ):
        acted = await client.post(f"{base}/{group_id}/actions", json=body)
        assert acted.status_code == 200, acted.text

    everything = await client.get(base)
    assert everything.status_code == 200, everything.text
    assert everything.json()["status_counts"] == {
        "open": 1,
        "acknowledged": 1,
        "muted": 1,
        "resolved": 1,
        "false_positive": 0,
    }
    drops = await client.get(base, params={"direction": "drop"})
    drop_counts = {"open": 1, "acknowledged": 0, "muted": 1, "resolved": 0, "false_positive": 0}
    assert drops.json()["status_counts"] == drop_counts

    for params, listing in (({}, everything.json()), ({"direction": "drop"}, drops.json())):
        for status in ("open", "acknowledged", "muted", "resolved", "false_positive"):
            narrowed = await client.get(base, params={**params, "status": status})
            assert narrowed.status_code == 200, narrowed.text
            # Same counts with or without the status param...
            assert narrowed.json()["status_counts"] == listing["status_counts"]
            # ...and the items are exactly the unfiltered list's slice.
            expected = [
                item["correlation_group_id"]
                for item in listing["items"]
                if item["status"] == status
            ]
            assert [item["correlation_group_id"] for item in narrowed.json()["items"]] == expected
            assert narrowed.json()["total"] == len(expected)

    muted_only = await client.get(base, params={"status": "muted"})
    assert [item["correlation_group_id"] for item in muted_only.json()["items"]] == [
        str(muted_drop)
    ]


# --------------------------------------------------------------------------- #
# dry-run danger-zone resets
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reset_anomalies_dry_run_counts_without_deleting(client: AsyncClient) -> None:
    ids = await _setup_reset_project(client, "st39")
    async with TestSessionLocal() as session:
        session.add(
            _anomaly(
                ids["scan_config_id"],
                "event_type",
                ids["event_type_id"],
                _IN_PERIOD,
                event_type_id=ids["event_type_id"],
            )
        )
        await session.commit()

    preview = await client.post(
        "/api/v1/projects/st39/danger/reset-anomalies",
        json={"before": _RESET_BEFORE, "dry_run": True},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "metric_anomalies": 1,
        "metric_breakdown_anomalies": 0,
        "metric_baselines": 0,
    }
    async with TestSessionLocal() as session:
        assert len((await session.execute(select(MetricAnomaly))).scalars().all()) == 1
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
        assert "project.reset_anomalies" not in actions

    drifts = await client.post("/api/v1/projects/st39/danger/reset-drifts", json={"dry_run": True})
    assert drifts.status_code == 200, drifts.text
    assert drifts.json() == {"schema_drifts": 0, "distribution_drifts": 0}

    # Omitting dry_run still deletes, as it always has.
    real = await client.post(
        "/api/v1/projects/st39/danger/reset-anomalies", json={"before": _RESET_BEFORE}
    )
    assert real.json() == {
        "metric_anomalies": 1,
        "metric_breakdown_anomalies": 0,
        "metric_baselines": 0,
    }
    async with TestSessionLocal() as session:
        assert (await session.execute(select(MetricAnomaly))).scalars().all() == []


# --------------------------------------------------------------------------- #
# B15: split 24h rows and readable row-limit defaults
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_scan_activity_splits_warehouse_rows_from_catalog_combinations(
    client: AsyncClient,
) -> None:
    ids = await _project_with_scan(client, "b15-rows")
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        for summary in (
            {"query_rows_scanned": 1_000},
            {"scan_rows_processed": 153},
        ):
            session.add(
                ScanJob(
                    id=uuid.uuid4(),
                    scan_config_id=uuid.UUID(ids["scan_config_id"]),
                    status="completed",
                    started_at=now - timedelta(hours=1),
                    completed_at=now - timedelta(minutes=50),
                    result_summary=summary,
                    error_message=None,
                    created_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(minutes=50),
                )
            )
        await session.commit()

    resp = await client.get("/api/v1/projects/b15-rows/scans/activity")
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    assert item["warehouse_rows_24h"] == 1_000
    assert item["catalog_combinations_24h"] == 153
    assert item["rows_read_24h"] == 1_153


@pytest.mark.asyncio
async def test_row_limit_defaults_are_readable(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/settings/row-limits")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scan_row_limit_default"] >= 1
    assert body["metrics_row_limit_default"] >= 1


# --------------------------------------------------------------------------- #
# data-source usage
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_data_sources_report_scan_and_run_counts(client: AsyncClient) -> None:
    ids = await _project_with_scan(client, "da40")
    async with TestSessionLocal() as session:
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(ids["scan_config_id"]),
                status="completed",
                result_summary={},
                error_message=None,
            )
        )
        await session.commit()

    listed = await client.get("/api/v1/data-sources")
    assert listed.status_code == 200, listed.text
    (source,) = [item for item in listed.json() if item["name"] == "Warehouse da40"]
    assert source["scan_count"] == 1
    assert source["scan_run_count"] == 1

    # Served from the list cache the second time, and still counted fresh.
    again = await client.get("/api/v1/data-sources")
    (cached,) = [item for item in again.json() if item["name"] == "Warehouse da40"]
    assert cached["scan_count"] == 1

    single = await client.get(f"/api/v1/data-sources/{source['id']}")
    assert single.json()["scan_run_count"] == 1


@pytest.mark.asyncio
async def test_tested_and_edited_sources_keep_their_usage_counts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page writes these responses straight into its list cache, so a bare
    0 would tell the delete confirm that nothing reads the source (review F47)."""
    await _project_with_scan(client, "da40-test")
    listed = await client.get("/api/v1/data-sources")
    (source,) = [item for item in listed.json() if item["name"] == "Warehouse da40-test"]
    monkeypatch.setattr(datasource_service, "_run_adapter_test", lambda _ds: (True, "ok"))

    tested = await client.post(f"/api/v1/data-sources/{source['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["data_source"]["scan_count"] == 1

    edited = await client.patch(
        f"/api/v1/data-sources/{source['id']}", json={"timeout_seconds": 45}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["scan_count"] == 1


# --------------------------------------------------------------------------- #
# meta-field usage
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_meta_field_usage_counts_values_and_events(client: AsyncClient) -> None:
    ids = await _project_with_scan(client, "au37")
    meta = await client.post(
        "/api/v1/projects/au37/meta-fields",
        json={"name": "ticket", "display_name": "Ticket", "field_type": "string"},
    )
    assert meta.status_code == 201, meta.text
    meta_id = meta.json()["id"]

    empty = await client.get(f"/api/v1/projects/au37/meta-fields/{meta_id}/usage")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"value_count": 0, "event_count": 0}

    for name, value in (("Signed Up", "T-1"), ("Logged In", "T-2"), ("Logged Out", "")):
        created = await client.post(
            "/api/v1/projects/au37/events",
            json={
                "event_type_id": ids["event_type_id"],
                "name": name,
                "meta_values": [{"meta_field_definition_id": meta_id, "value": value}],
            },
        )
        assert created.status_code == 201, created.text

    usage = await client.get(f"/api/v1/projects/au37/meta-fields/{meta_id}/usage")
    assert usage.json() == {"value_count": 2, "event_count": 2}

    missing = await client.get(f"/api/v1/projects/au37/meta-fields/{uuid.uuid4()}/usage")
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# ST-24 / ST-30: auth status email flag, AI prompt defaults
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auth_status_says_whether_email_can_send(anon_client: AsyncClient) -> None:
    resp = await anon_client.get("/api/v1/auth/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_configured"] is False


@pytest.mark.asyncio
async def test_ai_prompt_defaults_are_the_built_in_prompts(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/settings/ai/defaults")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "describe_system_prompt": ai_defaults.DEFAULT_DESCRIBE_SYSTEM_PROMPT,
        "ask_system_prompt": ai_defaults.DEFAULT_ASK_SYSTEM_PROMPT,
        "alert_explanation_system_prompt": ai_defaults.DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    }


# --------------------------------------------------------------------------- #
# variable event refs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_variable_list_carries_event_refs(client: AsyncClient) -> None:
    ids = await _project_with_scan(client, "au29")
    field = await client.post(
        f"/api/v1/projects/au29/event-types/{ids['event_type_id']}/fields",
        json={"name": "screen", "display_name": "Screen", "field_type": "string"},
    )
    assert field.status_code == 201, field.text
    variable = await client.post(
        "/api/v1/projects/au29/variables",
        json={"name": "screen", "variable_type": "string"},
    )
    assert variable.status_code == 201, variable.text
    variable_id = uuid.UUID(variable.json()["id"])
    async with TestSessionLocal() as session:
        stored = await session.get(Variable, variable_id)
        assert stored is not None
        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=stored.project_id,
                branch_id=stored.branch_id,
                variable_id=variable_id,
                event_id=uuid.UUID(ids["event_id"]),
                field_definition_id=uuid.UUID(field.json()["id"]),
                source_column="screen",
                values=["home"],
            )
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/au29/variables")
    assert resp.status_code == 200, resp.text
    (row,) = [item for item in resp.json()["items"] if item["id"] == str(variable_id)]
    assert row["event_refs"] == [{"id": ids["event_id"], "name": "Landing Viewed"}]
    assert row["event_names"] == ["Landing Viewed"]


# --------------------------------------------------------------------------- #
# edit a relation
# --------------------------------------------------------------------------- #


async def _two_types_with_fields(client: AsyncClient, slug: str) -> dict[str, str]:
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    out: dict[str, str] = {}
    for key in ("a", "b"):
        event_type = await client.post(
            f"/api/v1/projects/{slug}/event-types",
            json={"name": f"type_{key}", "display_name": f"Type {key}"},
        )
        out[f"{key}_type"] = event_type.json()["id"]
        for field in ("user_id", "session_id"):
            created = await client.post(
                f"/api/v1/projects/{slug}/event-types/{out[f'{key}_type']}/fields",
                json={"name": field, "display_name": field, "field_type": "string"},
            )
            out[f"{key}_{field}"] = created.json()["id"]
    return out


@pytest.mark.asyncio
async def test_relation_can_be_edited_and_ends_are_rechecked(client: AsyncClient) -> None:
    ids = await _two_types_with_fields(client, "au13")
    created = await client.post(
        "/api/v1/projects/au13/relations",
        json={
            "source_event_type_id": ids["a_type"],
            "target_event_type_id": ids["b_type"],
            "source_field_id": ids["a_user_id"],
            "target_field_id": ids["b_user_id"],
        },
    )
    assert created.status_code == 201, created.text
    relation_id = created.json()["id"]

    edited = await client.patch(
        f"/api/v1/projects/au13/relations/{relation_id}",
        json={
            "source_field_id": ids["a_session_id"],
            "target_field_id": ids["b_session_id"],
            "relation_type": "has_many",
            "description": "Same session",
        },
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["source_field_id"] == ids["a_session_id"]
    assert body["relation_type"] == "has_many"
    assert body["description"] == "Same session"

    # A field of the OTHER type is refused on the source end.
    wrong = await client.patch(
        f"/api/v1/projects/au13/relations/{relation_id}",
        json={"source_field_id": ids["b_user_id"]},
    )
    assert wrong.status_code == 422

    null = await client.patch(
        f"/api/v1/projects/au13/relations/{relation_id}", json={"relation_type": None}
    )
    assert null.status_code == 422

    missing = await client.patch(
        f"/api/v1/projects/au13/relations/{uuid.uuid4()}", json={"description": "x"}
    )
    assert missing.status_code == 404

    async with TestSessionLocal() as session:
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "relation.update" in actions


# --------------------------------------------------------------------------- #
# classify a failed test send
# --------------------------------------------------------------------------- #


def _wrapped(cause: BaseException) -> ValueError:
    """A ValueError raised ``from`` *cause*, the way the channel clients wrap."""
    try:
        raise ValueError("readable message") from cause
    except ValueError as exc:
        return exc


def test_classify_test_send_error_reads_the_cause_chain() -> None:
    http_error = urllib.error.HTTPError(
        "https://hooks.slack.com/x", 403, "Forbidden", Message(), None
    )
    assert classify_test_send_error(_wrapped(http_error)) == ("http_status", 403)
    dns = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
    assert classify_test_send_error(dns) == ("dns", None)
    assert classify_test_send_error(urllib.error.URLError(TimeoutError())) == ("timeout", None)
    assert classify_test_send_error(urllib.error.URLError(ssl.SSLError())) == ("tls", None)
    assert classify_test_send_error(ConnectionRefusedError()) == ("network", None)
    assert classify_test_send_error(smtplib.SMTPAuthenticationError(535, b"no")) == (
        "smtp",
        None,
    )
    assert classify_test_send_error(ValueError("bot_token is required")) == ("config", None)
    assert classify_test_send_error(RuntimeError("boom")) == ("other", None)
