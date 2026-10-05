import ast
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient

from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW, ShadowEventCandidate
from tripl.services import audit_service
from tripl.tests._audit_feed import org_audit
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal


def test_redact_masks_alerting_destination_secrets() -> None:
    """Alerting destination secrets carry key names that are not the generic
    password/token/secret set, so _redact must mask them explicitly."""
    payload = {
        "webhook_url": "https://hooks.slack.com/services/T/B/XXXX",
        "bot_token": "123456:ABCDEF-telegram-token",
        "target_url": "https://example.com/webhook?auth=zzz",
        "webhook_header_value": "Bearer header-secret",
        "jira_api_token": "jira-token-value",
        "linear_api_key": "lin_api_key_value",
        "pagerduty_routing_key": "R0UT1NGKEY0123456789abcdefABCDEF",
        "teams_webhook_url": "https://example.webhook.office.com/webhookb2/secret",
        "name": "Prod Slack",
    }

    redacted = audit_service._redact(payload)

    assert redacted["webhook_url"] == "***"
    assert redacted["bot_token"] == "***"
    assert redacted["target_url"] == "***"
    assert redacted["webhook_header_value"] == "***"
    assert redacted["jira_api_token"] == "***"
    assert redacted["linear_api_key"] == "***"
    assert redacted["pagerduty_routing_key"] == "***"
    assert redacted["teams_webhook_url"] == "***"
    # Benign fields pass through untouched.
    assert redacted["name"] == "Prod Slack"


async def _setup_project(client: AsyncClient, slug: str = "audit-proj") -> str:
    await client.post("/api/v1/projects", json={"name": "A", "slug": slug})
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "pv", "display_name": "PV"},
    )
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_audit_records_field_lifecycle(client: AsyncClient) -> None:
    et_id = await _setup_project(client, "audit-fields")

    create = await client.post(
        f"/api/v1/projects/audit-fields/event-types/{et_id}/fields",
        json={"name": "email", "display_name": "Email", "field_type": "string"},
    )
    assert create.status_code == 201
    field_id = create.json()["id"]

    patch = await client.patch(
        f"/api/v1/projects/audit-fields/event-types/{et_id}/fields/{field_id}",
        json={"sensitivity": "pii"},
    )
    assert patch.status_code == 200

    delete = await client.delete(
        f"/api/v1/projects/audit-fields/event-types/{et_id}/fields/{field_id}"
    )
    assert delete.status_code == 204

    audit = await client.get("/api/v1/projects/audit-fields/audit")
    assert audit.status_code == 200
    body = audit.json()
    actions = {entry["action"] for entry in body["items"]}
    # All three field actions plus the event-type create from setup.
    assert {"field.create", "field.update", "field.delete"} <= actions
    # User email is denormalized for retention after user deletion.
    assert all(entry["user_email"] == "test@example.com" for entry in body["items"])
    # A list row carries NO payload. The tab renders one only for the row a
    # reader expanded, so a page of blobs crossed the wire to be displayed
    # nowhere.
    update_entry = next(e for e in body["items"] if e["action"] == "field.update")
    assert "payload" not in update_entry

    # The detail route still reports exactly the fields the client changed.
    detail = await client.get(f"/api/v1/projects/audit-fields/audit/{update_entry['id']}")
    assert detail.status_code == 200
    assert detail.json()["payload"] == {"sensitivity": "pii"}


@pytest.mark.asyncio
async def test_audit_redacts_data_source_password(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Prod CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "default",
            "username": "default",
            "password": "super-secret",
        },
    )
    assert resp.status_code == 201

    # A data source belongs to no project: its row is in the organization-wide
    # log only, read here from the table.
    items = await org_audit("data_source.create", payload=True)
    assert len(items) == 1
    payload = items[0]["payload"]
    assert payload["password"] == "***"
    assert payload["host"] == "localhost"


@pytest.mark.asyncio
async def test_audit_filters_by_action_and_project(client: AsyncClient) -> None:
    await _setup_project(client, "audit-a")
    await _setup_project(client, "audit-b")

    by_action = await client.get("/api/v1/projects/audit-a/audit?action=event_type.create")
    assert by_action.status_code == 200
    assert by_action.json()["total"] == 1
    no_match = await client.get("/api/v1/projects/audit-a/audit?action=data_source.create")
    assert no_match.json()["total"] == 0

    by_project = await client.get("/api/v1/projects/audit-a/audit")
    assert by_project.status_code == 200
    slugs = {entry["project_slug"] for entry in by_project.json()["items"]}
    assert slugs == {"audit-a"}


@pytest.mark.asyncio
async def test_audit_filters_by_user_email_substring(client: AsyncClient) -> None:
    await _setup_project(client, "audit-email")

    # The conftest client is registered as test@example.com.
    hit = await client.get("/api/v1/projects/audit-email/audit?user_email=example.com")
    assert hit.status_code == 200
    assert hit.json()["total"] >= 1
    assert all("example.com" in entry["user_email"].lower() for entry in hit.json()["items"])

    miss = await client.get("/api/v1/projects/audit-email/audit?user_email=nobody")
    assert miss.status_code == 200
    assert miss.json()["total"] == 0


@pytest.mark.asyncio
async def test_audit_covers_meta_field_variable_revision(client: AsyncClient) -> None:
    """Wire-in sanity: meta_field / variable / plan_revision all emit audit
    entries with the right action namespace."""
    await _setup_project(client, "audit-wide")

    mf = await client.post(
        "/api/v1/projects/audit-wide/meta-fields",
        json={"name": "jira_key", "display_name": "Jira", "field_type": "string"},
    )
    assert mf.status_code == 201
    mf_id = mf.json()["id"]
    patch = await client.patch(
        f"/api/v1/projects/audit-wide/meta-fields/{mf_id}",
        json={"is_required": True},
    )
    assert patch.status_code == 200
    delete = await client.delete(f"/api/v1/projects/audit-wide/meta-fields/{mf_id}")
    assert delete.status_code == 204

    var = await client.post(
        "/api/v1/projects/audit-wide/variables",
        json={"name": "user_id", "variable_type": "string"},
    )
    assert var.status_code == 201

    rev = await client.post(
        "/api/v1/projects/audit-wide/revisions",
        json={"summary": "first snapshot"},
    )
    assert rev.status_code == 201

    audit = await client.get("/api/v1/projects/audit-wide/audit")
    actions = {entry["action"] for entry in audit.json()["items"]}
    assert {
        "meta_field.create",
        "meta_field.update",
        "meta_field.delete",
        "variable.create",
        "plan_revision.create",
    } <= actions


@pytest.mark.asyncio
async def test_audit_paging_is_total_when_a_batch_shares_one_timestamp() -> None:
    """One bulk action writes many rows under a single ``created_at``.

    ``created_at`` is ``server_default=now()`` — ``transaction_timestamp()`` on
    Postgres — so the inbox bulk route, which files one row per incident with
    ``commit=False`` and commits once, stamps up to 200 rows identically. Ordering
    on ``created_at`` alone leaves that tie group unordered, and each LIMIT/OFFSET
    page is its own top-N sort with a different bound: rows could come back on two
    pages and others on none. The ids here are fixed and ascending so descending-id
    order is the reverse of insertion order — the assertion below fails on
    insertion order, which is what an untied sort returns.
    """
    stamp = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    ids = [uuid.UUID(int=n) for n in range(1, 7)]

    async with TestSessionLocal() as session:
        for n, entry_id in enumerate(ids):
            session.add(
                AuditLog(
                    id=entry_id,
                    created_at=stamp,
                    user_email="bulk@example.com",
                    project_slug="audit-paging",
                    action="alert_inbox.mute",
                    target_type="alert_incident",
                    target_name=f"incident-{n}",
                    payload={},
                )
            )
        await session.commit()

        pages = [
            await audit_service.list_entries(
                session, project_slug="audit-paging", limit=2, offset=offset
            )
            for offset in (0, 2, 4)
        ]

    assert [page.total for page in pages] == [6, 6, 6]
    seen = [entry.id for page in pages for entry in page.items]
    # Every row reachable from exactly one page: nothing repeated, nothing lost.
    assert sorted(seen) == sorted(ids)
    assert seen == sorted(ids, reverse=True)


@pytest.mark.asyncio
async def test_audit_detail_is_owner_only(client: AsyncClient) -> None:
    """The payload moved to its own route, and the gate had to move with it.

    The feed is owner-only because entries carry the warehouse connection
    details a direct read blanks for non-owners, and the ``base_query`` SQL
    authoring a scan is owner-only to protect (test_rbac.py's
    ``test_audit_log_is_owner_only``). Those fields now leave the API through
    THIS route only, so an ungated get-one would reopen exactly that back door.
    """
    await _setup_project(client, "audit-gate")
    listed = await client.get("/api/v1/projects/audit-gate/audit")
    entry_id = listed.json()["items"][0]["id"]

    # The conftest client registered first and is therefore the owner; the next
    # registration defaults to editor.
    await client.post("/api/v1/auth/logout")
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "audit-editor@example.com",
            "password": "Password123!",
            "name": "Audit Editor",
        },
    )
    assert register.status_code == 201, register.text
    # A member of the project, so the refusal is the owner gate's, not a 404.
    await add_member_by_slug("audit-gate", "audit-editor@example.com", "editor")

    denied = await client.get(f"/api/v1/projects/audit-gate/audit/{entry_id}")
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_audit_detail_reports_an_unknown_id_as_missing(client: AsyncClient) -> None:
    await _setup_project(client, "audit-missing")
    resp = await client.get(f"/api/v1/projects/audit-missing/audit/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_audit_detail_of_another_project_is_missing(client: AsyncClient) -> None:
    """A project's history hands out its own entries only: an id from another
    project, or from no project, answers 404 like an unknown one."""
    await _setup_project(client, "audit-mine")
    await _setup_project(client, "audit-theirs")
    theirs = (await client.get("/api/v1/projects/audit-theirs/audit")).json()["items"][0]["id"]
    await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Unscoped",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "default",
            "username": "default",
            "password": "x",
        },
    )
    unscoped = (await org_audit("data_source.create"))[0]["id"]

    for entry_id in (theirs, unscoped):
        resp = await client.get(f"/api/v1/projects/audit-mine/audit/{entry_id}")
        assert resp.status_code == 404, entry_id
    own = await client.get(f"/api/v1/projects/audit-theirs/audit/{theirs}")
    assert own.status_code == 200


# --- branch context on audit rows ----------------------------


async def _create_branch(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": name})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _one_entry(client: AsyncClient, slug: str, action: str) -> dict:
    """The single audit row for ``action`` on ``slug``, as the list renders it."""
    listed = await client.get(f"/api/v1/projects/{slug}/audit?action={action}")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1, items
    return dict(items[0])


@pytest.mark.asyncio
async def test_audit_records_the_branch_a_write_was_scoped_to(client: AsyncClient) -> None:
    """PR #143 made ``?branch=`` an ordinary, documented way to write, but the
    audit row could not say which plan it wrote to: two contradictory edits to
    the same object on two branches produced two identical-looking rows."""
    await _setup_project(client, "audit-branch")
    branch_id = await _create_branch(client, "audit-branch", "redesign-checkout")

    created = await client.post(
        f"/api/v1/projects/audit-branch/meta-fields?branch={branch_id}",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )
    assert created.status_code == 201, created.text

    row = await _one_entry(client, "audit-branch", "meta_field.create")
    assert row["branch_id"] == branch_id
    assert row["branch_name"] == "redesign-checkout"

    # Both projections carry it: the issue asks for the list AND the owner-only
    # detail payload, and the detail response inherits the list's fields.
    detail = await client.get(f"/api/v1/projects/audit-branch/audit/{row['id']}")
    assert detail.status_code == 200
    assert detail.json()["branch_id"] == branch_id
    assert detail.json()["branch_name"] == "redesign-checkout"


@pytest.mark.asyncio
async def test_audit_on_main_records_no_branch(client: AsyncClient) -> None:
    """No ``?branch=`` means main, and main is spelled as the absence of a
    branch — no synthetic main id, so nothing has to be backfilled."""
    await _setup_project(client, "audit-main")

    created = await client.post(
        "/api/v1/projects/audit-main/meta-fields",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )
    assert created.status_code == 201, created.text

    row = await _one_entry(client, "audit-main", "meta_field.create")
    assert row["branch_id"] is None
    assert row["branch_name"] == ""


@pytest.mark.asyncio
async def test_an_explicit_main_branch_id_records_no_branch_either(client: AsyncClient) -> None:
    """``GET /branches`` hands an API caller main's own id, and every write route
    accepts it — nothing filters on ``kind``.

    Main is spelled as the absence of a branch everywhere else (the chip, the
    schema docstring, the CLI's "there is no literal for main"), so binding it
    here would spell it a second way: two identical writes to main, one with the
    parameter and one without, would render differently in the same compliance
    trail and the chip would read "main".
    """
    await _setup_project(client, "audit-main-id")
    listed = await client.get("/api/v1/projects/audit-main-id/branches")
    assert listed.status_code == 200, listed.text
    main_id = next(b["id"] for b in listed.json()["items"] if b["kind"] == "main")

    created = await client.post(
        f"/api/v1/projects/audit-main-id/meta-fields?branch={main_id}",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )
    # The write still targets main exactly as it does with no parameter at all.
    assert created.status_code == 201, created.text

    row = await _one_entry(client, "audit-main-id", "meta_field.create")
    assert row["branch_id"] is None
    assert row["branch_name"] == ""


@pytest.mark.asyncio
async def test_branch_context_does_not_leak_into_the_next_request(client: AsyncClient) -> None:
    """The branch is carried on a request-scoped contextvar, so unbinding it is
    the whole correctness argument.

    Under uvicorn each request cycle is its own task and so its own ``Context``,
    which would hide a missing reset entirely. The suite drives the app through
    ``httpx.ASGITransport`` (conftest.py), which awaits the app in the CALLER's
    task — so dropping the ``finally: reset(token)`` in ``bound_branch`` stamps
    the branch of the first write onto every later row this client writes,
    including rows on routes that have no branch dimension at all.
    """
    await _setup_project(client, "audit-leak")
    branch_id = await _create_branch(client, "audit-leak", "feature-leak")

    scoped = await client.post(
        f"/api/v1/projects/audit-leak/meta-fields?branch={branch_id}",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )
    assert scoped.status_code == 201, scoped.text
    assert (await _one_entry(client, "audit-leak", "meta_field.create"))["branch_id"] == branch_id

    # A route that declares no branch dependency at all, through the SAME client.
    revision = await client.post(
        "/api/v1/projects/audit-leak/revisions",
        json={"summary": "after the branch write"},
    )
    assert revision.status_code == 201, revision.text

    row = await _one_entry(client, "audit-leak", "plan_revision.create")
    assert row["branch_id"] is None
    assert row["branch_name"] == ""


@pytest.mark.asyncio
async def test_deleting_a_branch_keeps_the_branch_name_on_its_audit_rows(
    client: AsyncClient,
) -> None:
    """``branch_name`` is denormalized because the FK erases itself.

    ``plan_branch.delete`` hard-deletes the row and the FK is ``ON DELETE SET
    NULL``, so an id-only column would wipe the branch context from exactly the
    rows that recorded that branch's work — the same reason ``user_email`` and
    ``project_slug`` sit next to their ids.
    """
    await _setup_project(client, "audit-branch-gone")
    branch_id = await _create_branch(client, "audit-branch-gone", "short-lived")
    created = await client.post(
        f"/api/v1/projects/audit-branch-gone/meta-fields?branch={branch_id}",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )
    assert created.status_code == 201, created.text

    deleted = await client.delete(f"/api/v1/projects/audit-branch-gone/branches/{branch_id}")
    assert deleted.status_code == 204, deleted.text

    row = await _one_entry(client, "audit-branch-gone", "meta_field.create")
    assert row["branch_id"] is None
    assert row["branch_name"] == "short-lived"


@pytest.mark.asyncio
async def test_a_malformed_branch_still_answers_400_and_writes_nothing(
    client: AsyncClient,
) -> None:
    """Binding the branch turned the dependency into a generator; every raise
    has to stay AHEAD of its first yield or 400 becomes a 500 (or a 422)."""
    await _setup_project(client, "audit-bad-branch")

    resp = await client.post(
        "/api/v1/projects/audit-bad-branch/meta-fields?branch=not-a-uuid",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid branch id"
    listed = await client.get("/api/v1/projects/audit-bad-branch/audit?action=meta_field.create")
    assert listed.json()["items"] == []


# --- events in the audit log --------------------------------
#
# api/v1/events.py called ``audit_service.record`` zero times, so the central
# object of the product was the one object the compliance trail had no rows for.
# The per-event ``event_changes`` history is not a substitute and was not a
# defensible "deliberate alternative" either: it never records creation or
# deletion, covers 4 of the 10 mutable fields, and its FK is
# ``ondelete="CASCADE"`` — so it is destroyed by the very deletion an audit log
# most exists to record.


async def _create_event(
    client: AsyncClient, slug: str, event_type_id: str, name: str, **extra: object
) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": name, **extra},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _payload_of(client: AsyncClient, entry_id: str) -> dict:
    """The owner-only detail payload for one entry — list rows carry none."""
    # Read from the table: the project route serves it too, but a callers' slug
    # would only restate what the list call above it already scoped.
    rows = [row for row in await org_audit(payload=True) if row["id"] == entry_id]
    assert len(rows) == 1, entry_id
    return dict(rows[0]["payload"])


@pytest.mark.asyncio
async def test_audit_records_event_create_update_delete(client: AsyncClient) -> None:
    """The three single-object routes each file exactly one row, naming the
    event as it stood when the action happened."""
    et_id = await _setup_project(client, "audit-events")
    event_id = await _create_event(client, "audit-events", et_id, "checkout_started")

    patched = await client.patch(
        f"/api/v1/projects/audit-events/events/{event_id}",
        json={"name": "checkout_completed"},
    )
    assert patched.status_code == 200, patched.text

    deleted = await client.delete(f"/api/v1/projects/audit-events/events/{event_id}")
    assert deleted.status_code == 204, deleted.text

    create_row = await _one_entry(client, "audit-events", "event.create")
    assert create_row["target_type"] == "event"
    assert create_row["target_id"] == event_id
    assert create_row["target_name"] == "checkout_started"

    update_row = await _one_entry(client, "audit-events", "event.update")
    assert update_row["target_id"] == event_id
    # The name AFTER the edit: the row has to name the event that now exists,
    # not the one the request replaced.
    assert update_row["target_name"] == "checkout_completed"
    assert await _payload_of(client, update_row["id"]) == {
        "name": "checkout_completed",
        "field_values_replaced": False,
        "meta_values_replaced": False,
    }

    delete_row = await _one_entry(client, "audit-events", "event.delete")
    assert delete_row["target_id"] == event_id
    # The id no longer resolves to anything, so the name is the whole record.
    assert delete_row["target_name"] == "checkout_completed"


@pytest.mark.asyncio
async def test_audit_survives_the_event_it_records(client: AsyncClient) -> None:
    """THE regression test for this issue.

    ``event_changes.event_id`` is ``ForeignKey(..., ondelete="CASCADE")``, so
    deleting an event erases its change rows AND their read path — and the
    activity feed is a projection over live events, so it loses the event too.
    Before this fix, deleting an event left no trace anywhere in the product.
    """
    et_id = await _setup_project(client, "audit-event-gone")
    event_id = await _create_event(client, "audit-event-gone", et_id, "checkout_started")

    patched = await client.patch(
        f"/api/v1/projects/audit-event-gone/events/{event_id}",
        json={"status": "live"},
    )
    assert patched.status_code == 200, patched.text

    history = await client.get(f"/api/v1/projects/audit-event-gone/events/{event_id}/history")
    assert history.status_code == 200
    assert history.json() != [], "the edit should have produced per-event history"

    deleted = await client.delete(f"/api/v1/projects/audit-event-gone/events/{event_id}")
    assert deleted.status_code == 204, deleted.text

    # The per-event surface is gone with the event, which is exactly why it
    # cannot be the audit trail.
    gone = await client.get(f"/api/v1/projects/audit-event-gone/events/{event_id}/history")
    assert gone.status_code == 404

    # The audit row outlives it and still names what was deleted, by whom.
    row = await _one_entry(client, "audit-event-gone", "event.delete")
    assert row["target_name"] == "checkout_started"
    assert row["user_email"] == "test@example.com"


@pytest.mark.asyncio
async def test_audit_records_the_branch_an_event_write_was_scoped_to(client: AsyncClient) -> None:
    """Branch attribution is inherited, not re-implemented: every event route
    already declares ``BranchIdDep``, and ``record`` reads the contextvar that
    binds. This is the case an earlier test could not cover, because
    the row it needed did not exist."""
    await _setup_project(client, "audit-event-branch")
    branch_id = await _create_branch(client, "audit-event-branch", "redesign-checkout")

    # The branch's OWN event-type id: creating a branch deep-copies the plan and
    # FK-remaps the copies away from main (see test_plan_branches.py).
    branch_ets = await client.get(
        f"/api/v1/projects/audit-event-branch/event-types?branch={branch_id}"
    )
    assert branch_ets.status_code == 200, branch_ets.text
    branch_et_id = next(et["id"] for et in branch_ets.json() if et["name"] == "pv")

    created = await client.post(
        f"/api/v1/projects/audit-event-branch/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": "checkout_started"},
    )
    assert created.status_code == 201, created.text

    row = await _one_entry(client, "audit-event-branch", "event.create")
    assert row["branch_id"] == branch_id
    assert row["branch_name"] == "redesign-checkout"


@pytest.mark.asyncio
async def test_bulk_event_routes_write_exactly_one_audit_row_each(client: AsyncClient) -> None:
    """One row per REQUEST, not one per event.

    ``EventBulkDelete.event_ids`` and ``EventBulkUpdate.event_ids`` carry
    ``min_length=1`` and no upper bound, so a row per event would let one API
    call write an unbounded number of audit rows. ``_one_entry`` asserts exactly
    one match, which is the whole point of this test.
    """
    et_id = await _setup_project(client, "audit-event-bulk")
    created = await client.post(
        "/api/v1/projects/audit-event-bulk/events/bulk",
        json=[{"event_type_id": et_id, "name": name} for name in ("one", "two", "three")],
    )
    assert created.status_code == 201, created.text
    event_ids = [str(row["id"]) for row in created.json()]

    updated = await client.post(
        "/api/v1/projects/audit-event-bulk/events/bulk-update",
        json={"event_ids": event_ids, "reviewed": True},
    )
    assert updated.status_code == 204, updated.text

    deleted = await client.post(
        "/api/v1/projects/audit-event-bulk/events/bulk-delete",
        json={"event_ids": event_ids},
    )
    assert deleted.status_code == 204, deleted.text

    for action in ("event.bulk_create", "event.bulk_update", "event.bulk_delete"):
        row = await _one_entry(client, "audit-event-bulk", action)
        # No single target: the ids live in the payload, as variable.bulk_* does.
        assert row["target_id"] is None, action

    update_payload = await _payload_of(
        client, (await _one_entry(client, "audit-event-bulk", "event.bulk_update"))["id"]
    )
    assert update_payload["count"] == 3
    assert update_payload["reviewed"] is True

    delete_payload = await _payload_of(
        client, (await _one_entry(client, "audit-event-bulk", "event.bulk_delete"))["id"]
    )
    assert delete_payload["count"] == 3
    assert sorted(delete_payload["event_ids"]) == sorted(event_ids)
    # Names, not just ids — after this request the ids resolve to nothing.
    assert sorted(delete_payload["event_names"]) == ["one", "three", "two"]
    assert delete_payload["truncated"] is False

    still_there = await client.get(f"/api/v1/projects/audit-event-bulk/events/{event_ids[0]}")
    assert still_there.status_code == 404


@pytest.mark.asyncio
async def test_bulk_update_audit_counts_events_changed_not_ids_sent(client: AsyncClient) -> None:
    """``bulk_update_events`` updates ``set(data.event_ids)`` and validates the
    404 against that same set, so a request repeating an id is legal and changes
    fewer events than it listed. Auditing the raw request list filed
    ``count: 3`` for a 2-event change, which is the one thing
    ``bulk_event_audit_payload`` promises it never does. ``bulk-delete`` 404s
    this body, deliberately (event_service.bulk_delete_events), so bulk-update is
    the only route that can see a duplicate at all.
    """
    et_id = await _setup_project(client, "audit-bulk-dupe")
    first = await _create_event(client, "audit-bulk-dupe", et_id, "one")
    second = await _create_event(client, "audit-bulk-dupe", et_id, "two")

    updated = await client.post(
        "/api/v1/projects/audit-bulk-dupe/events/bulk-update",
        json={"event_ids": [first, first, second], "reviewed": True},
    )
    assert updated.status_code == 204, updated.text

    payload = await _payload_of(
        client, (await _one_entry(client, "audit-bulk-dupe", "event.bulk_update"))["id"]
    )
    assert payload["count"] == 2
    # Deduplicated in request order, so the sample matches the count it labels.
    assert payload["event_ids"] == [first, second]
    assert payload["reviewed"] is True


@pytest.mark.asyncio
async def test_archiving_dead_events_writes_an_audit_row(client: AsyncClient) -> None:
    """Reconciliation → Dead events → Archive is an editor retiring plan events,
    not a scan write.

    It calls ``event_service.bulk_update_events`` — byte for byte the call
    ``POST /events/bulk-update`` makes — to move events into a terminal
    lifecycle state, so it files the same ``event.bulk_update`` action. Before
    this, an editor could retire 40 events from that screen and the audit log
    the docs describe as covering every event edit held nothing for it; the only
    trace was per-event history, which is the surface the docs contrast with the
    log rather than a substitute for it.
    """
    et_id = await _setup_project(client, "audit-dead-archive")
    first = await _create_event(client, "audit-dead-archive", et_id, "one")
    second = await _create_event(client, "audit-dead-archive", et_id, "two")

    archived = await client.post(
        "/api/v1/projects/audit-dead-archive/reconciliation/dead-events/archive",
        json={"event_ids": [first, second]},
    )
    assert archived.status_code == 200, archived.text

    row = await _one_entry(client, "audit-dead-archive", "event.bulk_update")
    assert row["target_type"] == "event"
    # One row per request with the ids in the payload, exactly as the events
    # router's own bulk routes file it — the action must not have two shapes.
    assert row["target_id"] is None
    assert row["user_email"] == "test@example.com"

    payload = await _payload_of(client, row["id"])
    assert payload["status"] == "archived"
    assert payload["count"] == 2
    assert sorted(payload["event_ids"]) == sorted([first, second])
    assert payload["truncated"] is False


async def _seed_shadow_candidate(
    client: AsyncClient,
    slug: str,
    *,
    event_type_id: str | None = None,
    event_name: str = "screen | checkout",
    observed_count: int = 42,
) -> str:
    """A scan config and one still-unresolved candidate on it, written to the DB.

    The metrics collector is the only writer of ``shadow_event_candidates``, so
    there is no API to seed one through, and a candidate needs a scan config,
    which needs a data source.
    """
    project = await client.get(f"/api/v1/projects/{slug}")
    assert project.status_code == 200, project.text
    project_id = uuid.UUID(project.json()["id"])

    async with TestSessionLocal() as session:
        data_source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
        )
        session.add(data_source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            name="main scan",
            base_query="SELECT 1",
        )
        session.add(config)
        await session.flush()
        now = datetime.now(UTC)
        candidate = ShadowEventCandidate(
            project_id=project_id,
            scan_config_id=config.id,
            event_type_id=uuid.UUID(event_type_id) if event_type_id else None,
            event_name=event_name,
            observed_count=observed_count,
            first_seen_at=now - timedelta(days=2),
            last_seen_at=now,
            status=SHADOW_STATUS_NEW,
        )
        session.add(candidate)
        await session.commit()
        return str(candidate.id)


@pytest.mark.asyncio
async def test_accepting_a_shadow_event_writes_an_event_create_row(client: AsyncClient) -> None:
    """Reconciliation → Shadow events → Accept files ``event.create``, the same
    action ``POST /events`` files.

    This sat behind "events written by a scan are not recorded", which never
    covered it: the scan only PROPOSED an identity, and accepting it is a person
    authoring a plan row — the event that results is indistinguishable from a
    hand-written one. An action of its own would have been worse than none, not
    better: an owner filtering ``event.create`` for "which events did people
    create?" would get a subset that looks complete.
    """
    et_id = await _setup_project(client, "audit-shadow-accept")
    candidate_id = await _seed_shadow_candidate(
        client,
        "audit-shadow-accept",
        event_type_id=et_id,
        event_name="screen | checkout",
    )

    accepted = await client.post(
        f"/api/v1/projects/audit-shadow-accept/reconciliation/shadow-events/{candidate_id}/accept",
        json={"name": "Checkout Screen"},
    )
    assert accepted.status_code == 200, accepted.text

    row = await _one_entry(client, "audit-shadow-accept", "event.create")
    assert row["target_type"] == "event"
    assert row["target_id"] == accepted.json()["event_id"]
    assert row["target_name"] == "Checkout Screen"

    payload = await _payload_of(client, row["id"])
    # The shape POST /events files, so one action reads one way whichever door
    # the event came through...
    assert payload["name"] == "Checkout Screen"
    assert payload["status"] == "live"
    assert payload["field_value_count"] == 0
    # ...and one nested key says which door, naming the traffic the plan was
    # changed to match.
    assert payload["accepted_from"]["shadow_candidate_id"] == candidate_id
    assert payload["accepted_from"]["source_name"] == "screen | checkout"
    assert payload["accepted_from"]["observed_count"] == 42


@pytest.mark.asyncio
async def test_dismissing_a_shadow_event_is_recorded_against_the_candidate(
    client: AsyncClient,
) -> None:
    """Dismissal creates nothing, so it gets its own action and its own target.

    It is terminal through the API — accept and dismiss both require the
    candidate to still be new — so one click writes observed traffic off for
    everyone, permanently. The candidate row storing ``resolved_by`` is not a
    substitute for the audit row: it is CASCADE-deleted with its project and its
    scan config, so deleting the scan that found the traffic would erase every
    trace of who waved it away — the same way ``event_changes`` died with the
    event it described.
    """
    await _setup_project(client, "audit-shadow-dismiss")
    candidate_id = await _seed_shadow_candidate(
        client,
        "audit-shadow-dismiss",
        event_name="screen | ghost",
        observed_count=1234,
    )

    dismissed = await client.post(
        f"/api/v1/projects/audit-shadow-dismiss/reconciliation/shadow-events/{candidate_id}/dismiss"
    )
    assert dismissed.status_code == 200, dismissed.text

    row = await _one_entry(client, "audit-shadow-dismiss", "shadow_event.dismiss")
    assert row["target_type"] == "shadow_event_candidate"
    assert row["target_id"] == candidate_id
    assert row["target_name"] == "screen | ghost"

    payload = await _payload_of(client, row["id"])
    # How much traffic was written off, and over what span — the two facts that
    # keep the decision reviewable once the candidate row is gone.
    assert payload["observed_count"] == 1234
    assert payload["first_seen_at"] < payload["last_seen_at"]

    # Nothing was created, so the create action must stay empty: the two
    # resolutions differ in exactly the way the log says they do.
    listed = await client.get("/api/v1/projects/audit-shadow-dismiss/audit?action=event.create")
    assert listed.json()["items"] == []


@pytest.mark.asyncio
async def test_audit_payload_omits_event_field_values(client: AsyncClient) -> None:
    """``EventFieldValueIn.value`` permits 100 000 characters and the list has no
    upper bound, so the ``data.model_dump()`` every other router files would put
    megabytes into one ``audit_log.payload``. The counts say the values were
    written; the values themselves live on the event."""
    et_id = await _setup_project(client, "audit-event-payload")
    field = await client.post(
        f"/api/v1/projects/audit-event-payload/event-types/{et_id}/fields",
        json={"name": "blob", "display_name": "Blob", "field_type": "string"},
    )
    assert field.status_code == 201, field.text

    huge = "x" * 50_000
    await _create_event(
        client,
        "audit-event-payload",
        et_id,
        "checkout_started",
        field_values=[{"field_definition_id": field.json()["id"], "value": huge}],
    )

    payload = await _payload_of(
        client, (await _one_entry(client, "audit-event-payload", "event.create"))["id"]
    )
    assert payload["field_value_count"] == 1
    assert payload["meta_value_count"] == 0
    assert "field_values" not in payload
    assert huge not in json.dumps(payload)


@pytest.mark.asyncio
async def test_audit_truncates_a_target_name_longer_than_the_column(client: AsyncClient) -> None:
    """``audit_log.target_name`` is String(255) and ``Event.name`` is String(500).

    Events are the first audit target that can overflow the column. Untruncated,
    the audit INSERT fails on Postgres with "value too long for type character
    varying(255)" AFTER ``create_event`` already committed — a 500 response with
    the event created and no audit row. The suite runs on sqlite, which does not
    enforce the width, so this asserts the length the guard produces rather than
    waiting for the driver to complain.
    """
    et_id = await _setup_project(client, "audit-event-longname")
    long_name = "e" * 400  # valid input: Event.name and EventCreate.name allow 500

    await _create_event(client, "audit-event-longname", et_id, long_name)

    row = await _one_entry(client, "audit-event-longname", "event.create")
    assert len(row["target_name"]) == 255
    assert row["target_name"] == long_name[:255]


@pytest.mark.asyncio
async def test_audit_records_the_life_of_a_project(client: AsyncClient) -> None:
    """Everything the log tracks lives inside a project, and the project was the
    one object with no record of its own: an owner could destroy a workspace
    whole and the log held nothing about who did it.

    The delete row is the point. Afterwards the id resolves to nothing and every
    per-project surface is gone with it, so the row has to carry what it named.
    """
    created = await client.post(
        "/api/v1/projects", json={"name": "Checkout", "slug": "life-before"}
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    renamed = await client.patch(
        "/api/v1/projects/life-before", json={"name": "Checkout Funnel", "slug": "life-after"}
    )
    assert renamed.status_code == 200, renamed.text

    deleted = await client.delete("/api/v1/projects/life-after")
    assert deleted.status_code == 204, deleted.text

    # Read from the table: once deleted, the project has no page and no history
    # route of its own (the organization-wide log reads these rows).
    async def logged(slug: str, action: str) -> dict:
        rows = [row for row in await org_audit(action) if row["project_slug"] == slug]
        assert len(rows) == 1, rows
        return rows[0]

    # Filed under the slug the project had AT THE TIME, so the create row keeps
    # the old one — which is why the tab has to resolve a slug rather than match
    # the label.
    create_row = await logged("life-before", "project.create")
    assert create_row["target_type"] == "project"
    assert create_row["target_id"] == project_id
    assert create_row["target_name"] == "Checkout"

    update_row = await logged("life-after", "project.update")
    assert update_row["target_id"] == project_id
    # The name AFTER the edit: the row names the project that now exists.
    assert update_row["target_name"] == "Checkout Funnel"
    assert await _payload_of(client, update_row["id"]) == {
        "slug": "life-after",
        "name": "Checkout Funnel",
    }

    delete_row = await logged("life-after", "project.delete")
    assert delete_row["target_id"] == project_id
    assert delete_row["target_name"] == "Checkout Funnel"
    # No project id — it points at nothing now, and saying so is the honest
    # shape. The slug and name survive in the row itself.
    assert delete_row["project_id"] is None
    assert await _payload_of(client, delete_row["id"]) == {
        "slug": "life-after",
        "name": "Checkout Funnel",
    }


@pytest.mark.asyncio
async def test_audit_records_generating_and_resetting_a_demo(client: AsyncClient) -> None:
    """A demo is a project, and generating one is a person's decision — so it
    files the same action a hand-made project does.

    A reset files ``project.reset`` against the REPLACEMENT: the reset drops the
    old demo's rows by its id, so a row filed against the project
    being destroyed would go with them, and this one is what explains why the
    trail below it starts fresh.
    """
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    created = await _one_entry(client, slug, "project.create")
    assert created["target_type"] == "project"
    assert (await _payload_of(client, created["id"]))["is_demo"] is True

    reset = await client.post(f"/api/v1/projects/demo/{slug}/reset")
    assert reset.status_code == 200, reset.text

    after = (await client.get(f"/api/v1/projects/{slug}/audit?limit=200")).json()["items"]
    actions = {entry["action"] for entry in after}
    assert "project.reset" in actions
    # The generation row named the project the reset destroyed, so it went with
    # it. Left behind it would sit under the replacement's slug and date the
    # replacement's creation to before the reset that made it.
    assert "project.create" not in actions


@pytest.mark.asyncio
async def test_renaming_a_project_keeps_its_history_together(client: AsyncClient) -> None:
    """A row records the slug the project answered to at the time, so matching
    that label split a renamed project's trail in two: the tab showed what
    happened after the rename and nothing from before.

    Filtering by the project the slug RESOLVES TO puts them back together, which
    is what the row's ``project_id`` was carrying all along.
    """
    await _setup_project(client, "rename-before")
    renamed = await client.patch("/api/v1/projects/rename-before", json={"slug": "rename-after"})
    assert renamed.status_code == 200, renamed.text
    later = await client.post(
        "/api/v1/projects/rename-after/event-types",
        json={"name": "click", "display_name": "Click"},
    )
    assert later.status_code == 201, later.text

    listed = await client.get(
        "/api/v1/projects/rename-after/audit?action=event_type.create&limit=200"
    )
    assert listed.status_code == 200
    # Both: the one authored under the old slug and the one after the rename.
    assert len(listed.json()["items"]) == 2, listed.json()


@pytest.mark.asyncio
async def test_a_recreated_slug_does_not_inherit_the_previous_trail(client: AsyncClient) -> None:
    """Delete a project and make a new one at the same slug and the newcomer
    used to show its predecessor's history as its own — a slug is unique only
    among projects that currently exist."""
    await _setup_project(client, "recycled")
    assert (await client.delete("/api/v1/projects/recycled")).status_code == 204

    again = await client.post("/api/v1/projects", json={"name": "Second", "slug": "recycled"})
    assert again.status_code == 201, again.text

    listed = await client.get("/api/v1/projects/recycled/audit?limit=200")
    actions = [entry["action"] for entry in listed.json()["items"]]
    # Its own creation, and nothing the project before it did.
    assert actions == ["project.create"], actions


def test_the_scan_pipeline_cannot_write_audit_rows() -> None:
    """A scan must never fill the audit log, and the boundary is structural.

    ``audit_service.record`` is a coroutine over an ``AsyncSession`` that reads a
    request-scoped contextvar; the scan pipeline is the SYNC Celery worker
    constructing ``Event(...)`` rows directly, so a 10 000-event scan writes zero
    audit rows — not by policy but because that code cannot reach this function.
    Recording in the ROUTER is what keeps it that way. This test freezes the
    property cheaply, without standing up a sync-worker fixture: it reads
    imports, so a comment mentioning audit_service does not trip it.
    """
    package = Path(audit_service.__file__).parent.parent
    offenders: list[str] = []

    for root in (package / "worker", package / "core" / "analyzers"):
        # A renamed package would make rglob find nothing and the test pass while
        # guarding nothing at all.
        assert root.is_dir(), root
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    names = [(node.module or "")] + [alias.name for alias in node.names]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    continue
                if any("audit_service" in name for name in names):
                    offenders.append(str(path.relative_to(package)))

    assert offenders == []


# --- audit_log indexes: the model, the migrations, and the queries ---

# Every index ``audit_log`` is supposed to carry, and the exact column order that
# makes it useful. Each entry is earned by a live access path, so a row removed
# here should mean a query removed too:
#
#   project_created      ``audit_service.list_entries`` scoped to one project,
#                        whose predicate is the resolved ``project_id``
#                         — and, as a prefix, the
#                        ``ON DELETE SET NULL`` cascade a project delete runs;
#   project_slug_created the fallback for a slug no live project answers to, the
#                        only route back to a deleted project's rows;
#   created              the workspace-wide feed, which filters by nothing at all
#                        ;
#   branch               the ``SET NULL`` cascade a branch delete runs;
#   organization_created one organization's log (F20). Added with the column so
#                        the backfill and the index ship together; today it only
#                        serves the ``SET NULL`` an organization delete runs.
#
# All four read indexes trail ``created_at, id`` because every one of those
# queries ends in ``ORDER BY created_at DESC, id DESC`` with a LIMIT, and
# ``created_at`` alone is not a total order.
_AUDIT_LOG_INDEXES = {
    "ix_audit_log_project_created": ("project_id", "created_at", "id"),
    "ix_audit_log_project_slug_created": ("project_slug", "created_at", "id"),
    "ix_audit_log_created": ("created_at", "id"),
    "ix_audit_log_branch": ("branch_id",),
    "ix_audit_log_organization_created": ("organization_id", "created_at", "id"),
}


def _declared_audit_log_indexes() -> dict[str, tuple[str, ...]]:
    return {
        index.name: tuple(column.name for column in index.columns)
        for index in AuditLog.__table__.indexes
        if index.name is not None
    }


def test_audit_log_declares_exactly_the_indexes_its_access_paths_need() -> None:
    """The model's index set is the one the queries above justify — no more.

    An extra index is written on every insert into this append-only table.

    A MISSING one is the older failure this guards against just as tightly: when
    the project predicate moved from the slug to the id, the index
    bought for the slug stopped covering the query it was bought for and the
    audit tab quietly went back to a top-N sort per page. Pinning the exact
    column tuples means the next predicate change has to come here and say so.
    """
    assert _declared_audit_log_indexes() == _AUDIT_LOG_INDEXES


def test_audit_log_baseline_indexes_match_the_model() -> None:
    """A fresh PostgreSQL install must create the indexes the model declares."""
    baseline = (Path(__file__).resolve().parents[3] / "alembic" / "baseline_schema.sql").read_text()
    matches = re.findall(
        r"(?m)^CREATE (?:UNIQUE )?INDEX (\S+) ON public\.audit_log USING btree \(([^)]+)\);$",
        baseline,
    )
    baseline_indexes = {
        name: tuple(column.strip() for column in columns.split(",")) for name, columns in matches
    }
    assert baseline_indexes == _declared_audit_log_indexes()
