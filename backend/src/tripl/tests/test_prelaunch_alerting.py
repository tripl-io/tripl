"""Pre-launch polish: the inbox card is titled by the scope its numbers describe.

``scope_names`` used to be alphabetical, so a multi-scope incident was titled
with whichever scope sorted first ("Demo Project +3 more") while its badge,
counts and scope link came from a different item. The headline is now the
newest firing, loudest first, and the other names follow by the size of their
move, so the eight-name cap keeps the loud ones.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.services._alerting_deliveries import INBOX_SCOPE_NAME_LIMIT
from tripl.tests.test_alerting import _inbox_item, _seed_inbox_delivery, _seed_inbox_fixture


async def _new_project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201
    return uuid.UUID(resp.json()["id"])


@pytest.mark.asyncio
async def test_inbox_card_leads_with_the_scope_its_numbers_describe(client: AsyncClient) -> None:
    project_id = await _new_project(client, "inbox-headline")
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)

    # An older, bigger firing on a scope that sorts first alphabetically.
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=3),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=3),
                percent_delta=300.0,
                actual_count=40,
                expected_count=10,
                correlation_group_id=group_id,
                scope_name="aaa_old_spike",
            )
        ],
    )
    # The newest bucket holds three scopes; two tie on size.
    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=now - timedelta(hours=1),
        items=[
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=40.0,
                actual_count=14,
                expected_count=10,
                correlation_group_id=group_id,
                scope_name="bbb_small",
            ),
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=80.0,
                actual_count=18,
                expected_count=10,
                correlation_group_id=group_id,
                scope_name="ddd_loud",
                scope_ref="ref-ddd",
            ),
            _inbox_item(
                scope_type="event",
                bucket=now - timedelta(hours=1),
                percent_delta=80.0,
                actual_count=18,
                expected_count=10,
                correlation_group_id=group_id,
                scope_name="ccc_loud",
                scope_ref="ref-ccc",
            ),
        ],
    )

    resp = await client.get("/api/v1/projects/inbox-headline/alert-inbox")
    assert resp.status_code == 200
    group = resp.json()["items"][0]

    # The newest bucket's loudest item, the name closing the tie — and the
    # name, the link and the size all come from that one item.
    assert group["scope_names"][0] == "ccc_loud"
    assert group["scope_ref"] == "ref-ccc"
    assert group["actual_count"] == 18
    assert group["percent_delta"] == 80.0
    # The rest by the size of their move, not by name.
    assert group["scope_names"] == ["ccc_loud", "aaa_old_spike", "ddd_loud", "bbb_small"]
    assert group["max_abs_percent_delta"] == 300.0


@pytest.mark.asyncio
async def test_inbox_card_ranks_a_firing_with_no_baseline_first(client: AsyncClient) -> None:
    project_id = await _new_project(client, "inbox-headline-baseline")
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    bucket = now - timedelta(hours=1)

    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=bucket,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=bucket,
                percent_delta=500.0,
                actual_count=60,
                expected_count=10,
                correlation_group_id=group_id,
                scope_name="aaa_measured",
            ),
            # Stored 0.0 is a placeholder, not the smallest move.
            _inbox_item(
                scope_type="event",
                bucket=bucket,
                percent_delta=0.0,
                actual_count=30,
                expected_count=0,
                correlation_group_id=group_id,
                scope_name="zzz_new_event",
            ),
        ],
    )

    resp = await client.get("/api/v1/projects/inbox-headline-baseline/alert-inbox")
    assert resp.status_code == 200
    group = resp.json()["items"][0]

    assert group["scope_names"] == ["zzz_new_event", "aaa_measured"]
    assert group["expected_count"] == 0
    assert group["percent_delta"] is None
    assert group["max_abs_percent_delta"] == 500.0


@pytest.mark.asyncio
async def test_inbox_name_cap_drops_the_quietest_scopes(client: AsyncClient) -> None:
    project_id = await _new_project(client, "inbox-headline-cap")
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    bucket = now - timedelta(hours=1)
    count = INBOX_SCOPE_NAME_LIMIT + 1
    # "a_quietest" sorts first by name and moves least: alphabetical order kept
    # it and dropped a louder scope.
    names = ["a_quietest"] + [f"scope_{index}" for index in range(1, count)]

    await _seed_inbox_delivery(
        project_id,
        scan_config_id=scan_config_id,
        destination_id=destination_id,
        rule_id=rule_ids[0],
        created_at=bucket,
        items=[
            _inbox_item(
                scope_type="event",
                bucket=bucket,
                percent_delta=50.0 + 10 * index,
                actual_count=15 + index,
                expected_count=10,
                correlation_group_id=group_id,
                scope_name=name,
            )
            for index, name in enumerate(names)
        ],
    )

    resp = await client.get("/api/v1/projects/inbox-headline-cap/alert-inbox")
    assert resp.status_code == 200
    group = resp.json()["items"][0]

    assert len(group["scope_names"]) == INBOX_SCOPE_NAME_LIMIT
    assert group["scope_names"][0] == names[-1]
    assert "a_quietest" not in group["scope_names"]
