"""Seam follow-ups after the design and UX review.

The draft destination test reached through ``alerting_service``, a
catalog run's warehouse rows counted as warehouse rows in the 24h scan
activity, and the demo's shadow candidate carrying samples.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.models.scan_job import ScanJob
from tripl.services import _alerting_test_send, alerting_service
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alert_rule_and_summary_contracts import _project_with_scan


def test_alerting_service_reexports_the_draft_test_send() -> None:
    assert (
        alerting_service.send_draft_destination_test
        is _alerting_test_send.send_draft_destination_test
    )
    assert "send_draft_destination_test" in alerting_service.__all__


@pytest.mark.asyncio
async def test_scan_activity_counts_catalog_warehouse_rows_as_warehouse_rows(
    client: AsyncClient,
) -> None:
    ids = await _project_with_scan(client, "seam-rows")
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        for summary in (
            # A metrics run.
            {"query_rows_scanned": 1_000},
            # A newer catalog run: its warehouse rows, not its 40 combinations.
            {"scan_rows_processed": 40, "catalog_rows_scanned": 5_000},
            # An older catalog run that reported combinations only.
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

    resp = await client.get("/api/v1/projects/seam-rows/scans/activity")
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    assert item["warehouse_rows_24h"] == 6_000
    assert item["catalog_combinations_24h"] == 153
    # The split pair still sums to the mixed total.
    assert item["rows_read_24h"] == 6_153


@pytest.mark.asyncio
async def test_demo_shadow_candidate_carries_samples(client: AsyncClient) -> None:
    created = await client.post("/api/v1/projects/demo")
    assert created.status_code == 202
    slug = created.json()["slug"]

    resp = await client.get(f"/api/v1/projects/{slug}/reconciliation/shadow-events")
    assert resp.status_code == 200
    candidate = next(
        item for item in resp.json()["items"] if item["event_name"] == "app_heartbeat_v1"
    )
    samples = candidate["sample_properties"]
    assert samples
    assert all(sample["event_name"] == "app_heartbeat_v1" for sample in samples)
