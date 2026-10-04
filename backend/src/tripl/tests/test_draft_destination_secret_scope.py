"""Review follow-ups for the backend lane of the design follow-ups change.

* F6 — a draft destination test never lends a stored secret to a host it was
  not saved for, and the audit entry records the host the test was aimed at.
* F7 — the row-sparkline series only reads buckets an open signal can carry.
* F8/F9 — a fact draft's series preview runs on the bucket grid, capped at a
  month of wall clock.
* F10 — the drilldown reports no next collection for a config the dispatcher
  never collects (no time column).
* F11 — a data source's "Used by" list is capped per source in SQL.
* F12 — a shadow sample is capped by key count and size.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.audit_log import AuditLog
from tripl.models.event_metric import EventMetric
from tripl.services import datasource_service
from tripl.services.metric_preview_service import (
    FACT_SERIES_PREVIEW_MAX_SPAN,
    FACT_SERIES_PREVIEW_MIN_BUCKETS,
    PREVIEW_WINDOW_BUCKETS,
    fact_series_preview_window,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics.chunk_processing import (
    _SHADOW_SAMPLE_CHARS_MAX,
    _SHADOW_SAMPLE_KEYS_MAX,
    _shadow_sample,
)

HOUR = timedelta(hours=1)


async def _project(client: AsyncClient, slug: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text


async def _data_source(client: AsyncClient, name: str) -> str:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": name,
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _scan(
    client: AsyncClient,
    slug: str,
    data_source_id: str,
    name: str,
    *,
    time_column: str | None = None,
) -> str:
    body: dict[str, object] = {
        "data_source_id": data_source_id,
        "name": name,
        "base_query": "SELECT 1",
        "interval": "1h",
    }
    if time_column is not None:
        body["time_column"] = time_column
    resp = await client.post(f"/api/v1/projects/{slug}/scans", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


# --------------------------------------------------------------------------- #
# F6: draft destination test keeps stored secrets on their own host
# --------------------------------------------------------------------------- #


def _capture_jira(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    from tripl.worker.tasks import alerts

    sent: list[dict[str, object]] = []

    def capture(**kwargs: object) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(alerts, "_send_jira_issue", capture)
    return sent


def _capture_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, str] | None]]:
    from tripl.worker.tasks import alerts

    posted: list[tuple[str, dict[str, str] | None]] = []

    def capture_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        posted.append((url, headers))
        return None

    monkeypatch.setattr(alerts, "_post_json", capture_post_json)
    return posted


async def _test_audits() -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return [
            row
            for row in (await session.execute(select(AuditLog))).scalars().all()
            if row.action == "alert_destination.test"
        ]


@pytest.mark.asyncio
async def test_a_draft_cannot_send_the_stored_jira_token_to_another_site(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _capture_jira(monkeypatch)
    slug = "lane-draft-jira"
    await _project(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "jira",
            "name": "Ops Jira",
            "jira_base_url": "https://example.atlassian.net",
            "jira_auth_email": "alice@example.com",
            "jira_api_token": "stored-token",
            "jira_project_key": "ENG",
        },
    )
    assert created.status_code == 201, created.text
    destination_id = created.json()["id"]
    draft = {
        "destination_id": destination_id,
        "type": "jira",
        "jira_auth_email": "alice@example.com",
        "jira_project_key": "ENG",
    }
    url = f"/api/v1/projects/{slug}/alert-destinations/test"

    moved = await client.post(url, json={**draft, "jira_base_url": "https://attacker.example.com"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["ok"] is False
    assert moved.json()["error_kind"] == "config"
    assert sent == []

    # Same site (trailing slash and case aside): the stored token is lent.
    kept = await client.post(url, json={**draft, "jira_base_url": "https://Example.atlassian.net/"})
    assert kept.json()["ok"] is True, kept.text
    # A token typed into the draft goes wherever the draft points.
    typed = await client.post(
        url,
        json={
            **draft,
            "jira_base_url": "https://other.atlassian.net",
            "jira_api_token": "typed-token",
        },
    )
    assert typed.json()["ok"] is True, typed.text
    assert [call["api_token"] for call in sent] == ["stored-token", "typed-token"]

    audits = await _test_audits()
    assert sorted(str(row.payload["target_origin"]) for row in audits) == [
        "https://attacker.example.com",
        "https://example.atlassian.net",
        "https://other.atlassian.net",
    ]


@pytest.mark.asyncio
async def test_a_draft_cannot_send_the_stored_header_secret_to_another_host(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _capture_posts(monkeypatch)
    slug = "lane-draft-webhook"
    await _project(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "webhook",
            "name": "Hook",
            "target_url": "https://hooks.example.com/tripl?token=abc",
            "webhook_header_name": "X-Secret",
            "webhook_header_value": "s3cret",
        },
    )
    assert created.status_code == 201, created.text
    draft = {
        "destination_id": created.json()["id"],
        "type": "webhook",
        "webhook_header_name": "X-Secret",
    }
    url = f"/api/v1/projects/{slug}/alert-destinations/test"

    moved = await client.post(
        url, json={**draft, "target_url": "https://collector.example.org/hook"}
    )
    assert moved.json()["ok"] is False
    assert moved.json()["error_kind"] == "config"
    # The stored target URL is a secret too: the refusal does not name it.
    assert "hooks.example.com" not in str(moved.json()["error"])
    assert posted == []

    same_host = await client.post(
        url, json={**draft, "target_url": "https://hooks.example.com/other-path"}
    )
    assert same_host.json()["ok"] is True, same_host.text
    assert posted == [("https://hooks.example.com/other-path", {"X-Secret": "s3cret"})]

    audits = await _test_audits()
    origins = sorted(str(row.payload["target_origin"]) for row in audits)
    # Scheme and host only: the path and query of a webhook URL are secrets.
    assert origins == ["https://collector.example.org", "https://hooks.example.com"]


# --------------------------------------------------------------------------- #
# F7: signal series reads only buckets an open signal can carry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_signal_series_skips_buckets_no_open_signal_can_carry(
    client: AsyncClient,
) -> None:
    slug = "lane-signal-series-bounds"
    await _project(client, slug)
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type.status_code == 201, event_type.text
    event_type_id = uuid.UUID(event_type.json()["id"])
    data_source_id = await _data_source(client, "Warehouse lane series")
    scan_config_id = uuid.UUID(await _scan(client, slug, data_source_id, "Production scan"))
    recent = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - 3 * HOUR
    ancient = datetime(1970, 1, 1, tzinfo=UTC)
    async with TestSessionLocal() as session:
        for bucket in (ancient, recent):
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=None,
                    event_type_id=event_type_id,
                    bucket=bucket,
                    count=7,
                )
            )
        await session.commit()

    def scope(bucket: datetime) -> dict[str, str]:
        return {
            "scan_config_id": str(scan_config_id),
            "scope_type": "event_type",
            "scope_ref": str(event_type_id),
            "bucket": bucket.isoformat(),
        }

    response = await client.post(
        f"/api/v1/projects/{slug}/anomalies/signals/series",
        json={
            "scopes": [
                scope(ancient),
                scope(datetime(2100, 1, 1, tzinfo=UTC)),
                scope(recent),
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Only the recent scope is drawn; 1970 and 2100 are omitted, not widened into.
    assert len(body) == 1
    assert [point["count"] for point in body[0]["data"]] == [7]


# --------------------------------------------------------------------------- #
# F8/F9: fact series preview window
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("interval_code", ["15m", "1h", "6h", "1d", "1w"])
def test_fact_series_preview_window_is_aligned_and_capped(interval_code: str) -> None:
    from tripl.core.bucketing import floor_to_bucket
    from tripl.core.intervals import get_interval

    now = datetime(2026, 9, 26, 13, 47, 12, tzinfo=UTC)
    delta = get_interval(interval_code).delta
    time_from, time_to = fact_series_preview_window(interval_code, now=now)

    # Both edges on the collector's grid, and the still-open bucket left out.
    assert time_to == floor_to_bucket(now, interval_code)
    assert floor_to_bucket(time_from, interval_code) == time_from
    buckets = (time_to - time_from) // delta
    assert FACT_SERIES_PREVIEW_MIN_BUCKETS <= buckets <= PREVIEW_WINDOW_BUCKETS
    assert (
        time_to - time_from <= FACT_SERIES_PREVIEW_MAX_SPAN
        or buckets == FACT_SERIES_PREVIEW_MIN_BUCKETS
    )


def test_a_weekly_fact_preview_no_longer_covers_a_year() -> None:
    time_from, time_to = fact_series_preview_window("1w", now=datetime(2026, 9, 26, tzinfo=UTC))
    assert time_to - time_from == FACT_SERIES_PREVIEW_MIN_BUCKETS * timedelta(weeks=1)


# --------------------------------------------------------------------------- #
# F10: no next collection without a time column
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drilldown_has_no_next_collection_without_a_time_column(
    client: AsyncClient,
) -> None:
    slug = "lane-no-time-column"
    await _project(client, slug)
    data_source_id = await _data_source(client, "Warehouse lane timing")
    without = await _scan(client, slug, data_source_id, "No time column")
    with_column = await _scan(
        client, slug, data_source_id, "With time column", time_column="event_time"
    )

    for scan_config_id, expect_next in ((without, False), (with_column, True)):
        response = await client.get(
            f"/api/v1/projects/{slug}/metrics/total",
            params={"scan_config_id": scan_config_id},
        )
        assert response.status_code == 200, response.text
        assert (response.json()["next_collection_at"] is not None) is expect_next


# --------------------------------------------------------------------------- #
# F11: "Used by" capped per source in SQL
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_data_source_scan_refs_are_capped_per_source(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(datasource_service, "DATA_SOURCE_SCAN_REFS_LIMIT", 2)
    await _project(client, "lane-refs-a")
    await _project(client, "lane-refs-b")
    shared = await _data_source(client, "Shared lane warehouse")
    single = await _data_source(client, "Single lane warehouse")
    await _scan(client, "lane-refs-b", shared, "Zeta")
    await _scan(client, "lane-refs-a", shared, "Beta")
    await _scan(client, "lane-refs-a", shared, "Alpha")
    await _scan(client, "lane-refs-b", single, "Only")

    response = await client.get("/api/v1/data-sources")
    assert response.status_code == 200, response.text
    by_id = {source["id"]: source for source in response.json()}

    assert by_id[shared]["scan_count"] == 3
    # Ordered by project name, then scan name; the cap keeps the first two.
    assert [ref["name"] for ref in by_id[shared]["scans"]] == ["Alpha", "Beta"]
    # The cap is per source: another source's refs are not crowded out.
    assert [ref["name"] for ref in by_id[single]["scans"]] == ["Only"]


# --------------------------------------------------------------------------- #
# F12: shadow sample size
# --------------------------------------------------------------------------- #


def _wide_sample(columns: int, value: str) -> dict[str, str]:
    names = [f"col_{index:03d}" for index in range(columns)]
    return _shadow_sample(
        tuple(value for _ in names),
        reg_index={name: index for index, name in enumerate(names)},
        json_index={},
        n_reg=columns,
        json_value_names=[],
        event_type_column=None,
        time_column=None,
    )


def test_shadow_sample_caps_the_number_of_keys() -> None:
    sample = _wide_sample(_SHADOW_SAMPLE_KEYS_MAX * 5, "v")
    assert len(sample) == _SHADOW_SAMPLE_KEYS_MAX
    # The first columns in row order are the ones kept.
    assert next(iter(sample)) == "col_000"


def test_shadow_sample_caps_its_total_size() -> None:
    sample = _wide_sample(_SHADOW_SAMPLE_KEYS_MAX, "x" * 500)
    size = sum(len(name) + len(value) for name, value in sample.items())
    assert 0 < size <= _SHADOW_SAMPLE_CHARS_MAX
    assert len(sample) < _SHADOW_SAMPLE_KEYS_MAX
