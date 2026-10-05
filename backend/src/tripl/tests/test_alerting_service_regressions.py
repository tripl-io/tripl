"""Alerting service regressions.

One section per fix, in the order the fixes landed. Everything here runs
on the shared in-memory SQLite of ``conftest`` through the app, unless a section
says otherwise.
"""

from __future__ import annotations

import ast
import inspect
import json
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

import tripl.alerting_validation as av
from tripl.alert_templates import NO_BASELINE_LABEL
from tripl.api.v1 import alerting as alerting_router
from tripl.main import app
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricKind, MetricStatus
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import PlanBranch
from tripl.models.project_anomaly_settings import (
    DEFAULT_SIGMA_THRESHOLD,
    ProjectAnomalySettings,
)
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.schemas.alerting import (
    _DESTINATION_NOT_NULLABLE_ON_UPDATE,
    _RULE_NOT_NULLABLE_ON_UPDATE,
    AlertDestinationCreate,
    AlertDestinationUpdate,
    AlertInboxActionRequest,
    AlertInboxBulkActionRequest,
    AlertRuleCreate,
    AlertRuleUpdate,
    MonitorMuteRequest,
    SimulatedRuleFiring,
    _validate_jira_base_url_format,
    _validate_webhook_target_url_format,
)
from tripl.schemas.data_source import DataSourceCreate, DataSourceUpdate
from tripl.schemas.event_photo import EventCommentActionRequest
from tripl.schemas.plan_branch import PlanBranchCreate
from tripl.schemas.relation import RelationCreate
from tripl.schemas.scan_config import ScanConfigCreate, ScanConfigUpdate, ScanDryRunRequest
from tripl.schemas.schema_drift import SchemaDriftActionRequest
from tripl.schemas.variable_value_drift import VariableValueDriftActionRequest
from tripl.services import alerting_service
from tripl.services.alerting_rendering import render_firing_item
from tripl.services.demo.builders.alerts import _delivery_item
from tripl.tests.conftest import TestSessionLocal

# ---------------------------------------------------------------------------
# the rule DELETE route ran its own slug-less lookup
# ---------------------------------------------------------------------------


async def _make_project(client: AsyncClient, slug: str, name: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": name, "slug": slug})
    assert resp.status_code == 201, resp.text


async def _make_destination(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": name,
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/batch4",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _make_rule(client: AsyncClient, slug: str, destination_id: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={"name": name},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.mark.asyncio
async def test_deleting_a_rule_gives_one_404_for_any_destination_outside_the_project(
    client: AsyncClient,
) -> None:
    """The route must not answer before the slug has been checked.

    Naming the rule for the audit entry used to be done with a SELECT in the
    router itself, keyed on ``(rule_id, destination_id)`` and nothing else. It
    ran first, so which 404 came back depended on whether the rule existed rather
    than on whether the caller's project owned the destination: a rule id that
    exists under a foreign destination fell through to the service and got
    "Alert destination not found", while one that does not exist there was
    short-circuited into "Alert rule not found". Same caller, same permissions,
    two different answers — i.e. an existence check on rows in a project the
    caller never named.

    This is the assertion that reddens if ``delete_rule`` stops returning the
    name and the router's own select comes back: the two details diverge again.
    """
    await _make_project(client, "batch4-mine", "Mine")
    await _make_project(client, "batch4-theirs", "Theirs")

    their_destination = await _make_destination(client, "batch4-theirs", "Their Slack")
    their_rule = await _make_rule(client, "batch4-theirs", their_destination, "Their Rule")

    # Both requests name MY project in the path and THEIR destination in the
    # URL; only the rule id differs, and only one of the two exists.
    existing = await client.delete(
        f"/api/v1/projects/batch4-mine/alert-destinations/{their_destination}/rules/{their_rule}"
    )
    absent = await client.delete(
        f"/api/v1/projects/batch4-mine/alert-destinations/{their_destination}/rules/{uuid.uuid4()}"
    )

    assert existing.status_code == 404, existing.text
    assert absent.status_code == 404, absent.text
    assert existing.json()["detail"] == absent.json()["detail"] == "Alert destination not found"

    # The rule is untouched, as it always was — the service re-resolved through
    # ``get_rule(project_id=...)`` even while the router was answering early.
    listed = await client.get("/api/v1/projects/batch4-theirs/alert-destinations")
    assert listed.status_code == 200, listed.text
    assert [rule["id"] for rule in listed.json()[0]["rules"]] == [their_rule]


@pytest.mark.asyncio
async def test_deleting_a_rule_still_names_it_and_still_404s_inside_the_project(
    client: AsyncClient,
) -> None:
    """The two behaviours the move had to preserve, not change.

    Neither assertion here discriminates the fix on its own — both held before
    it — and that is the point: the audit entry still names the row that is now
    gone, and a rule id missing from a destination the project DOES own still
    gets "Alert rule not found" rather than being flattened into the
    destination's 404.
    """
    await _make_project(client, "batch4-own", "Own")
    destination = await _make_destination(client, "batch4-own", "Own Slack")
    rule_id = await _make_rule(client, "batch4-own", destination, "Delete Me")

    missing = await client.delete(
        f"/api/v1/projects/batch4-own/alert-destinations/{destination}/rules/{uuid.uuid4()}"
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "Alert rule not found"

    deleted = await client.delete(
        f"/api/v1/projects/batch4-own/alert-destinations/{destination}/rules/{rule_id}"
    )
    assert deleted.status_code == 204, deleted.text

    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog.action, AuditLog.target_name).where(
                        AuditLog.target_type == "alert_rule"
                    )
                )
            )
            .tuples()
            .all()
        )
    named = {action: target_name for action, target_name in rows}
    assert named["alert_rule.delete"] == "Delete Me"


# ``session.commit`` is deliberately absent from this set. The bulk inbox action
# at the bottom of the router commits ONE transaction for a batch of audit
# records (see the ``commit=False`` comment there); that is a transaction
# boundary the route owns, not a query it builds.
_ROUTER_QUERY_METHODS = frozenset({"execute", "get", "scalar", "scalars", "stream"})
_QUERY_BUILDERS = frozenset({"select", "insert", "update", "delete"})


def _is_query_call(node: ast.Call) -> bool:
    """``select(...)``/``insert(...)``/... or a query method on ``session``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _QUERY_BUILDERS
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "session"
        and func.attr in _ROUTER_QUERY_METHODS
    )


def _query_sites(source_path: Path) -> list[str]:
    """Every place a module builds or runs a query of its own, as ``file:line``.

    Parsed rather than grepped: the router now EXPLAINS the select it used to
    run, at length, right where it used to be — a grep would keep finding the
    explanation and could only be silenced by deleting it.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    return [
        f"{source_path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_query_call(node)
    ]


def test_the_alerting_router_builds_no_query_of_its_own() -> None:
    """Thin router, checked rather than remembered.

    "Parse/validate, call a service, return a schema" (CONTRIBUTING.md) is the
    rule the delete-rule select broke, and the reason it broke it — needing one
    string off a row — is a need that will come back. The behavioural test above
    catches a regression on THAT route; this catches one on any of the routes in
    this file, including one added tomorrow, and it names the line.

    If a router ever genuinely needs to reach the session, give the service the
    method instead. Fails with the offending ``file:line``.
    """
    source = inspect.getsourcefile(alerting_router)
    assert source is not None
    assert _query_sites(Path(source)) == []


# ---------------------------------------------------------------------------
# a rule PATCH answered with the filters it had just replaced
# ---------------------------------------------------------------------------


def _filter_shape(filters: list[dict]) -> list[tuple]:
    """A filter list minus the ids, which are fresh rows on every replacement."""
    return [(item["field"], item["operator"], item["values"]) for item in filters]


@pytest.mark.asyncio
async def test_patching_a_rules_filters_answers_with_the_filters_it_stored(
    client: AsyncClient,
) -> None:
    """The 200 body must describe the row the PATCH wrote, not the row it replaced.

    ``replace_rule_filters`` used to bulk-DELETE the filter rows and INSERT the
    replacements keyed on ``rule_id``, leaving ``rule.filters`` — the collection
    the response is rendered from — holding the deleted ones. ``expire_on_commit``
    is off and a ``selectinload`` will not overwrite an already-loaded collection,
    so the re-read at the end of ``update_rule`` handed the response the stale
    list. Revert the assignment in ``replace_rule_filters`` and the first PATCH
    assertion below comes back ``[("direction", "in", ["up"])]`` — the filter the
    request had just removed — while the database says otherwise.

    The database half is asserted too, and separately: without it a "fix" that
    made the response agree with itself by not writing at all would pass.
    """
    await _make_project(client, "batch4-filters", "Filters")
    destination = await _make_destination(client, "batch4-filters", "Filters Slack")

    created = await client.post(
        f"/api/v1/projects/batch4-filters/alert-destinations/{destination}/rules",
        json={
            "name": "Spikes only",
            "filters": [{"field": "direction", "operator": "in", "values": ["up"]}],
        },
    )
    assert created.status_code == 201, created.text
    assert _filter_shape(created.json()["filters"]) == [("direction", "in", ["up"])]
    rule_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/projects/batch4-filters/alert-destinations/{destination}/rules/{rule_id}",
        json={
            "filters": [
                {"field": "direction", "operator": "ne", "values": ["up"]},
                {"field": "direction", "operator": "in", "values": ["down"]},
            ]
        },
    )
    assert patched.status_code == 200, patched.text
    assert _filter_shape(patched.json()["filters"]) == [
        ("direction", "ne", ["up"]),
        ("direction", "in", ["down"]),
    ]

    async with TestSessionLocal() as session:
        stored = (
            await session.execute(
                select(
                    AlertRuleFilter.field,
                    AlertRuleFilter.operator,
                    AlertRuleFilter.values,
                )
                .where(AlertRuleFilter.rule_id == uuid.UUID(rule_id))
                .order_by(AlertRuleFilter.position)
            )
        ).all()
        table_wide = (await session.scalars(select(AlertRuleFilter.id))).all()
    assert [tuple(row) for row in stored] == [
        ("direction", "ne", ["up"]),
        ("direction", "in", ["down"]),
    ]
    # The displaced row is now removed by the ``delete-orphan`` cascade instead of
    # by the bulk DELETE that used to do it, so check it really goes: this project
    # has one rule, and the whole table holds exactly what that rule declared.
    assert len(table_wide) == 2

    # A later reader agrees with the mutation body — the two used to disagree.
    listed = await client.get("/api/v1/projects/batch4-filters/alert-destinations")
    assert listed.status_code == 200, listed.text
    assert _filter_shape(listed.json()[0]["rules"][0]["filters"]) == [
        ("direction", "ne", ["up"]),
        ("direction", "in", ["down"]),
    ]

    # Emptying the list is the same defect's sharpest edge: pre-fix the response
    # listed filters the rule no longer had at all.
    cleared = await client.patch(
        f"/api/v1/projects/batch4-filters/alert-destinations/{destination}/rules/{rule_id}",
        json={"filters": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["filters"] == []
    async with TestSessionLocal() as session:
        remaining = (await session.scalars(select(AlertRuleFilter.id))).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_creating_a_rule_with_filters_survives_the_relationship_write(
    client: AsyncClient,
) -> None:
    """Pins the ``session.refresh(rule, ["filters"])`` line, not the assignment.

    Replacing a ``delete-orphan`` collection makes the ORM read the old one to
    find the orphans. On this path the rule was INSERTed one line earlier and its
    filters were never loaded, so that read is a lazy load — IO from async code,
    which SQLAlchemy refuses with ``MissingGreenlet``. Delete the refresh and this
    test does not fail an assertion, it raises out of the request: rule creation
    with filters would 500 for every caller.
    """
    await _make_project(client, "batch4-newrule", "New Rule")
    destination = await _make_destination(client, "batch4-newrule", "New Rule Slack")

    created = await client.post(
        f"/api/v1/projects/batch4-newrule/alert-destinations/{destination}/rules",
        json={
            "name": "Drops only",
            "filters": [
                {"field": "direction", "operator": "in", "values": ["down"]},
                {"field": "direction", "operator": "ne", "values": ["up"]},
            ],
        },
    )
    assert created.status_code == 201, created.text
    assert _filter_shape(created.json()["filters"]) == [
        ("direction", "in", ["down"]),
        ("direction", "ne", ["up"]),
    ]


# ---------------------------------------------------------------------------
# a PATCH validated its fields one at a time, not the merged row
# ---------------------------------------------------------------------------


def _messages(resp: Response) -> str:
    """Every message in a 422 body, whether it is FastAPI's list or a plain detail.

    The two halves of this fix answer through different machinery — the null
    rejection is a pydantic ``model_validator`` (a list of error objects), the
    direction and chat_id rejections are ``HTTPException`` (a bare string) — and
    a reader of these tests should not have to care which.
    """
    detail = resp.json()["detail"]
    if isinstance(detail, str):
        return detail
    return " | ".join(str(item.get("msg", item)) for item in detail)


async def _make_telegram_destination(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "telegram",
            "name": name,
            "enabled": True,
            "bot_token": "123456:ABCDEF-telegram-token",
            "chat_id": "-100123",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.mark.asyncio
async def test_a_rule_patch_refuses_to_null_a_column_that_cannot_be_null(
    client: AsyncClient,
) -> None:
    """``{"enabled": null}`` is a 422 naming the field, not a 500.

    ``update_rule`` ``setattr``s whatever ``exclude_unset`` hands it, and a
    Python-side ``default=`` applies on INSERT, not on UPDATE — so an explicit
    null on a NOT NULL column used to travel all the way to the commit and fail
    there. The only handler for an IntegrityError is the catch-all in
    ``main.py``, so the caller got ``Internal server error``.

    Revert ``AlertRuleUpdate.reject_null_for_required_fields`` and this test does
    not merely fail an assertion: the PATCH raises IntegrityError out of the
    request, because the shared ``client`` fixture builds its transport with
    ``raise_app_exceptions`` left at the default.
    """
    await _make_project(client, "batch4-nulls", "Nulls")
    destination = await _make_destination(client, "batch4-nulls", "Nulls Slack")
    rule_id = await _make_rule(client, "batch4-nulls", destination, "Keep Me")
    url = f"/api/v1/projects/batch4-nulls/alert-destinations/{destination}/rules/{rule_id}"

    one = await client.patch(url, json={"enabled": None})
    assert one.status_code == 422, one.text
    assert "enabled" in _messages(one)

    # Every offending field is named, not only the first one reached.
    several = await client.patch(url, json={"name": None, "cooldown_minutes": None})
    assert several.status_code == 422, several.text
    assert "cooldown_minutes, name" in _messages(several)

    # ``filters`` is not a column and never 500'd — it silently did nothing,
    # which is the same lie told quietly. Clearing filters is ``[]``.
    filters = await client.patch(url, json={"filters": None})
    assert filters.status_code == 422, filters.text
    assert "filters" in _messages(filters)

    # Nothing was written by any of the three: the refusal lands before the
    # service is called at all.
    listed = await client.get("/api/v1/projects/batch4-nulls/alert-destinations")
    assert listed.status_code == 200, listed.text
    rule = listed.json()[0]["rules"][0]
    assert (rule["name"], rule["enabled"], rule["cooldown_minutes"]) == ("Keep Me", True, 1440)


@pytest.mark.asyncio
async def test_a_rule_patch_still_accepts_the_nulls_that_clear_something(
    client: AsyncClient,
) -> None:
    """The other half of the contract: three nullable columns must keep taking null.

    This is the assertion that reddens if anyone "simplifies" the fix into
    "reject every explicit null". ``scan_config_id`` null is the documented way
    to widen a rule back to the whole project, and a null template is how a rule
    goes back to the built-in one — all three are real operations with no other
    spelling, and all three are pinned in ``test_alerting.py`` as well.
    """
    await _make_project(client, "batch4-clearable", "Clearable")
    destination = await _make_destination(client, "batch4-clearable", "Clearable Slack")
    rule_id = await _make_rule(client, "batch4-clearable", destination, "Clearable")
    url = f"/api/v1/projects/batch4-clearable/alert-destinations/{destination}/rules/{rule_id}"

    templated = await client.patch(
        url,
        json={"message_template": "*Matched:* ${matched_count}\n${items_text}"},
    )
    assert templated.status_code == 200, templated.text
    assert templated.json()["message_template"] is not None

    cleared = await client.patch(url, json={"message_template": None, "items_template": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["message_template"] is None
    assert cleared.json()["items_template"] is None

    widened = await client.patch(url, json={"scan_config_id": None})
    assert widened.status_code == 200, widened.text
    assert widened.json()["scan_config_id"] is None


@pytest.mark.asyncio
async def test_a_one_sided_direction_patch_cannot_leave_a_rule_unable_to_fire(
    client: AsyncClient,
) -> None:
    """Both directions off is refused even when the body only turns off one.

    ``AlertRuleBase.validate_direction`` sees the request, not the row: on a body
    of ``{"notify_on_spike": false}`` the other side reads as None rather than as
    the false already stored, so the guard passed and the rule was saved with
    both columns false — enabled, rendered as enabled, and rejected by every
    direction gate in ``alerting_matching``. That is the invariant
    ``website/docs/use/alerting.md`` states as "at least one must be on".

    Delete the merged check in ``update_rule`` and the second PATCH below goes
    200, and the final assertion — that the stored row can still fire — fails.
    """
    await _make_project(client, "batch4-direction", "Direction")
    destination = await _make_destination(client, "batch4-direction", "Direction Slack")
    rule_id = await _make_rule(client, "batch4-direction", destination, "Both Ways")
    url = f"/api/v1/projects/batch4-direction/alert-destinations/{destination}/rules/{rule_id}"

    # Turning off ONE direction stays legal — the fix must not be stricter than
    # the rule it enforces.
    narrowed = await client.patch(url, json={"notify_on_drop": False})
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["notify_on_spike"] is True
    assert narrowed.json()["notify_on_drop"] is False

    # Turning off the other one, in a second request, is what used to slip past.
    inert = await client.patch(url, json={"notify_on_spike": False})
    assert inert.status_code == 422, inert.text
    assert _messages(inert) == "At least one alert direction must be enabled"

    # Both-off inside ONE body was always caught, by the schema validator, and
    # still is — same sentence, so a client cannot tell the two apart.
    at_once = await client.patch(url, json={"notify_on_spike": False, "notify_on_drop": False})
    assert at_once.status_code == 422, at_once.text
    assert "At least one alert direction must be enabled" in _messages(at_once)

    async with TestSessionLocal() as session:
        stored = await session.get(AlertRule, uuid.UUID(rule_id))
        assert stored is not None
        directions = (stored.enabled, stored.notify_on_spike, stored.notify_on_drop)
    assert directions == (True, True, False)


@pytest.mark.asyncio
async def test_a_telegram_destination_patch_cannot_clear_the_chat_id(
    client: AsyncClient,
) -> None:
    """``{"chat_id": null}`` on Telegram is a 422, not a bare ValueError.

    The schema's field validator returns None untouched, so the null reached
    ``validate_telegram_chat_id`` in the service, whose "required" arm raises a
    plain ValueError — from inside a service, where the only thing that catches
    it is the catch-all handler. Remove the ``chat_id is None`` guard in
    ``update_destination`` and the first PATCH below raises ValueError out of the
    request instead of answering 422.
    """
    await _make_project(client, "batch4-telegram", "Telegram")
    destination = await _make_telegram_destination(client, "batch4-telegram", "Ops Bot")
    url = f"/api/v1/projects/batch4-telegram/alert-destinations/{destination}"

    cleared = await client.patch(url, json={"chat_id": None})
    assert cleared.status_code == 422, cleared.text
    assert "chat_id" in _messages(cleared)

    # Replacing it still works — the guard refuses the clear, not the write.
    replaced = await client.patch(url, json={"chat_id": "-100999"})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["chat_id"] == "-100999"


@pytest.mark.asyncio
async def test_a_destination_patch_refuses_its_two_not_null_columns_only(
    client: AsyncClient,
) -> None:
    """``name``/``enabled`` are NOT NULL; a cadence of null is how you go immediate.

    The destination set is two fields wide for a reason, and the second half of
    this test is what stops it growing: ``delivery_schedule_cron`` null means
    IMMEDIATE — the default every destination ships with — so a blanket null
    rejection here would make "stop digesting this channel" unexpressible.
    """
    await _make_project(client, "batch4-dest-nulls", "Dest Nulls")
    destination = await _make_destination(client, "batch4-dest-nulls", "Dest Slack")
    url = f"/api/v1/projects/batch4-dest-nulls/alert-destinations/{destination}"

    named = await client.patch(url, json={"name": None})
    assert named.status_code == 422, named.text
    assert "name" in _messages(named)

    switched = await client.patch(url, json={"enabled": None})
    assert switched.status_code == 422, switched.text
    assert "enabled" in _messages(switched)

    scheduled = await client.patch(url, json={"delivery_schedule_cron": "0 9 * * *"})
    assert scheduled.status_code == 200, scheduled.text
    immediate = await client.patch(url, json={"delivery_schedule_cron": None})
    assert immediate.status_code == 200, immediate.text
    assert immediate.json()["delivery_schedule_cron"] is None

    listed = await client.get("/api/v1/projects/batch4-dest-nulls/alert-destinations")
    assert listed.status_code == 200, listed.text
    assert (listed.json()[0]["name"], listed.json()[0]["enabled"]) == ("Dest Slack", True)


def _not_null_columns(model: type, fields: Iterable[str]) -> set[str]:
    """Which of ``fields`` name a NOT NULL column on ``model``'s table."""
    columns = model.__table__.columns  # type: ignore[attr-defined]
    return {name for name in fields if name in columns and not columns[name].nullable}


def test_the_null_rejection_sets_still_match_the_columns_behind_them() -> None:
    """The two frozensets are a copy of the schema; this is what keeps them one.

    Both sets are written out by hand in ``schemas/alerting.py`` on purpose — the
    422 they produce is a published contract and should not silently change shape
    because a column was added — but a hand-written copy of the database schema
    rots. Adding a NOT NULL column to ``alert_rules`` and a matching Optional
    field to ``AlertRuleUpdate`` without extending the set reddens this test,
    which is the only warning anyone gets before that field starts 500ing.

    ``filters`` is added on the right-hand side rather than derived: it is a
    relationship, not a column, and belongs to the set for the other reason the
    comment in ``schemas/alerting.py`` gives.
    """
    rule_columns = _not_null_columns(AlertRule, AlertRuleUpdate.model_fields)
    assert rule_columns | {"filters"} == _RULE_NOT_NULLABLE_ON_UPDATE
    destination_columns = _not_null_columns(AlertDestination, AlertDestinationUpdate.model_fields)
    assert destination_columns == _DESTINATION_NOT_NULLABLE_ON_UPDATE

    # Named individually as well, because the derivation above would happily
    # agree with itself if one of these columns were ever made NOT NULL: each is
    # a field whose null is a documented operation with no other spelling.
    clearable = {"scan_config_id", "message_template", "items_template"}
    assert clearable <= set(AlertRuleUpdate.model_fields)
    assert not (clearable & _RULE_NOT_NULLABLE_ON_UPDATE)
    assert "delivery_schedule_cron" in AlertDestinationUpdate.model_fields
    assert "delivery_schedule_cron" not in _DESTINATION_NOT_NULLABLE_ON_UPDATE


# ---------------------------------------------------------------------------
# create/update schemas left strings unbounded against VARCHAR
# ---------------------------------------------------------------------------

# Each row pairs a bounded schema field with the column that field is written
# to. The pairing is the point: SQLite ignores VARCHAR widths, so the failure
# these bounds remove — a Postgres StringDataRightTruncation out of the INSERT,
# surfacing as the catch-all 500 in ``main.py`` — cannot be reproduced on this
# suite at all. What CAN be pinned here is that pydantic refuses the value first,
# and that the number it refuses at is the column's own width rather than a
# plausible-looking constant. Both halves are asserted below.
#
# The two ``email_from_address`` rows arrive by a different road than the rest
# and belong to a separate change, not the max_length sweep: that field was never declared
# with a bound, it merely inherited one from ``validate_email_address``, which
# normalises through ``email_validator`` and refuses an address over 254 octets.
# Loosening the override to ``validate_sender_address`` — which returns the
# original string so a display name survives — took that accident away, so the
# bound is declared here like every other row's.
#
# The eight destination channel rows close that sweep late: the sweep
# bounded ``name`` on these two schemas and stopped, leaving four more fields on
# the very same schemas writing into bounded columns with nothing measuring
# them. ``linear_label_ids`` is the one whose contradiction was internal rather
# than hypothetical — ``_LINEAR_LABEL_LIMIT`` (20) entries of ``_LINEAR_ID_RE``'s
# 64 characters join to 1299 against a 1024-wide column — and the test after
# this one is about that pair alone.
_BOUNDED_AGAINST_ITS_COLUMN: tuple[tuple[Any, str, type[BaseModel], str], ...] = (
    (AlertRule, "name", AlertRuleCreate, "name"),
    (AlertRule, "name", AlertRuleUpdate, "name"),
    (AlertDestination, "name", AlertDestinationCreate, "name"),
    (AlertDestination, "name", AlertDestinationUpdate, "name"),
    (AlertDestination, "email_from_address", AlertDestinationCreate, "email_from_address"),
    (AlertDestination, "email_from_address", AlertDestinationUpdate, "email_from_address"),
    (AlertDestination, "chat_id", AlertDestinationCreate, "chat_id"),
    (AlertDestination, "chat_id", AlertDestinationUpdate, "chat_id"),
    (AlertDestination, "webhook_header_name", AlertDestinationCreate, "webhook_header_name"),
    (AlertDestination, "webhook_header_name", AlertDestinationUpdate, "webhook_header_name"),
    (AlertDestination, "jira_base_url", AlertDestinationCreate, "jira_base_url"),
    (AlertDestination, "jira_base_url", AlertDestinationUpdate, "jira_base_url"),
    (AlertDestination, "linear_label_ids", AlertDestinationCreate, "linear_label_ids"),
    (AlertDestination, "linear_label_ids", AlertDestinationUpdate, "linear_label_ids"),
    (AlertDestination, "pagerduty_severity", AlertDestinationCreate, "pagerduty_severity"),
    (AlertDestination, "pagerduty_severity", AlertDestinationUpdate, "pagerduty_severity"),
    (PlanBranch, "name", PlanBranchCreate, "name"),
    (EventTypeRelation, "relation_type", RelationCreate, "relation_type"),
    (DataSource, "username", DataSourceCreate, "username"),
    (DataSource, "username", DataSourceUpdate, "username"),
    (ScanConfig, "event_name_format", ScanConfigCreate, "event_name_format"),
    (ScanConfig, "event_name_format", ScanConfigUpdate, "event_name_format"),
    (ScanDryRunJob, "event_name_format", ScanDryRunRequest, "event_name_format"),
)


def _declared_bound(schema: type[BaseModel], field: str) -> int | None:
    """The ``max_length`` pydantic will enforce for ``field``, or None if unbounded."""
    return next(
        (
            meta.max_length
            for meta in schema.model_fields[field].metadata
            if getattr(meta, "max_length", None) is not None
        ),
        None,
    )


def _rejections_for(schema: type[BaseModel], field: str, value: str) -> list[str]:
    """The error types ``schema`` raises for ``field`` alone, given ``value``.

    The payload deliberately carries nothing but ``field``: a create schema will
    also complain about its own required fields, and that is not what is under
    test, so every error located anywhere else is discarded.
    """
    try:
        schema.model_validate({field: value})
    except ValidationError as exc:
        return [str(error["type"]) for error in exc.errors() if error["loc"] == (field,)]
    return []


def _linear_label_csv(length: int) -> str:
    """A Linear label list of exactly ``length`` characters, every entry legal.

    Distinct numbered ids padded to at most 64 characters and joined with single
    commas — the exact shape ``validate_linear_label_ids`` returns, so the value
    that validator would hand on is the one whose length is measured.
    """
    ids: list[str] = []
    used = 0
    while used < length:
        separator = 1 if ids else 0
        size = min(64, length - used - separator)
        ids.append(f"{len(ids):04d}".ljust(size, "x"))
        used += separator + size
    return ",".join(ids)


# A run of ``x`` is the default probe, because for most of these fields nothing
# but the length can refuse it. Three of them have a validator that would refuse
# that run for a different reason, which would leave the "still accepted at
# exactly the column width" half of the test below proving nothing: a Telegram
# chat id must be digits or ``@channel``, a Jira base url must parse as https
# (and on ``AlertDestinationUpdate`` that check is ``mode="before"``, so it runs
# ahead of the bound and would hide it), and a label list must be commas between
# ids of at most 64 characters. Each builder returns a value of exactly the
# length asked for that its own field validator accepts.
_PROBE_VALUE: dict[str, Callable[[int], str]] = {
    "chat_id": lambda length: "1" * length,
    "jira_base_url": lambda length: "https://jira.example.com/" + "x" * (length - 25),
    "linear_label_ids": _linear_label_csv,
}


def _probe(field: str, length: int) -> str:
    """``length`` characters that only ``field``'s own bound has a reason to refuse."""
    return _PROBE_VALUE.get(field, lambda size: "x" * size)(length)


@pytest.mark.parametrize(
    ("model", "column", "schema", "field"),
    _BOUNDED_AGAINST_ITS_COLUMN,
    ids=[f"{schema.__name__}.{field}" for _, _, schema, field in _BOUNDED_AGAINST_ITS_COLUMN],
)
def test_a_bounded_text_field_stops_exactly_where_its_column_does(
    model: Any, column: str, schema: type[BaseModel], field: str
) -> None:
    """Every field in the table refuses one character past its column's width.

    Every row but the ``email_from_address`` pair is a field that shipped with no
    bound at all — the rule and destination names, the branch name, the relation
    type, the data source username, the event name format, and the four
    destination channel fields — and each one turned a too-long string into an
    INSERT that Postgres refused: a generic 500 naming no field, for a body this
    layer had already accepted. Drop any ``max_length=`` added by that sweep
    and the row for it fails on the first assertion, before the rejection
    assertions are even reached. The ``email_from_address`` pair reddens
    identically for the inverse reason: its bound was never declared, only
    inherited from the strict address validator the From: override stopped using,
    so removing the ``max_length=255`` that now replaces that
    accident fails the same first assertion.

    The equality against ``type.length`` is what stops the bound drifting from
    the column: widen ``alert_rules.name`` to 512 and leave the schema at 255 and
    this reddens, which is the only place the two numbers are compared anywhere.
    """
    width = model.__table__.c[column].type.length
    assert width is not None, f"{model.__name__}.{column} is not a bounded VARCHAR"
    assert _declared_bound(schema, field) == width

    assert "string_too_long" in _rejections_for(schema, field, _probe(field, width + 1))
    # And not one character sooner: the bound mirrors the column, it does not
    # narrow it, so a value that would fit the column must still be accepted.
    assert "string_too_long" not in _rejections_for(schema, field, _probe(field, width))


def test_the_widest_label_list_its_own_validator_allows_is_refused_by_the_column() -> None:
    """Where the entry-count limit and the column disagree, the column wins, in 422.

    ``_LINEAR_LABEL_LIMIT`` is 20 and ``_LINEAR_ID_RE`` allows 64 characters per
    id, so the longest list ``validate_linear_label_ids`` documents as legal
    joins to 1299 characters against a 1024-wide column, and that validator
    returns it unchanged — nothing in ``alerting_validation`` knows how wide the
    column is, which is why the refusal has to come from the field. Without it
    the string reached the INSERT and came back as the catch-all 500 naming no
    field; SQLite ignores VARCHAR widths, so the 422 below is all this suite can
    stand in for.

    Drop ``max_length=1024`` from ``AlertDestinationCreate.linear_label_ids`` and
    the first ``pytest.raises`` finds nothing raised; drop it from
    ``AlertDestinationUpdate`` and the second does.

    The last two assertions are the other half: Linear issues UUIDs, twenty of
    them come to 739 characters, and the bound takes nothing away from a real
    label list. Lowering ``_LINEAR_LABEL_LIMIT`` far enough to make the two
    agree is the other way to reconcile them, and it reddens the first
    ``validate_linear_label_ids`` call here rather than passing silently.
    """

    def payload(label_ids: str) -> dict[str, str]:
        return {
            "type": "linear",
            "name": "Linear",
            "linear_api_key": "lin_api_0123456789",
            "linear_team_id": "team-abc",
            "linear_label_ids": label_ids,
        }

    widest = ",".join(f"{index:04d}".ljust(64, "x") for index in range(20))
    assert len(widest) == 1299
    assert av.validate_linear_label_ids(widest) == widest
    assert AlertDestination.__table__.c["linear_label_ids"].type.length == 1024

    with pytest.raises(ValidationError) as create_error:
        AlertDestinationCreate.model_validate(payload(widest))
    assert [(error["loc"], error["type"]) for error in create_error.value.errors()] == [
        (("linear_label_ids",), "string_too_long")
    ]

    with pytest.raises(ValidationError) as update_error:
        AlertDestinationUpdate.model_validate({"linear_label_ids": widest})
    assert [(error["loc"], error["type"]) for error in update_error.value.errors()] == [
        (("linear_label_ids",), "string_too_long")
    ]

    real_ids = ",".join(str(uuid.uuid4()) for _ in range(20))
    assert len(real_ids) == 739
    assert AlertDestinationCreate.model_validate(payload(real_ids)).linear_label_ids == real_ids


@pytest.mark.asyncio
async def test_a_rule_name_wider_than_its_column_is_refused_before_the_insert(
    client: AsyncClient,
) -> None:
    """256 characters is a 422 naming the field, not a 500 out of the INSERT.

    ``AlertRuleCreate.name`` was a bare ``str`` against ``alert_rules.name``
    (String(255)), and the only thing between it and the database was the
    catch-all exception handler: on Postgres the caller got ``Internal server
    error`` with nothing written and no indication which field was at fault.
    SQLite stores an overlong string happily, which is why the suite never saw
    it — so what is asserted here is the 422 that replaced it. Delete
    ``max_length=255`` from ``AlertRuleCreate.name`` and the second request goes
    201; delete it from ``AlertRuleBase.name`` and the PATCH goes 200.
    """
    await _make_project(client, "batch4-width", "Width")
    destination = await _make_destination(client, "batch4-width", "Width Slack")
    rules_url = f"/api/v1/projects/batch4-width/alert-destinations/{destination}/rules"

    at_the_limit = await client.post(rules_url, json={"name": "n" * 255})
    assert at_the_limit.status_code == 201, at_the_limit.text
    assert at_the_limit.json()["name"] == "n" * 255

    over_the_limit = await client.post(rules_url, json={"name": "n" * 256})
    assert over_the_limit.status_code == 422, over_the_limit.text
    assert "at most 255 characters" in _messages(over_the_limit)

    rule_url = f"{rules_url}/{at_the_limit.json()['id']}"
    renamed = await client.patch(rule_url, json={"name": "n" * 256})
    assert renamed.status_code == 422, renamed.text
    assert "at most 255 characters" in _messages(renamed)

    # The refused create wrote nothing and the refused rename left the row it
    # named alone: exactly one rule, still called what it was created with.
    async with TestSessionLocal() as session:
        stored = (
            (
                await session.execute(
                    select(AlertRule.name).where(AlertRule.destination_id == uuid.UUID(destination))
                )
            )
            .scalars()
            .all()
        )
    assert list(stored) == ["n" * 255]


@pytest.mark.asyncio
async def test_a_rule_name_is_stripped_and_a_blank_one_is_refused(
    client: AsyncClient,
) -> None:
    """A rule could be called ``""``; a destination on the same screen could not.

    Destination names have gone through ``normalize_required_text`` all along.
    Rule names went through nothing, so ``""`` and ``"   "`` were both accepted
    and ``create_rule`` copied them onto the row — a monitor with no name in
    every list that renders one, and no way to tell it apart from its neighbours.
    ``AlertRuleBase.normalize_name`` is what closes that asymmetry: remove it and
    the first two requests below go 201, and the stored name keeps its padding.
    """
    await _make_project(client, "batch4-blank", "Blank")
    destination = await _make_destination(client, "batch4-blank", "Blank Slack")
    rules_url = f"/api/v1/projects/batch4-blank/alert-destinations/{destination}/rules"

    empty = await client.post(rules_url, json={"name": ""})
    assert empty.status_code == 422, empty.text
    assert "Rule name is required" in _messages(empty)

    whitespace = await client.post(rules_url, json={"name": "   "})
    assert whitespace.status_code == 422, whitespace.text
    assert "Rule name is required" in _messages(whitespace)

    # The same sentence the destination gives, for the same input, one noun apart.
    padded = await client.post(rules_url, json={"name": "  Ops Spike  "})
    assert padded.status_code == 201, padded.text
    assert padded.json()["name"] == "Ops Spike"

    rule_url = f"{rules_url}/{padded.json()['id']}"
    renamed = await client.patch(rule_url, json={"name": "  Renamed  "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"

    # Read back from the row rather than the response: the strip has to happen
    # before the write, not on the way out.
    async with TestSessionLocal() as session:
        stored = (
            (
                await session.execute(
                    select(AlertRule.name).where(AlertRule.destination_id == uuid.UUID(destination))
                )
            )
            .scalars()
            .all()
        )
    assert list(stored) == ["Renamed"]


# ---------------------------------------------------------------------------
# a muted_until sent without an offset 500'd instead of muting
# ---------------------------------------------------------------------------


def test_a_mute_payload_without_an_offset_is_read_as_utc() -> None:
    """Two spellings of one instant parse to one aware value.

    Pure schema, no app: the coercion has to happen at parse time, because the
    thing it protects — ``mute_monitor``'s ``muted_until <= datetime.now(UTC)``
    — is the first code the value reaches. Delete
    ``MonitorMuteRequest.normalize_datetime`` and the first assertion fails on a
    ``tzinfo`` of ``None``.
    """
    floating = MonitorMuteRequest.model_validate({"muted_until": "2026-09-20T10:00:00"})
    assert floating.muted_until == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)

    # A non-UTC offset is CONVERTED, not merely tolerated. Landing both spellings
    # on one value is what lets the audit payload below be a readable instant
    # instead of whichever wall clock the client happened to keep.
    elsewhere = MonitorMuteRequest.model_validate({"muted_until": "2026-09-20T12:00:00+02:00"})
    assert elsewhere.muted_until == floating.muted_until


@pytest.mark.asyncio
async def test_muting_a_monitor_with_an_offset_less_instant_mutes_it(
    client: AsyncClient,
) -> None:
    """``{"muted_until": "2026-09-11T10:00:00"}`` is a mute, not a 500.

    The published schema says ``format: date-time`` and has never demanded an
    offset, so this body is contract-legal — but pydantic kept it naive and
    ``mute_monitor`` compared it against an aware ``now``. Reverting
    ``normalize_datetime`` does not merely fail an assertion here: the compare
    raises "can't compare offset-naive and offset-aware datetimes" out of the
    request, because the shared ``client`` fixture leaves ``raise_app_exceptions``
    at its default.
    """
    await _make_project(client, "batch4-mute", "Mute")
    destination = await _make_destination(client, "batch4-mute", "Mute Slack")
    rule_id = await _make_rule(client, "batch4-mute", destination, "Mute Me")
    url = f"/api/v1/projects/batch4-mute/monitors/{rule_id}/mute"

    # No offset and no trailing "Z": a floating wall time two hours out.
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    muted = await client.post(
        url,
        json={"muted_until": expires_at.replace(tzinfo=None).isoformat()},
    )
    assert muted.status_code == 200, muted.text
    assert muted.json()["muted"] is True

    # The audit row records the NORMALIZED instant, and this is the assertion
    # that isolates the schema half of the fix: the service carries its own
    # naive guard, so reverting only the validator would still mute — but the
    # route audits ``data.model_dump(mode="json")``, which would then write back
    # the zone-less wall time nobody can place.
    async with TestSessionLocal() as session:
        payload = (
            await session.execute(
                select(AuditLog.payload).where(AuditLog.action == "alert_rule.mute")
            )
        ).scalar_one()
    recorded = datetime.fromisoformat(payload["muted_until"])
    assert recorded.tzinfo is not None
    assert recorded == expires_at

    # A past instant is still the one refusal on this route, and it is still the
    # 422 the TypeError used to preempt — the branch, not just the status code.
    stale = datetime.now(UTC) - timedelta(hours=1)
    refused = await client.post(
        url,
        json={"muted_until": stale.replace(tzinfo=None).isoformat()},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "muted_until must be in the future"

    # And the refusal left the mute that was already in force alone.
    detail = await client.get(f"/api/v1/projects/batch4-mute/monitors/{rule_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["muted"] is True


@pytest.mark.asyncio
async def test_mute_monitor_reads_a_naive_argument_as_utc(client: AsyncClient) -> None:
    """The service does not lean on the router's schema for its own type safety.

    ``mute_monitor`` is a service function that happens to have one caller
    today; ``MonitorMuteRequest`` protects that caller and nothing else. Remove
    the ``tzinfo is None`` coercion inside ``_alerting_monitors.mute_monitor``
    and both halves below raise TypeError instead of muting and refusing.
    """
    await _make_project(client, "batch4-mute-svc", "Mute Service")
    destination = await _make_destination(client, "batch4-mute-svc", "Mute Svc Slack")
    rule_id = await _make_rule(client, "batch4-mute-svc", destination, "Mute Svc")

    future = (datetime.now(UTC) + timedelta(hours=3)).replace(tzinfo=None)
    async with TestSessionLocal() as session:
        detail = await alerting_service.mute_monitor(
            session, "batch4-mute-svc", uuid.UUID(rule_id), future
        )
    assert detail.muted is True

    # Read the row rather than the response: the point is that the instant which
    # passed the future check is the instant that was stored, read as UTC.
    # SQLite hands back a naive column whatever was bound, so the comparison is
    # made on the wall clock both sides agree is UTC.
    async with TestSessionLocal() as session:
        stored = (
            await session.execute(
                select(AlertRule.muted_until).where(AlertRule.id == uuid.UUID(rule_id))
            )
        ).scalar_one()
    assert stored is not None
    assert stored.replace(tzinfo=None) == future

    past = (datetime.now(UTC) - timedelta(hours=3)).replace(tzinfo=None)
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException) as rejected:
            await alerting_service.mute_monitor(
                session, "batch4-mute-svc", uuid.UUID(rule_id), past
            )
    assert rejected.value.status_code == 422
    assert rejected.value.detail == "muted_until must be in the future"


# ---------------------------------------------------------------------------
# a silence that had already ended was accepted as a silence
# ---------------------------------------------------------------------------

# The four request bodies that carry an instant meaning "stay quiet until then"
# AND refuse a lapsed one, with the field each spells it as and the minimum body
# that reaches the guard.
#
# ``MonitorMuteRequest`` is deliberately absent: it has refused a past instant
# since it shipped, and it is the reference the other four were brought into line
# with — its own coverage is the section above.
# ``EventCommentActionRequest`` is deliberately absent too, for the opposite
# reason: it still takes a lapsed instant on purpose, which is pinned on its own
# below and explained in ``schemas/time_guards.py``.
_SILENCE_BODIES: list[tuple[type[BaseModel], str, dict[str, Any]]] = [
    (AlertInboxActionRequest, "muted_until", {"action": "mute"}),
    (
        AlertInboxBulkActionRequest,
        "muted_until",
        {"action": "mute", "correlation_group_ids": [uuid.uuid4()]},
    ),
    (SchemaDriftActionRequest, "snoozed_until", {"action": "snooze"}),
    (VariableValueDriftActionRequest, "snoozed_until", {"action": "snooze"}),
]

_SILENCE_IDS = [model.__name__ for model, _, _ in _SILENCE_BODIES]


def _build(model: type[BaseModel], base: dict[str, Any], field: str, value: Any) -> BaseModel:
    return model(**base, **{field: value})


@pytest.mark.parametrize(("model", "field", "base"), _SILENCE_BODIES, ids=_SILENCE_IDS)
def test_a_silence_that_has_already_ended_is_refused(
    model: type[BaseModel], field: str, base: dict[str, Any]
) -> None:
    """Each of the four guards, one test case each.

    Delete any one of the four ``require_future_instant`` calls — in
    ``AlertInboxActionRequest.validate_action``,
    ``AlertInboxBulkActionRequest.validate_action``,
    ``SchemaDriftActionRequest.validate_action`` or
    ``VariableValueDriftActionRequest.validate_action`` — and that model's case
    goes red while the other three stay green.
    """
    lapsed = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(ValidationError) as refused:
        _build(model, base, field, lapsed)
    # The exact sentence ``mute_monitor`` has always answered with, asserted as a
    # literal rather than a substring: the whole defect was two surfaces
    # disagreeing about one rule, so two ways of wording the refusal would be the
    # same divergence one level down.
    assert [error["msg"] for error in refused.value.errors()] == [
        f"Value error, {field} must be in the future"
    ]


@pytest.mark.parametrize(("model", "field", "base"), _SILENCE_BODIES, ids=_SILENCE_IDS)
def test_an_offsetless_past_instant_is_a_refusal_and_never_a_typeerror(
    model: type[BaseModel], field: str, base: dict[str, Any]
) -> None:
    """The half of the fix that is easiest to delete without noticing.

    ``"2020-01-01T00:00:00"`` with no offset is a contract-legal body — the
    published schema says ``format: date-time`` and has never demanded one — and
    pydantic keeps it NAIVE. Remove the ``tzinfo is None`` line from
    ``require_future_instant`` and ``naive <= datetime.now(UTC)`` raises
    TypeError, which pydantic does NOT wrap into a ValidationError: it escapes
    the validator, reaches ``main.py``'s catch-all and becomes a 500. That is
    a real bug on the monitor route, and this fix would have manufactured
    four more copies of it.

    ``pytest.raises(ValidationError)`` is what pins it — a TypeError fails this
    test rather than satisfying it.
    """
    lapsed_without_offset = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    with pytest.raises(ValidationError) as refused:
        _build(model, base, field, lapsed_without_offset.isoformat())
    assert [error["msg"] for error in refused.value.errors()] == [
        f"Value error, {field} must be in the future"
    ]


def test_a_comment_thread_still_takes_a_snooze_whose_date_has_passed() -> None:
    """The silence body this sweep must NOT reach.

    A lapsed instant hides nothing here, which is the whole reason the four above
    refuse one. The action reply carries ``snoozed_until`` beside ``status``, and
    both readers that decide whether the thread still wants an answer use the
    pair rather than ``status`` alone: ``unanswered_clause`` puts the thread
    straight back among the open questions, and the client's
    ``isThreadUnanswered`` drops the "snoozed" badge. The operator is told the
    truth on the next render, so there is no 200-that-did-nothing to catch — and
    no second button on that screen refusing the same body, which was the actual
    defect there.

    Add ``require_future_instant`` to ``EventCommentActionRequest.validate_action``
    and both halves below go red — as does the wire-level
    ``test_event_comments.py::test_a_lapsed_snooze_counts_as_unanswered_again``,
    which posts ``2000-01-01T00:00:00Z`` to reach the lapsed-snooze state in one
    request rather than waiting a week for it.

    The offsetless half is the body a client that does not normalize actually
    sends. Nothing compares it against a clock on this path, so it stays naive
    and accepted — never the TypeError the section above exists to prevent.
    """
    lapsed = datetime(2000, 1, 1, tzinfo=UTC)
    accepted = EventCommentActionRequest(action="snooze", snoozed_until=lapsed)
    assert accepted.snoozed_until == lapsed

    offsetless = EventCommentActionRequest.model_validate(
        {"action": "snooze", "snoozed_until": "2000-01-01T00:00:00"}
    )
    assert offsetless.snoozed_until == datetime(2000, 1, 1)


@pytest.mark.parametrize(("model", "field", "base"), _SILENCE_BODIES, ids=_SILENCE_IDS)
def test_an_accepted_silence_is_stored_as_the_utc_instant_it_was_checked_against(
    model: type[BaseModel], field: str, base: dict[str, Any]
) -> None:
    """Two spellings of one moment must survive as one value, not two.

    The guard normalizes and ASSIGNS BACK, so what the service writes to the
    column — and what the inbox route audits through ``model_dump`` — is the UTC
    instant the future-check passed on rather than a floating wall time whose
    meaning depends on how the driver resolves a naive value into a
    ``timestamptz``. Drop the assignment and keep the check, and the offsetless
    case below comes back naive: ``tzinfo is not None`` fails.
    """
    instant = (datetime.now(UTC) + timedelta(hours=5)).replace(microsecond=0)
    without_offset = _build(model, base, field, instant.replace(tzinfo=None).isoformat())
    # The same moment written from a zone two hours east — the shape a client in
    # Europe sends when it does not normalize before posting.
    elsewhere = _build(
        model, base, field, instant.astimezone(timezone(timedelta(hours=2))).isoformat()
    )

    for parsed in (without_offset, elsewhere):
        stored = getattr(parsed, field)
        assert stored.tzinfo is not None
        assert stored == instant
    assert getattr(without_offset, field) == getattr(elsewhere, field)


def test_the_indefinite_mute_is_not_a_lapsed_one() -> None:
    """The null arm of the mute guard, which is the arm most likely to be tidied away.

    A null ``muted_until`` on an INCIDENT is "muted until I unmute"
    — the one silence that can never lapse — so it is the last body that should
    be refused for having lapsed. Drop the ``is not None`` from either inbox
    guard and ``require_future_instant`` is handed a None: the first two
    assertions raise instead of building.

    The third and fourth pin the other boundary: a ``muted_until`` sent with a
    non-mute action, or a ``snoozed_until`` on an accept, is refused as a
    mismatch rather than silently discarded, the rule
    ``EventCommentActionRequest`` already applied.
    """
    assert AlertInboxActionRequest(action="mute", muted_until=None).muted_until is None
    assert (
        AlertInboxBulkActionRequest(correlation_group_ids=[uuid.uuid4()], action="mute").muted_until
        is None
    )

    lapsed = datetime.now(UTC) - timedelta(days=30)
    with pytest.raises(ValidationError, match="only meaningful when action is mute"):
        AlertInboxActionRequest(action="acknowledge", muted_until=lapsed)
    with pytest.raises(ValidationError, match="only meaningful when action is snooze"):
        SchemaDriftActionRequest(action="accept", snoozed_until=lapsed)

    # And the guard that was already there still fires first: a snooze with no
    # instant at all is still refused for being absent, not for being stale.
    with pytest.raises(ValidationError) as missing:
        EventCommentActionRequest(action="snooze")
    assert [error["msg"] for error in missing.value.errors()] == [
        "Value error, snoozed_until is required when action is snooze"
    ]


@pytest.mark.asyncio
async def test_the_inbox_refuses_a_past_mute_before_the_route_can_look_the_incident_up(
    client: AsyncClient,
) -> None:
    """The wire half: the guard is on the model the route actually binds.

    Both requests below name a correlation group that does not exist, and
    ``_get_or_create_correlation_state`` answers 404 for one of those. So the
    status code says exactly which layer replied:

    * 422 — FastAPI refused the BODY and the handler never ran, which is the fix.
    * 404 — the body was accepted and the service got as far as looking the
      incident up, which is what a past instant used to do before it went on to
      store a mute that every reader lapsed on sight.

    Revert ``AlertInboxActionRequest``'s mute guard and the first assertion reads
    404, not 200 — the refusal simply moves down a layer, which is precisely the
    point. The future-instant request is the control that keeps this test honest:
    without it, a route that 422'd every body would pass.
    """
    await _make_project(client, "batch4-past-mute", "Past Mute")
    url = f"/api/v1/projects/batch4-past-mute/alert-inbox/{uuid.uuid4()}/actions"

    lapsed = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    refused = await client.post(url, json={"action": "mute", "muted_until": lapsed})
    assert refused.status_code == 422, refused.text
    assert any(
        "muted_until must be in the future" in entry["msg"] for entry in refused.json()["detail"]
    ), refused.text

    ahead = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    accepted_body = await client.post(url, json={"action": "mute", "muted_until": ahead})
    assert accepted_body.status_code == 404, accepted_body.text


@pytest.mark.asyncio
async def test_the_bulk_mute_is_refused_before_two_hundred_incidents_are_touched(
    client: AsyncClient,
) -> None:
    """Same proof on the route where one mistyped instant costs the most.

    A bulk mute is how a screenful is silenced in one click, so a body this route
    accepts and cannot honour reports up to 200 incidents as muted while leaving
    every one of them open. The 422 has to land before the service starts
    resolving ids, and the 404 on the identical body with a future instant is
    what shows that it did.
    """
    await _make_project(client, "batch4-bulk-mute", "Bulk Mute")
    url = "/api/v1/projects/batch4-bulk-mute/alert-inbox/bulk-actions"
    group_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    lapsed = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    refused = await client.post(
        url,
        json={"correlation_group_ids": group_ids, "action": "mute", "muted_until": lapsed},
    )
    assert refused.status_code == 422, refused.text
    assert any(
        "muted_until must be in the future" in entry["msg"] for entry in refused.json()["detail"]
    ), refused.text

    ahead = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    accepted_body = await client.post(
        url,
        json={"correlation_group_ids": group_ids, "action": "mute", "muted_until": ahead},
    )
    assert accepted_body.status_code != 422, accepted_body.text


@pytest.mark.parametrize(("model", "field", "base"), _SILENCE_BODIES, ids=_SILENCE_IDS)
def test_the_lapsed_silence_refusal_names_its_field_only_in_msg(
    model: type[BaseModel], field: str, base: dict[str, Any]
) -> None:
    """WHERE the field name is, which the tests above never check.

    All four guards run in ``@model_validator(mode="after")``, and a ``ValueError``
    raised there is located at the MODEL, not at a field: pydantic reports
    ``loc: ()``. The name reaches a reader only because ``require_future_instant``
    interpolates ``field_name`` into the message, which is the documented reason
    it takes that argument at all.

    ``test_a_silence_that_has_already_ended_is_refused`` asserts ``msg`` and stops
    there, so nothing pinned the other half. Move any one of the four
    ``require_future_instant`` calls to a ``@field_validator`` — the one change
    that would silently make ``schemas/time_guards.py``'s account wrong again, in
    the opposite direction — and that model's case reddens here with
    ``loc == (field,)`` while every ``msg`` assertion above stays green.
    """
    lapsed = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(ValidationError) as refused:
        _build(model, base, field, lapsed)

    errors = refused.value.errors()
    assert [error["loc"] for error in errors] == [()]
    # The name is in the sentence and only in the sentence.
    assert field in errors[0]["msg"]


@pytest.mark.asyncio
async def test_the_inbox_refusal_reaches_the_operator_through_msg_not_through_loc(
    client: AsyncClient,
) -> None:
    """The same shape one layer out, where a client actually reads it.

    FastAPI publishes the model validator's empty ``loc`` as ``["body"]``, so no
    segment names ``muted_until``. What keeps the refusal readable anyway is
    ``formatValidationDetail`` (``frontend/src/api/client.ts``): it joins ``loc``
    minus the ``body``/``query`` prefix and falls back to ``msg`` alone when that
    path comes out empty, which is reproduced below the way
    ``test_scan_dry_run.user_visible_422`` reproduces it.

    It also pins the half of the claim about ``mute_monitor``: this ``detail`` is
    a LIST of error objects, while the monitor route's ``HTTPException`` carries a
    plain string (``test_mute_monitor_reads_a_naive_argument_as_utc`` asserts that
    one). The two mute surfaces agree on the status code and on nothing else in
    the body, so code reading one shape cannot parse the other.

    Reverting the guard reddens this too, but that is already covered above. What
    this test uniquely catches is a guard moved to a ``@field_validator``, or a
    ``RequestValidationError`` handler added to ``main.py`` that reshapes ``loc``
    — either would leave every existing assertion green while changing the
    contract ``schemas/time_guards.py`` describes.
    """
    await _make_project(client, "batch4-mute-loc", "Mute Loc")
    url = f"/api/v1/projects/batch4-mute-loc/alert-inbox/{uuid.uuid4()}/actions"

    lapsed = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    refused = await client.post(url, json={"action": "mute", "muted_until": lapsed})
    assert refused.status_code == 422, refused.text

    detail = refused.json()["detail"]
    assert isinstance(detail, list), refused.text
    assert [entry["loc"] for entry in detail] == [["body"]], refused.text
    assert not any("muted_until" in [str(seg) for seg in entry["loc"]] for entry in detail)

    shown = []
    for entry in detail:
        path = ".".join(str(seg) for seg in entry["loc"] if seg not in ("body", "query"))
        shown.append(f"{path}: {entry['msg']}" if path else entry["msg"])
    assert "; ".join(shown) == "Value error, muted_until must be in the future"


# ---------------------------------------------------------------------------
# the destination URL guards resolved DNS on the event loop
# ---------------------------------------------------------------------------


def _record_resolver(monkeypatch: pytest.MonkeyPatch, address: str) -> list[tuple[str, int]]:
    """Stand in for ``socket.getaddrinfo`` and record WHICH THREAD asked.

    ``getaddrinfo`` is the blocking call at the bottom of ``reject_private_host``
    and the whole subject of that fix: it used to run inside a pydantic
    validator — i.e. during FastAPI's body parsing, which for an ``async def``
    route happens on the event loop — and now runs in a worker thread. The
    thread id is the only thing that can tell the fix from its absence, because
    the status code is identical either way.

    Patching it also makes these tests hermetic. ``conftest``'s ``deny_network``
    tripwire covers ``socket.socket`` but not name resolution, so the webhook
    tests in ``test_alerting.py`` really do resolve ``example.com``; nothing
    below depends on the network, or on what a public name resolves to today.
    """
    calls: list[tuple[str, int]] = []

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[Any]:
        calls.append((host, threading.get_ident()))
        return [(2, 1, 6, "", (address, 0))]

    monkeypatch.setattr(av.socket, "getaddrinfo", fake_getaddrinfo)
    return calls


# A public address, so the guard passes and the request reaches a 201/200.
_PUBLIC_IP = "93.184.216.34"
# The EC2/GCE metadata endpoint — the canonical SSRF target, and link-local, so
# ``_is_blocked_ip`` refuses it.
_METADATA_IP = "169.254.169.254"

_WEBHOOK_BODY = {
    "type": "webhook",
    "name": "Ops Webhook",
    "target_url": "https://hooks.example.com/abc",
}
_JIRA_BODY = {
    "type": "jira",
    "name": "Ops Tickets",
    "jira_base_url": "https://acme.atlassian.net/",
    "jira_auth_email": "ops@example.com",
    "jira_api_token": "token-1",
    "jira_project_key": "ENG",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slug", "body", "host"),
    [
        ("batch4-ssrf-webhook", _WEBHOOK_BODY, "hooks.example.com"),
        ("batch4-ssrf-jira", _JIRA_BODY, "acme.atlassian.net"),
    ],
)
async def test_creating_a_destination_resolves_its_host_off_the_event_loop(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    slug: str,
    body: dict[str, str],
    host: str,
) -> None:
    """The SSRF lookup must not run on the thread that is serving the request.

    ``AlertDestinationCreate.validate_channel_config`` used to call
    ``validate_webhook_target_url`` / ``validate_jira_base_url``, whose
    ``block_private_hosts=True`` ends in ``socket.getaddrinfo`` — a blocking
    resolver call honouring no timeout of ours. A pydantic validator runs inside
    FastAPI's body parsing, and for an ``async def`` route that is the event
    loop, so saving a destination whose host resolved slowly stalled the entire
    uvicorn worker and every unrelated request already in flight on it.

    Give ``schemas.alerting._validate_webhook_target_url_format`` (or its Jira
    twin) back its ``block_private_hosts=True`` and the recorded thread becomes
    the loop's own, reddening the last assertion. The 201 does not move — which
    is precisely why the thread id has to be the assertion.
    """
    loop_thread = threading.get_ident()
    calls = _record_resolver(monkeypatch, _PUBLIC_IP)
    await _make_project(client, slug, "SSRF")

    resp = await client.post(f"/api/v1/projects/{slug}/alert-destinations", json=body)

    assert resp.status_code == 201, resp.text
    # The guard still runs AT ALL. Without this, simply deleting the SSRF check
    # would satisfy every other assertion here.
    assert [asked for asked, _ in calls] == [host]
    assert all(thread != loop_thread for _, thread in calls)


@pytest.mark.asyncio
async def test_repointing_a_webhook_destination_resolves_the_new_host_off_the_loop(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PATCH path had its own copy of the defect and needs its own proof.

    ``AlertDestinationUpdate.validate_target_url`` is a *field* validator, so a
    repoint blocked the loop exactly as a create did. ``update_destination``
    now runs the guard itself, before it assigns anything.
    """
    loop_thread = threading.get_ident()
    calls = _record_resolver(monkeypatch, _PUBLIC_IP)
    await _make_project(client, "batch4-ssrf-patch", "SSRF Patch")
    created = await client.post(
        "/api/v1/projects/batch4-ssrf-patch/alert-destinations", json=_WEBHOOK_BODY
    )
    assert created.status_code == 201, created.text

    calls.clear()
    resp = await client.patch(
        f"/api/v1/projects/batch4-ssrf-patch/alert-destinations/{created.json()['id']}",
        json={"target_url": "https://relay.example.com/xyz"},
    )

    assert resp.status_code == 200, resp.text
    assert [asked for asked, _ in calls] == ["relay.example.com"]
    assert all(thread != loop_thread for _, thread in calls)


@pytest.mark.asyncio
async def test_a_destination_resolving_to_the_metadata_endpoint_is_still_refused(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relocating the guard must not soften it.

    A hostname that resolves to 169.254.169.254 is the attack the
    guard exists for: a literal IP is refused by inspection, a NAME has to be
    resolved to be caught. The refusal now arrives as ``HTTPException(422)``
    from the service rather than a pydantic field error, so the body is a
    string instead of a list — both shapes are handled by the frontend's API
    client (``isFieldErrorArray`` falls through to the string branch), and the
    sentence an operator reads is unchanged.

    Delete the ``_assert_public_destination_host`` call from
    ``create_destination`` and this is a 201 with the row stored.
    """
    _record_resolver(monkeypatch, _METADATA_IP)
    await _make_project(client, "batch4-ssrf-evil", "SSRF Evil")

    resp = await client.post(
        "/api/v1/projects/batch4-ssrf-evil/alert-destinations",
        json={**_WEBHOOK_BODY, "target_url": "https://rebind.example.com/hook"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "Webhook target_url must not point to a private or internal address"
    )
    # Refused BEFORE the write, not merely reported: nothing was stored.
    async with TestSessionLocal() as session:
        stored = (await session.execute(select(AlertDestination))).scalars().all()
        assert stored == []


@pytest.mark.asyncio
async def test_a_malformed_target_url_is_still_a_parse_time_refusal(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that deliberately STAYED in the schema.

    Only the resolver moved. A URL's shape is a property of the string, so it is
    still settled during body parsing: the response is still FastAPI's
    structured ``detail`` list, and no lookup is attempted for a value that
    cannot be a destination whatever it resolves to. That ordering is what keeps
    ``test_alerting.py``'s ``"target_url" in resp.text`` assertions green and
    keeps a typo from costing a DNS round trip.
    """
    calls = _record_resolver(monkeypatch, _PUBLIC_IP)
    await _make_project(client, "batch4-ssrf-shape", "SSRF Shape")

    resp = await client.post(
        "/api/v1/projects/batch4-ssrf-shape/alert-destinations",
        json={**_WEBHOOK_BODY, "target_url": "http://hooks.example.com/abc"},
    )

    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list), resp.text
    assert "Webhook target_url must be a valid https URL" in resp.text
    assert calls == []


def test_the_format_only_guards_refuse_in_the_same_words_as_the_resolving_ones() -> None:
    """One rule, two call sites, one sentence — pinned so they cannot drift.

    ``_validate_webhook_target_url_format`` restates the arguments that
    ``alerting_validation.validate_webhook_target_url`` passes to the shared
    ``_validate_https_url``, minus ``block_private_hosts``. That duplication is
    the price of keeping the resolver out of the schema, and this is the guard
    on it: change the field label on either side — "Webhook target_url" to
    "Webhook URL", say — and the two halves start describing one rule in two
    voices on one screen, and this goes red.

    Every input below is refused before ``_validate_https_url`` reaches the
    ``block_private_hosts`` branch, so the resolving versions need no network.
    """

    def refusal(validator: Any, value: str | None) -> str:
        with pytest.raises(ValueError) as excinfo:
            validator(value)
        return str(excinfo.value)

    for bad in (None, "", "   ", "http://example.com/hook", "https://", "https://a b"):
        assert refusal(_validate_webhook_target_url_format, bad) == refusal(
            av.validate_webhook_target_url, bad
        )
        assert refusal(_validate_jira_base_url_format, bad) == refusal(
            av.validate_jira_base_url, bad
        )

    # The normalization the schema owes the service also survived the split: the
    # service stores what this returns, and a Jira base URL is stored unslashed.
    assert _validate_jira_base_url_format("https://acme.atlassian.net/") == (
        "https://acme.atlassian.net"
    )


@pytest.mark.asyncio
async def test_saving_a_tracker_base_url_resolves_it_off_the_event_loop(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third caller, which has no pydantic layer at all.

    ``ProjectTrackerConfigUpdate`` declares no validators, so ``base_url`` was
    validated inline through ``_JIRA_FIELD_VALIDATORS`` inside the ``async``
    ``update_project_tracker_config`` — the same blocking lookup, on the same
    event loop, one router over. It is now the one entry pulled out of that map
    and run through ``_validate_async``.

    The two assertions guard different halves and each has its own revert. Give
    any validator still in the map a resolving step and the first reddens: that
    map is now, and must stay, pure string work, which is precisely what makes
    the loop applying it safe to run inline. Make ``_validate_async`` call its
    validator directly instead of through ``asyncio.to_thread`` and the second
    reddens.

    What reddens NEITHER is putting ``base_url`` back into the map: the explicit
    branch above it wins, so the branch is the load-bearing half of this fix and
    the map entry's absence is only tidiness. Say it here because the obvious
    reading of the diff is the opposite one. The normalized value is asserted as
    well, since moving a field out of the validator map is exactly how its
    trailing-slash stripping would get dropped in silence.
    """
    loop_thread = threading.get_ident()
    calls = _record_resolver(monkeypatch, _PUBLIC_IP)
    await _make_project(client, "batch4-tracker-ssrf", "Tracker SSRF")

    untouched = await client.patch(
        "/api/v1/projects/batch4-tracker-ssrf/tracker-config",
        json={"auth_email": "ops@example.com", "project_key": "eng", "issue_type": "Bug"},
    )
    assert untouched.status_code == 200, untouched.text
    assert calls == [], "no validator left in _JIRA_FIELD_VALIDATORS may touch the network"

    resp = await client.patch(
        "/api/v1/projects/batch4-tracker-ssrf/tracker-config",
        json={"base_url": "https://acme.atlassian.net/"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["base_url"] == "https://acme.atlassian.net"
    assert [asked for asked, _ in calls] == ["acme.atlassian.net"]
    assert all(thread != loop_thread for _, thread in calls)


# ---------------------------------------------------------------------------
# the replay shipped the no-baseline placeholder as a real 0.0
# ---------------------------------------------------------------------------


async def _seed_two_metric_anomalies(project_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """One catalog metric with NO baseline and one with a real one.

    Both in the same project so ONE replay answers for both: the fix has to null
    the undefined ratio without touching the defined one, and a fixture carrying
    only the zero-baseline row could not tell those apart.

    Catalog-metric scope is the cheapest anomaly to seed — it is project-global,
    so it carries a NULL ``scan_config_id`` and reaches the replay through the
    project's metric definitions rather than through any scan join. Copied from
    ``test_metric_scope_purge.test_simulator_reports_the_percent_delta_live_dispatch_would_store``,
    which seeds the signed-baseline case the same way.
    """
    now = datetime.now(UTC)
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"ds-272-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                data_source_id=data_source.id,
                project_id=project_id,
                name="sc-272",
                base_query="SELECT 1",
                cardinality_threshold=100,
                interval="1h",
            )
        )
        metric_ids: list[uuid.UUID] = []
        for name, display_name, expected_count in (
            ("resumed_signups", "Resumed signups", 0.0),
            ("steady_signups", "Steady signups", 10.0),
        ):
            metric = MetricDefinition(
                id=uuid.uuid4(),
                project_id=project_id,
                name=name,
                display_name=display_name,
                kind=MetricKind.sql.value,
                aggregation=None,
                composition=None,
                config={},
                data_source_id=data_source.id,
                interval="1h",
                status=MetricStatus.active.value,
                anomaly_detection_enabled=True,
            )
            session.add(metric)
            await session.flush()
            metric_ids.append(metric.id)
            session.add(
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=None,
                    scope_type="metric",
                    scope_ref=str(metric.id),
                    event_id=None,
                    event_type_id=None,
                    bucket=now - timedelta(days=1),
                    actual_count=40.0,
                    expected_count=expected_count,
                    stddev=1.0,
                    z_score=6.0,
                    direction="spike",
                )
            )
    return metric_ids[0], metric_ids[1]


@pytest.mark.asyncio
async def test_a_replay_firing_with_no_baseline_reports_null_not_the_stored_0_0(
    client: AsyncClient,
) -> None:
    """The replay was the last surface emitting the placeholder as a number.

    ``percent_delta_of`` answers 0.0 when ``expected_count`` is 0 because the
    ratio is undefined, and every other outbound encoding names that instead of
    printing it — the delivery's ``items[]``, the inbox card,
    ``payload_snapshot`` and the webhook body all run it through
    ``percent_delta_or_none``. So one incident answered "how big was this move"
    with ``null`` on the delivery it produced and with ``0.0`` on the simulate
    response that PREDICTED it, and an agent testing ``percent_delta >
    threshold`` read "no change" for a scope firing from nothing.

    Four assertions, three different reverts:

    * ``percent_delta is None`` on the zero-baseline firing is the fix itself.
      Delete ``SimulatedRuleFiring.encode_percent_delta`` and it is 0.0 again.
    * ``percent_delta == 300.0`` on the baselined firing is the other half.
      Make the serializer unconditional — return ``None`` without asking
      ``has_baseline`` — and this reddens while the first still passes.
    * ``rendered_item`` catches the SHAPE the filed issue asked for. Retype the
      field ``float | None`` and null it in a ``model_validator`` the way the
      two sibling responses do, and ``alerting_rendering.render_firing_item``
      — which runs inside ``simulate_rule``, before this response is built —
      raises ``TypeError: unsupported format string passed to NoneType`` on
      ``f"{firing.percent_delta:.1f}"``. The status assertion above goes red
      first, on a 500, for precisely the class this fix is about.
    * ``absolute_delta`` is asserted because it is the number that DOES mean
      something for this class, and the null is only safe while it is there.
    """
    slug = "batch4-272-replay"
    await _make_project(client, slug, "Batch4 272 Replay")
    lookup = await client.get(f"/api/v1/projects/{slug}")
    assert lookup.status_code == 200, lookup.text
    project_id = uuid.UUID(lookup.json()["id"])

    no_baseline_metric, baselined_metric = await _seed_two_metric_anomalies(project_id)
    destination_id = await _make_destination(client, slug, "Sim Slack 272")
    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "Metrics only",
            "enabled": True,
            "include_project_total": False,
            "include_event_types": False,
            "include_events": False,
            "include_metrics": True,
            "notify_on_spike": True,
            "notify_on_drop": True,
            # 100 is the measured default and the reason the no-baseline class
            # matters: ``rule_matches_anomaly`` deliberately admits a zero
            # baseline past the percent gate rather than dividing by it, so the
            # loudest firings arrive here WITH a percent threshold in force.
            "min_percent_delta": 100,
            "min_absolute_delta": 0,
            "min_expected_count": 0,
        },
    )
    assert rule_resp.status_code == 201, rule_resp.text
    rule_id = rule_resp.json()["id"]

    replay = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate"
        "?days=7"
    )
    assert replay.status_code == 200, replay.text
    firings = {firing["scope_ref"]: firing for firing in replay.json()["firings"]}
    assert set(firings) == {str(no_baseline_metric), str(baselined_metric)}

    undefined = firings[str(no_baseline_metric)]
    assert undefined["expected_count"] == pytest.approx(0.0)
    assert undefined["percent_delta"] is None
    assert undefined["absolute_delta"] == pytest.approx(40.0)
    assert NO_BASELINE_LABEL in undefined["rendered_item"]

    measured = firings[str(baselined_metric)]
    assert measured["percent_delta"] == pytest.approx(300.0)
    assert "300.0%" in measured["rendered_item"]


def test_the_replay_firing_keeps_a_real_float_for_its_two_other_consumers() -> None:
    """``SimulatedRuleFiring`` is a shared DTO, and this is why the fix is a
    serializer rather than the validator its two siblings use.

    ``AlertDeliveryItemResponse`` and ``AlertInboxGroupResponse`` null the value
    in a ``@model_validator(mode="after")``. They can: they are response-only.
    This model is also what the preview renderer and the demo seeder read, and
    a validator fires at CONSTRUCTION, so copying that idiom here would hand
    ``None`` to both.

    Each assertion names one consumer and reverts differently. Retype the field
    ``float | None`` and null it at construction and the first three go red
    together — the renderer with a ``TypeError`` on its ``:.1f``, the seeder
    with a ``None`` bound for ``AlertDeliveryItem.percent_delta``, which is NOT
    NULL. Delete the serializer and only the last two go red.

    The ``${percent_delta}`` half of the rendered string is asserted on purpose:
    it is a DOCUMENTED bare number (``website/docs/use/alerting.md``, "Message
    templates", pinned by ``test_alert_message_rendering``), so an operator's
    saved template still prints ``0.0`` here. Nulling the encoding was allowed
    to change the JSON and nothing else.
    """
    firing = SimulatedRuleFiring(
        anomaly_id=uuid.uuid4(),
        scope_type="event",
        scope_ref="checkout:completed",
        scope_name="checkout:completed",
        event_type_id=None,
        event_id=None,
        bucket=datetime(2026, 9, 1, 12, tzinfo=UTC),
        direction="spike",
        actual_count=40.0,
        expected_count=0.0,
        absolute_delta=40.0,
        percent_delta=0.0,
    )

    # 1. The attribute is untouched — still the stored placeholder, still a float.
    assert firing.percent_delta == 0.0

    # 2. ...which is what keeps the preview alive for the zero-baseline class.
    rendered = render_firing_item(
        firing,
        message_format="plain",
        items_template="${scope_name}: ${percent_delta} / ${percent_delta_label}",
    )
    assert rendered == f"checkout:completed: 0.0 / {NO_BASELINE_LABEL}"

    # 3. ...and what keeps the demo seeder able to fill a NOT NULL column.
    item = _delivery_item(firing, uuid.uuid4(), uuid.uuid4())
    assert item.percent_delta == 0.0

    # 4. Only the ENCODING changed, and in both dump modes, because FastAPI
    #    serializes a response through ``dump_python(mode="json")`` while a
    #    reader debugging one reaches for the plain ``model_dump``.
    assert firing.model_dump()["percent_delta"] is None
    assert json.loads(firing.model_dump_json())["percent_delta"] is None

    # 5. A real baseline is still reported, through the same serializer.
    measured = firing.model_copy(update={"expected_count": 10.0, "percent_delta": 300.0})
    assert measured.model_dump()["percent_delta"] == pytest.approx(300.0)


def test_the_published_replay_contract_says_the_percent_can_be_null() -> None:
    """The annotation on the serializer is load-bearing, so pin what it publishes.

    FastAPI builds a response model's OpenAPI from pydantic's SERIALIZATION
    schema, which is where ``encode_percent_delta``'s ``-> float | None`` lands.
    Drop that return annotation and the body still goes ``null`` while the
    contract still promises a ``number`` — a silent lie to every generated
    client, and nothing else in the suite would notice.

    ``required`` is asserted beside it because nullable and optional are
    different promises and this repo keeps saying so (``event_id``,
    ``acted_by_name``, ``AlertDestinationTestResponse.error``): the server never
    omits this key, so a generated client must not call it optional.

    This reads the LIVE schema, so it is green the moment the serializer lands.
    ``test_openapi_contract.py`` compares the live schema against the committed
    ``backend/openapi.json`` and stays red until that snapshot and
    ``frontend/src/types/api.gen.ts`` are regenerated — which is a separate,
    deliberate step, not something this file can assert its way out of.
    """
    schema = app.openapi()["components"]["schemas"]["SimulatedRuleFiring"]

    assert schema["properties"]["percent_delta"]["anyOf"] == [
        {"type": "number"},
        {"type": "null"},
    ], schema["properties"]["percent_delta"]
    assert "percent_delta" in schema["required"]


# ---------------------------------------------------------------------------
# the replay quoted a sigma column the detector never reads
# ---------------------------------------------------------------------------


async def _seed_sigma_project(
    client: AsyncClient,
    slug: str,
    *,
    scan_sigmas: tuple[float, ...],
    project_sigma: float | None,
) -> list[uuid.UUID]:
    """A replayable project plus BOTH places a sigma threshold is written down.

    ``scan_sigmas`` fills one ``ScanConfig.sigma_threshold`` per scan — the
    per-scan copy the replay used to quote, which no API writes and which
    ``worker.tasks.metrics.detect`` never reads — while ``project_sigma`` writes,
    or with ``None`` deliberately withholds, the ``ProjectAnomalySettings`` row
    the detector actually scores against.

    Every value a caller passes is distinct from ``DEFAULT_SIGMA_THRESHOLD`` and
    from every other value in the same fixture, on purpose: the pre-fix bug was
    invisible in the existing coverage precisely because both columns default to
    4.0, so an assertion on 4.0 cannot say which one answered.

    No anomalies are seeded. ``sigma_threshold_saved`` is quoted from settings
    rather than derived from the candidate rows, so an empty window exercises it
    exactly as a busy one would and the fixture stays readable.
    """
    resp = await client.post("/api/v1/projects", json={"name": f"Sigma {slug}", "slug": slug})
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    scan_ids: list[uuid.UUID] = []
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"ds-160-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        for index, sigma in enumerate(scan_sigmas):
            scan_id = uuid.uuid4()
            session.add(
                ScanConfig(
                    id=scan_id,
                    data_source_id=data_source.id,
                    project_id=project_id,
                    name=f"sc-160-{index}",
                    base_query="SELECT 1",
                    cardinality_threshold=100,
                    interval="1h",
                    sigma_threshold=sigma,
                )
            )
            scan_ids.append(scan_id)
        if project_sigma is not None:
            session.add(
                ProjectAnomalySettings(
                    project_id=project_id,
                    anomaly_detection_enabled=True,
                    sigma_threshold=project_sigma,
                )
            )
    return scan_ids


def _simulate_url(slug: str, destination_id: str, rule_id: str) -> str:
    return f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate"


@pytest.mark.asyncio
async def test_the_replay_quotes_the_projects_detector_sigma_not_the_scan_column(
    client: AsyncClient,
) -> None:
    """The number the dialog calls "saved" has to be the number that detects.

    Sigma lives in two columns and only one of them is real:
    ``ProjectAnomalySettings.sigma_threshold``, which ``detect._build_anomaly_settings``
    turns into the ``AnomalyDetectionSettings`` every bucket is scored against,
    and ``ScanConfig.sigma_threshold``, which no API writes and no detector reads.
    The replay quoted the second one. An operator who raised sensitivity to 5.0
    in Detection settings was shown 4.0 — or here 2.5 — and every what-if typed
    into this dialog was then reasoned about against a threshold nothing detects
    with: an "override" of 3.0 reads as a tightening while it is really a
    loosening of 5.0, and one of 4.5 drops nothing because every stored row
    already cleared 5.0.

    Reverting the fix reddens the first assertion with 2.5, the dead column's
    value — not with a null, which is what makes it name the wrong SOURCE rather
    than merely a missing value.
    """
    slug = "batch4-sigma-project"
    scan_ids = await _seed_sigma_project(client, slug, scan_sigmas=(2.5,), project_sigma=5.0)
    destination = await _make_destination(client, slug, "Sigma Slack")
    rule_id = await _make_rule(client, slug, destination, "Sigma Rule")
    url = _simulate_url(slug, destination, rule_id)

    replay = await client.post(f"{url}?days=7")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["sigma_threshold_saved"] == 5.0
    assert body["sigma_threshold_used"] == 5.0

    # An override still replaces only ``used``; ``saved`` keeps quoting the
    # project, which is the whole point of reporting the pair.
    overridden = await client.post(f"{url}?days=7&sigma_threshold_override=3")
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["sigma_threshold_used"] == 3.0
    assert overridden.json()["sigma_threshold_saved"] == 5.0

    # Fixture guard. Without it the claim "reverting reddens this" would rest on
    # a seed that might silently not have happened: with no scan row at all the
    # OLD code also stops answering 2.5, and this test would pass against the bug.
    async with TestSessionLocal() as session:
        stored = (
            (
                await session.execute(
                    select(ScanConfig.sigma_threshold).where(ScanConfig.id.in_(scan_ids))
                )
            )
            .scalars()
            .all()
        )
    assert list(stored) == [2.5]
    # ...and 2.5 is neither the project's 5.0 nor the ``DEFAULT_SIGMA_THRESHOLD``
    # the absent-row branch answers with, so nothing but the dead column can
    # produce it.
    assert DEFAULT_SIGMA_THRESHOLD != 2.5


@pytest.mark.asyncio
async def test_the_replay_has_one_sigma_to_quote_whatever_the_scans_hold(
    client: AsyncClient,
) -> None:
    """Both answers the scan column used to give, replaced by the one true one.

    Two scans holding different values used to make ``sigma_threshold_saved``
    NULL for a project-wide rule ("the scans disagree, so there is nothing to
    quote"), while a rule bound to one scan quoted THAT scan's number. Neither
    was a fact about detection: the detector scores every scan in the project
    against the single project threshold, so both rules here are measured against
    5.0 and both must say so.

    Reverting the fix reddens the two arms differently — the wide rule goes back
    to ``None`` and the bound rule to 6.0 — which is why both are asserted in one
    test: they are two faces of one wrong source, not two behaviours.
    """
    slug = "batch4-sigma-scans"
    scan_ids = await _seed_sigma_project(
        client,
        slug,
        scan_sigmas=(2.5, 6.0),
        project_sigma=5.0,
    )
    destination = await _make_destination(client, slug, "Sigma Slack")
    wide_rule = await _make_rule(client, slug, destination, "All scans")
    bound_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination}/rules",
        json={"name": "One scan", "scan_config_id": str(scan_ids[1])},
    )
    assert bound_resp.status_code == 201, bound_resp.text
    bound_rule = str(bound_resp.json()["id"])
    # The binding is what used to make the two rules disagree, so pin that it
    # really took rather than being dropped as an unknown field.
    assert bound_resp.json()["scan_config_id"] == str(scan_ids[1])

    for rule_id in (wide_rule, bound_rule):
        replay = await client.post(f"{_simulate_url(slug, destination, rule_id)}?days=7")
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert body["sigma_threshold_saved"] == 5.0, rule_id
        assert body["sigma_threshold_used"] == 5.0, rule_id


@pytest.mark.asyncio
async def test_a_project_with_no_detection_settings_row_quotes_the_default(
    client: AsyncClient,
) -> None:
    """The fallback is the value the settings row would be born with.

    ``ProjectAnomalySettings`` is created lazily, only by the Detection settings
    endpoint, so a project nobody has opened that screen for has no row at all —
    and ``detect`` reads that as "detection disabled" and writes no anomalies, so
    a replay there has nothing to measure anyway. What matters is WHICH constant
    fills the gap: ``DEFAULT_SIGMA_THRESHOLD``, the same fallback the
    false-positive ratchet uses for the same value, and not whatever the scan row
    happens to be carrying.

    The scan column is seeded to 6.0 so that reverting the fix reddens this with
    6.0; if it were left at its own default the two sources would agree and the
    assertion would prove nothing — which is exactly how the pre-fix suite passed.
    """
    slug = "batch4-sigma-default"
    await _seed_sigma_project(client, slug, scan_sigmas=(6.0,), project_sigma=None)
    destination = await _make_destination(client, slug, "Sigma Slack")
    rule_id = await _make_rule(client, slug, destination, "Sigma Rule")

    replay = await client.post(f"{_simulate_url(slug, destination, rule_id)}?days=7")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["sigma_threshold_saved"] == DEFAULT_SIGMA_THRESHOLD
    assert body["sigma_threshold_used"] == DEFAULT_SIGMA_THRESHOLD
    assert body["sigma_threshold_saved"] != 6.0
