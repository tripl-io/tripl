"""Everything that pointed at an event, once the event is deleted outright.

``services._event_reference_cleanup`` is the delete-path twin of the group-merge
re-pointer. ``test_event_reference_policy`` already pins the pure decisions; this
file pins the EXECUTION — that the decision actually lands in the database, on
all three doors an event can leave by (single delete, bulk delete, event-type
delete).

Two things make this worth its own module rather than a couple of extra cases:

* **Every read-back here goes through a fresh session.** The JSON columns
  (``alert_rule_filters.values``, ``implementation_tickets.event_ids``) are plain
  ``JSON`` with no ``MutableList`` mapped anywhere, so an assertion made against
  an in-memory instance passes whether or not anything was written. That trap is
  documented at the top of ``core.event_references``, and it is the reason
  tripl-xfxa survived as long as it did.
* **Counting ``event_id == dead_id`` proves nothing on its own.** Those foreign
  keys are ``ON DELETE SET NULL``, so the count drops to zero even when the
  cleanup does nothing at all — the database just NULLed the column. The
  assertion that has teeth is that no row is left holding a NULL ``event_id``,
  because ``filter_matches_anomaly`` reads NULL as "matches", so an orphan
  satisfies every event filter written to scope it away.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import func, select

from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.release_regression import ReleaseRegression
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal

_BUCKET = datetime(2026, 8, 1, tzinfo=UTC)
_OTHER_BUCKET = datetime(2026, 8, 2, tzinfo=UTC)

# A ``scope_ref`` that is NOT any event's id. Anomaly rows are found by event_id
# OR by scope_ref, and a row carrying only one of the two keys is what proves
# both halves of that OR are live.
_UNRELATED_SCOPE_REF = "scope-ref-that-is-not-an-event"


@dataclass(frozen=True)
class _Fixture:
    """One project, two event types, one event each. Only ``doomed`` dies.

    The survivor sits under its OWN event type so that all three delete doors
    remove exactly the same single event — the event-type door would otherwise
    take the control row with it.
    """

    slug: str
    project_id: uuid.UUID
    branch_id: uuid.UUID
    doomed_event_type_id: uuid.UUID
    survivor_event_type_id: uuid.UUID
    doomed_id: uuid.UUID
    survivor_id: uuid.UUID
    scan_config_id: uuid.UUID

    @property
    def doomed_ref(self) -> str:
        return str(self.doomed_id)

    @property
    def survivor_ref(self) -> str:
        return str(self.survivor_id)


# --------------------------------------------------------------------------
# Fixture construction: over HTTP where an endpoint exists, direct where not
# --------------------------------------------------------------------------


async def _create_event_type(client: AsyncClient, slug: str, name: str) -> tuple[str, str]:
    response = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={
            "name": name,
            "display_name": name.replace("_", " ").title(),
            "field_definitions": [
                {"name": "payload", "display_name": "Payload", "field_type": "json"}
            ],
        },
    )
    assert response.status_code == 201, response.text
    event_type_id = response.json()["id"]

    fields = await client.get(f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields")
    assert fields.status_code == 200, fields.text
    return event_type_id, fields.json()[0]["id"]


async def _create_event(client: AsyncClient, slug: str, event_type_id: str, name: str) -> str:
    response = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": name, "field_values": []},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _seed_scan_config(project_id: uuid.UUID, name: str) -> uuid.UUID:
    """A scan config to hang anomalies off: their FK to it is NOT nullable."""
    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"{name} DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name=name,
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        await session.commit()
        return scan_config.id


async def _seed_project(client: AsyncClient, slug: str) -> _Fixture:
    project = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project.status_code == 201, project.text
    project_id = uuid.UUID(project.json()["id"])

    doomed_type_id, _ = await _create_event_type(client, slug, "checkout")
    survivor_type_id, _ = await _create_event_type(client, slug, "signup")
    doomed_id = uuid.UUID(await _create_event(client, slug, doomed_type_id, "checkout_started"))
    survivor_id = uuid.UUID(await _create_event(client, slug, survivor_type_id, "signup_started"))

    async with TestSessionLocal() as session:
        doomed = await session.get(Event, doomed_id)
        assert doomed is not None
        branch_id = doomed.branch_id

    return _Fixture(
        slug=slug,
        project_id=project_id,
        branch_id=branch_id,
        doomed_event_type_id=uuid.UUID(doomed_type_id),
        survivor_event_type_id=uuid.UUID(survivor_type_id),
        doomed_id=doomed_id,
        survivor_id=survivor_id,
        scan_config_id=await _seed_scan_config(project_id, "Main scan"),
    )


# --------------------------------------------------------------------------
# Seeding the references
# --------------------------------------------------------------------------


async def _assert_seeded(model: type, expected: int) -> None:
    """Guard against a fixture that never reached the database.

    Almost every assertion in this file is a count going to zero, and a fixture
    that silently seeded nothing satisfies all of them at once. Each seed helper
    therefore reads its own rows back through a fresh session before the test is
    allowed to prove anything about them.
    """
    assert await _count(model) == expected, f"{model.__name__} fixture did not land"


def _anomaly(fx: _Fixture, *, event_id: uuid.UUID | None, scope_ref: str, bucket: datetime):
    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=fx.scan_config_id,
        scope_type="event",
        scope_ref=scope_ref,
        event_id=event_id,
        event_type_id=None,
        bucket=bucket,
        actual_count=100.0,
        expected_count=10.0,
        stddev=1.0,
        z_score=9.0,
        effective_stddev=1.0,
        detector_kind="phase",
        direction="spike",
    )


def _breakdown_anomaly(
    fx: _Fixture, *, event_id: uuid.UUID | None, scope_ref: str, bucket: datetime
):
    return MetricBreakdownAnomaly(
        id=uuid.uuid4(),
        scan_config_id=fx.scan_config_id,
        scope_type="event",
        scope_ref=scope_ref,
        event_id=event_id,
        event_type_id=None,
        bucket=bucket,
        breakdown_column="country",
        breakdown_value="DE",
        is_other=False,
        kind="volume",
        actual_count=100.0,
        expected_count=10.0,
        stddev=1.0,
        z_score=9.0,
        effective_stddev=1.0,
        detector_kind="phase",
        direction="spike",
    )


async def _seed_anomalies(fx: _Fixture) -> None:
    """Three rows per table: one reachable by each key, and one control.

    ``scope_ref`` carries no foreign key — it is a plain string — so the two
    keys genuinely do not travel together, and each row here is reachable by
    only one of them. A cleanup that dropped either half of the OR would leave
    one of them behind.
    """
    async with TestSessionLocal() as session:
        session.add_all(
            [
                _anomaly(fx, event_id=fx.doomed_id, scope_ref=_UNRELATED_SCOPE_REF, bucket=_BUCKET),
                _anomaly(fx, event_id=None, scope_ref=fx.doomed_ref, bucket=_OTHER_BUCKET),
                _anomaly(fx, event_id=fx.survivor_id, scope_ref=fx.survivor_ref, bucket=_BUCKET),
                _breakdown_anomaly(
                    fx, event_id=fx.doomed_id, scope_ref=_UNRELATED_SCOPE_REF, bucket=_BUCKET
                ),
                _breakdown_anomaly(
                    fx, event_id=None, scope_ref=fx.doomed_ref, bucket=_OTHER_BUCKET
                ),
                _breakdown_anomaly(
                    fx, event_id=fx.survivor_id, scope_ref=fx.survivor_ref, bucket=_BUCKET
                ),
            ]
        )
        await session.commit()
    await _assert_seeded(MetricAnomaly, 3)
    await _assert_seeded(MetricBreakdownAnomaly, 3)


async def _seed_scope_overrides(fx: _Fixture) -> None:
    """An event-scoped ratchet, plus a metric-scoped one with the SAME ref.

    ``scope_ref`` is polymorphic across seven scope types, so the only thing
    keeping a metric override safe from an event delete is the ``scope_type``
    predicate. The collision is seeded deliberately.
    """
    async with TestSessionLocal() as session:
        session.add_all(
            [
                AnomalyScopeOverride(
                    id=uuid.uuid4(),
                    project_id=fx.project_id,
                    scan_config_id=fx.scan_config_id,
                    scope_type="event",
                    scope_ref=fx.doomed_ref,
                    scope_name="checkout_started",
                    sigma_threshold=4.5,
                    min_expected_count=15,
                    false_positive_count=1,
                ),
                AnomalyScopeOverride(
                    id=uuid.uuid4(),
                    project_id=fx.project_id,
                    scan_config_id=None,
                    scope_type="metric",
                    scope_ref=fx.doomed_ref,
                    scope_name="a metric that merely shares the string",
                    sigma_threshold=5.0,
                    min_expected_count=20,
                    false_positive_count=2,
                ),
            ]
        )
        await session.commit()
    await _assert_seeded(AnomalyScopeOverride, 2)


async def _seed_annotations(fx: _Fixture) -> None:
    async with TestSessionLocal() as session:
        session.add_all(
            [
                ChartAnnotation(
                    id=uuid.uuid4(),
                    project_id=fx.project_id,
                    scope_type="event",
                    scope_ref=fx.doomed_ref,
                    bucket=_BUCKET,
                    label="deploy on the doomed event",
                ),
                ChartAnnotation(
                    id=uuid.uuid4(),
                    project_id=fx.project_id,
                    scope_type=None,
                    scope_ref=None,
                    bucket=_BUCKET,
                    label="project-wide outage",
                ),
            ]
        )
        await session.commit()
    await _assert_seeded(ChartAnnotation, 2)


async def _seed_tickets(fx: _Fixture) -> tuple[uuid.UUID, uuid.UUID]:
    open_id = uuid.uuid4()
    closed_id = uuid.uuid4()
    # The closed ticket needs its own branch: uq_implementation_ticket_branch
    # allows one ticket per branch.
    shipped_branch_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            PlanBranch(
                id=shipped_branch_id,
                project_id=fx.project_id,
                name="shipped",
                kind=BranchKind.working.value,
                status=BranchStatus.draft.value,
            )
        )
        await session.flush()
        session.add_all(
            [
                ImplementationTicket(
                    id=open_id,
                    project_id=fx.project_id,
                    branch_id=fx.branch_id,
                    status="open",
                    summary="still being implemented",
                    event_ids=[fx.doomed_ref, fx.survivor_ref],
                ),
                ImplementationTicket(
                    id=closed_id,
                    project_id=fx.project_id,
                    branch_id=shipped_branch_id,
                    status="closed",
                    summary="a record of what shipped",
                    event_ids=[fx.doomed_ref, fx.survivor_ref],
                ),
            ]
        )
        await session.commit()
    return open_id, closed_id


async def _create_destination(client: AsyncClient, slug: str) -> str:
    response = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_rule(
    client: AsyncClient,
    slug: str,
    destination_id: str,
    *,
    name: str,
    filters: list[dict[str, object]],
) -> uuid.UUID:
    response = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={"name": name, "enabled": True, "filters": filters},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _rule_with_event_filter(
    client: AsyncClient, fx: _Fixture, *, operator: str, values: list[str]
) -> uuid.UUID:
    destination_id = await _create_destination(client, fx.slug)
    rule_id = await _create_rule(
        client,
        fx.slug,
        destination_id,
        name=f"{operator} rule",
        filters=[{"field": "event", "operator": operator, "values": values}],
    )
    # Same reason as _assert_seeded: "the filter row is gone" is satisfied for
    # free by a rule that never had one.
    assert await _filter_rows(rule_id) == [("event", operator, values)]
    return rule_id


# --------------------------------------------------------------------------
# The three doors an event can leave by
# --------------------------------------------------------------------------


async def _delete_event(client: AsyncClient, fx: _Fixture) -> None:
    response = await client.delete(f"/api/v1/projects/{fx.slug}/events/{fx.doomed_id}")
    assert response.status_code == 204, response.text


async def _bulk_delete_event(client: AsyncClient, fx: _Fixture) -> None:
    response = await client.post(
        f"/api/v1/projects/{fx.slug}/events/bulk-delete",
        json={"event_ids": [fx.doomed_ref]},
    )
    assert response.status_code == 204, response.text


async def _delete_event_type(client: AsyncClient, fx: _Fixture) -> None:
    response = await client.delete(
        f"/api/v1/projects/{fx.slug}/event-types/{fx.doomed_event_type_id}"
    )
    assert response.status_code == 204, response.text


# --------------------------------------------------------------------------
# Read-backs. Every one opens a NEW session — see the module docstring.
# --------------------------------------------------------------------------


async def _filter_rows(rule_id: uuid.UUID) -> list[tuple[str, str, list[str]]]:
    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AlertRuleFilter)
                    .where(AlertRuleFilter.rule_id == rule_id)
                    .order_by(AlertRuleFilter.position, AlertRuleFilter.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [(row.field, row.operator, list(row.values or [])) for row in rows]


async def _rule_enabled(rule_id: uuid.UUID) -> bool:
    async with TestSessionLocal() as session:
        rule = await session.get(AlertRule, rule_id)
        assert rule is not None, "an orphaned rule must be disabled, never deleted"
        return rule.enabled


async def _count(model: type, *criteria: object) -> int:
    async with TestSessionLocal() as session:
        return (
            await session.scalar(select(func.count()).select_from(model).where(*criteria))  # type: ignore[arg-type]
        ) or 0


async def _ticket_event_ids(ticket_id: uuid.UUID) -> list[str]:
    async with TestSessionLocal() as session:
        ticket = await session.get(ImplementationTicket, ticket_id)
        assert ticket is not None
        return list(ticket.event_ids or [])


# --------------------------------------------------------------------------
# 1. The endpoint guard — the failure that takes a whole screen down
# --------------------------------------------------------------------------


async def test_the_destinations_endpoint_survives_deleting_an_event_a_filter_named(
    client: AsyncClient,
) -> None:
    """The Alerting tab must still render after the delete.

    This is the worst outcome the design is guarding against, and it is invisible
    to a service-level assertion: persisting ``values=[]`` fails the "must have at
    least one value" validator that ``AlertRuleFilterResponse`` inherits, so ONE
    emptied filter 500s the destinations endpoint for the entire project —
    including the screen an operator would have to open to repair it. The rule
    must therefore come back visibly disabled rather than not come back at all.
    """
    fx = await _seed_project(client, "evt-del-http")
    rule_id = await _rule_with_event_filter(client, fx, operator="in", values=[fx.doomed_ref])

    await _delete_event(client, fx)

    response = await client.get(f"/api/v1/projects/{fx.slug}/alert-destinations")
    assert response.status_code == 200, response.text
    rules = response.json()[0]["rules"]
    assert [uuid.UUID(rule["id"]) for rule in rules] == [rule_id]
    assert rules[0]["filters"] == []
    assert rules[0]["enabled"] is False


# --------------------------------------------------------------------------
# 2-5. Alert rule filters
# --------------------------------------------------------------------------


async def test_an_emptied_inclusive_filter_is_deleted_and_takes_its_rule_out_of_service(
    client: AsyncClient,
) -> None:
    """Deleting the row alone would WIDEN the rule to everything it watches.

    A rule narrowed to one event would start paging on every event its
    destination sees, which is the opposite of what its author asked for.
    """
    fx = await _seed_project(client, "evt-del-in")
    rule_id = await _rule_with_event_filter(client, fx, operator="in", values=[fx.doomed_ref])

    async with TestSessionLocal() as session:
        session.add(
            AlertRuleState(
                id=uuid.uuid4(),
                rule_id=rule_id,
                scan_config_id=fx.scan_config_id,
                scope_type="event",
                scope_ref=fx.doomed_ref,
                is_active=True,
            )
        )
        await session.commit()

    await _delete_event(client, fx)

    assert await _filter_rows(rule_id) == []
    assert await _rule_enabled(rule_id) is False
    assert await _count(AlertRuleState, AlertRuleState.rule_id == rule_id) == 0, (
        "a disabled rule must not keep an open incident for a scope that is gone"
    )


async def test_an_emptied_exclusive_filter_is_deleted_and_leaves_its_rule_running(
    client: AsyncClient,
) -> None:
    """ "Exclude this event" genuinely degrades to "exclude nothing" once it is gone.

    The author's intent survives the delete intact, so — unlike the inclusive
    case — there is nothing to switch off.
    """
    fx = await _seed_project(client, "evt-del-notin")
    rule_id = await _rule_with_event_filter(client, fx, operator="not_in", values=[fx.doomed_ref])

    await _delete_event(client, fx)

    assert await _filter_rows(rule_id) == []
    assert await _rule_enabled(rule_id) is True


async def test_a_multi_value_filter_loses_only_the_dead_id_and_keeps_its_order(
    client: AsyncClient,
) -> None:
    """Order is what the operator sees as the chip order in the filter editor."""
    fx = await _seed_project(client, "evt-del-multi")
    # A real third event: the rules endpoint 404s a filter value that names no
    # event, so a bare uuid4 cannot stand in for one here.
    third = await _create_event(client, fx.slug, str(fx.survivor_event_type_id), "signup_completed")
    rule_id = await _rule_with_event_filter(
        client, fx, operator="in", values=[fx.survivor_ref, fx.doomed_ref, third]
    )

    await _delete_event(client, fx)

    assert await _filter_rows(rule_id) == [("event", "in", [fx.survivor_ref, third])]
    assert await _rule_enabled(rule_id) is True


async def test_a_direction_filter_holding_the_same_string_is_left_alone(
    client: AsyncClient,
) -> None:
    """Only ``field='event'`` is in scope, and the field is the ONLY thing saying so.

    Seeded directly because the create API rejects a direction value that is not
    ``up``/``down`` — which is exactly why a row like this can only ever arrive
    by some other route, and why the cleanup must not match on the value alone.
    """
    fx = await _seed_project(client, "evt-del-direction")
    destination_id = await _create_destination(client, fx.slug)
    rule_id = await _create_rule(
        client,
        fx.slug,
        destination_id,
        name="direction rule",
        filters=[{"field": "direction", "operator": "in", "values": ["up"]}],
    )
    async with TestSessionLocal() as session:
        row = (
            (
                await session.execute(
                    select(AlertRuleFilter).where(AlertRuleFilter.rule_id == rule_id)
                )
            )
            .scalars()
            .one()
        )
        row.values = [fx.doomed_ref]
        await session.commit()

    await _delete_event(client, fx)

    assert await _filter_rows(rule_id) == [("direction", "in", [fx.doomed_ref])]
    assert await _rule_enabled(rule_id) is True


# --------------------------------------------------------------------------
# 6-9. The rows that outlive the event
# --------------------------------------------------------------------------


async def _assert_anomalies_are_gone(fx: _Fixture) -> None:
    for model in (MetricAnomaly, MetricBreakdownAnomaly):
        assert await _count(model, model.event_id == fx.doomed_id) == 0
        assert await _count(model, model.scope_ref == fx.doomed_ref) == 0
        # The two assertions above are NOT sufficient on their own: the FK is
        # ON DELETE SET NULL, so the first one reads zero even when nothing was
        # cleaned up — the database merely NULLed the column. A surviving NULL
        # row is the whole defect, because filter_matches_anomaly treats a NULL
        # actual value as a match and the row then satisfies every event filter.
        assert await _count(model, model.event_id.is_(None)) == 0, (
            "a NULL event_id survives every event filter — the row must be deleted"
        )
        assert await _count(model, model.event_id == fx.survivor_id) == 1, (
            "another event's anomalies are not collateral"
        )


async def test_a_deleted_events_anomalies_are_deleted_by_both_keys(client: AsyncClient) -> None:
    """Archiving an event suppresses its alerts; deleting one used to UN-suppress them.

    Both FKs are ``ON DELETE SET NULL``, and both gates that would have contained
    the orphan read NULL as "allow", so the rows have to go rather than be
    re-pointed. There is nowhere to re-point them to in any case: the anomaly
    described a series that went with the event.
    """
    fx = await _seed_project(client, "evt-del-anomalies")
    await _seed_anomalies(fx)

    await _delete_event(client, fx)

    await _assert_anomalies_are_gone(fx)


async def test_an_event_scoped_ratchet_is_dropped_and_a_colliding_metric_one_is_not(
    client: AsyncClient,
) -> None:
    """Deleting the override row IS the undo, per the model's own contract.

    Leaving it would strand a tuned threshold in Detection settings naming an
    event nobody can open — but ``scope_ref`` is polymorphic across seven scope
    types, so a metric override that happens to hold the same string must be
    untouched.
    """
    fx = await _seed_project(client, "evt-del-override")
    await _seed_scope_overrides(fx)

    await _delete_event(client, fx)

    assert (
        await _count(
            AnomalyScopeOverride,
            AnomalyScopeOverride.scope_type == "event",
            AnomalyScopeOverride.scope_ref == fx.doomed_ref,
        )
        == 0
    )
    assert (
        await _count(
            AnomalyScopeOverride,
            AnomalyScopeOverride.scope_type == "metric",
            AnomalyScopeOverride.scope_ref == fx.doomed_ref,
        )
        == 1
    ), "scope_ref is polymorphic; only the event-scoped row was aimed at this event"


async def test_an_event_scoped_annotation_is_dropped_and_a_project_wide_one_is_not(
    client: AsyncClient,
) -> None:
    """Promoting the marker to project-wide was rejected on purpose.

    That would paint a deleted event's annotation onto every chart in the
    project, and the reader could never select it again to remove it.
    """
    fx = await _seed_project(client, "evt-del-annotation")
    await _seed_annotations(fx)

    await _delete_event(client, fx)

    assert await _count(ChartAnnotation, ChartAnnotation.scope_ref == fx.doomed_ref) == 0
    assert await _count(ChartAnnotation, ChartAnnotation.scope_type.is_(None)) == 1


async def test_an_open_ticket_loses_the_id_and_a_closed_one_keeps_it(
    client: AsyncClient,
) -> None:
    """A closed ticket records what shipped and is never rewritten."""
    fx = await _seed_project(client, "evt-del-ticket")
    open_id, closed_id = await _seed_tickets(fx)

    await _delete_event(client, fx)

    assert await _ticket_event_ids(open_id) == [fx.survivor_ref]
    assert await _ticket_event_ids(closed_id) == [fx.doomed_ref, fx.survivor_ref]


# --------------------------------------------------------------------------
# 10-11. The whole battery through the other two doors
# --------------------------------------------------------------------------


async def _seed_everything(
    client: AsyncClient, fx: _Fixture
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    await _seed_anomalies(fx)
    await _seed_scope_overrides(fx)
    await _seed_annotations(fx)
    open_id, closed_id = await _seed_tickets(fx)
    rule_id = await _rule_with_event_filter(client, fx, operator="in", values=[fx.doomed_ref])
    return rule_id, open_id, closed_id


async def _assert_everything_cleared(
    client: AsyncClient, fx: _Fixture, refs: Sequence[uuid.UUID]
) -> None:
    rule_id, open_id, closed_id = refs

    await _assert_anomalies_are_gone(fx)
    assert await _count(AnomalyScopeOverride, AnomalyScopeOverride.scope_type == "event") == 0
    assert await _count(AnomalyScopeOverride, AnomalyScopeOverride.scope_type == "metric") == 1
    assert await _count(ChartAnnotation, ChartAnnotation.scope_ref == fx.doomed_ref) == 0
    assert await _count(ChartAnnotation, ChartAnnotation.scope_type.is_(None)) == 1
    assert await _ticket_event_ids(open_id) == [fx.survivor_ref]
    assert await _ticket_event_ids(closed_id) == [fx.doomed_ref, fx.survivor_ref]
    assert await _filter_rows(rule_id) == []
    assert await _rule_enabled(rule_id) is False

    # And the page an operator would use to see all of that still renders.
    response = await client.get(f"/api/v1/projects/{fx.slug}/alert-destinations")
    assert response.status_code == 200, response.text


async def test_bulk_delete_clears_every_reference_too(client: AsyncClient) -> None:
    """The bulk door is a Core DELETE: no ORM cascade runs at ALL.

    Everything not covered by a database-level foreign key survives untouched
    here unless the cleanup runs explicitly, which makes this the door where
    forgetting it is least visible.
    """
    fx = await _seed_project(client, "evt-del-bulk")
    refs = await _seed_everything(client, fx)

    await _bulk_delete_event(client, fx)

    assert await _count(Event, Event.id == fx.doomed_id) == 0
    await _assert_everything_cleared(client, fx, refs)


async def test_deleting_an_event_type_clears_its_events_references_too(
    client: AsyncClient,
) -> None:
    """The most common door, and the least visible: a pure database FK cascade.

    ``EventType`` maps no ``events`` relationship, so its events are removed by
    the database alone and no service ever sees them go. Their dangling
    references have to be cleared here or nowhere.
    """
    fx = await _seed_project(client, "evt-del-type")
    refs = await _seed_everything(client, fx)

    await _delete_event_type(client, fx)

    assert await _count(Event, Event.id == fx.doomed_id) == 0
    assert await _count(Event, Event.id == fx.survivor_id) == 1
    await _assert_everything_cleared(client, fx, refs)


def _release_regression(fx: _Fixture, *, event_id: uuid.UUID | None, scope_ref: str, version: str):
    return ReleaseRegression(
        id=uuid.uuid4(),
        scan_config_id=fx.scan_config_id,
        scope_type="event",
        scope_ref=scope_ref,
        event_id=event_id,
        event_type_id=None,
        app_version_column="app_version",
        version=version,
        previous_version="1.0.0",
        kind="missing",
        observed_count=0,
        expected_count=120.0,
        ratio=0.0,
        share_prev=0.4,
        share_new=0.0,
        release_share=0.6,
        window_from=datetime(2026, 1, 1, tzinfo=UTC),
        window_to=datetime(2026, 1, 8, tzinfo=UTC),
    )


async def test_a_deleted_events_release_regressions_are_deleted_by_both_keys(
    client: AsyncClient,
) -> None:
    """The same un-suppression as the anomalies, behind a recompute that hides it.

    ``release_regressions.event_id`` is ON DELETE SET NULL too, and the table is
    wiped and rebuilt per scan config on every collection — which looks like a
    fix and is not. Until that next collection the orphan sits there with
    ``scope_type='event'`` and a live ``scope_ref``, ``signals`` lifts it into a
    drift candidate, and drift candidates match on a bare
    ``all(filter_matches_anomaly(...))`` where a NULL ``event_id`` satisfies
    every event filter. Self-healing within one scan interval is not harmless.

    Asserting ``event_id IS NULL`` is zero is the assertion with teeth: counting
    rows still pointing AT the dead id reads zero either way, because the FK
    NULLed them.
    """
    fx = await _seed_project(client, "del-regressions")
    async with TestSessionLocal() as session:
        session.add_all(
            [
                _release_regression(
                    fx, event_id=fx.doomed_id, scope_ref=str(uuid.uuid4()), version="2.0.0"
                ),
                _release_regression(
                    fx, event_id=None, scope_ref=str(fx.doomed_id), version="2.1.0"
                ),
                _release_regression(
                    fx, event_id=fx.survivor_id, scope_ref=str(fx.survivor_id), version="2.2.0"
                ),
            ]
        )
        await session.commit()
    await _assert_seeded(ReleaseRegression, 3)

    await _delete_event(client, fx)

    async with TestSessionLocal() as session:
        rows = (await session.execute(select(ReleaseRegression))).scalars().all()
    assert len(rows) == 1, "both the event_id row and the scope_ref row must go"
    assert rows[0].event_id == fx.survivor_id
    assert not [row for row in rows if row.event_id is None], (
        "an orphan with a NULL event_id is the row that alerts past every filter"
    )


# ---------------------------------------------------------------------------
# The branch doors
#
# Three more ways an event disappears, none of them with a survivor to
# re-point onto — so the rule is the same DROP the CRUD doors use. Two of the
# three take their events through a database cascade that no service ever sees,
# which is exactly why the executor's callers are pinned in the FK ledger.
# ---------------------------------------------------------------------------


async def _branch(client: AsyncClient, slug: str, name: str) -> str:
    response = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_deleting_a_branch_clears_its_events_references(client: AsyncClient) -> None:
    """The door the issue never named, and the only one reaching branch-local events.

    PlanBranch maps no ``events`` relationship, so a branch delete takes every
    event on it through the database cascade with nothing in the ORM reporting
    it. Project-scoped rows really can name a branch-local event — the alert-rule
    filter picker lists events on the ACTIVE branch — so this is the door where a
    stranded reference is most likely, not least.
    """
    fx = await _seed_project(client, "del-branch")
    branch_id = await _branch(client, fx.slug, "work")

    # An event that exists only on the branch, with the same reference set the
    # other doors are tested against.
    async with TestSessionLocal() as session:
        branch_event = Event(
            id=uuid.uuid4(),
            project_id=fx.project_id,
            branch_id=uuid.UUID(branch_id),
            event_type_id=fx.doomed_event_type_id,
            name="branch_only",
            source_name="branch_only",
            order=99,
            status="in_review",
        )
        session.add(branch_event)
        await session.flush()
        branch_event_id = branch_event.id
        session.add_all(
            [
                _anomaly(
                    fx,
                    event_id=branch_event_id,
                    scope_ref=str(uuid.uuid4()),
                    bucket=datetime(2026, 3, 1, tzinfo=UTC),
                ),
                ChartAnnotation(
                    id=uuid.uuid4(),
                    project_id=fx.project_id,
                    scope_type="event",
                    scope_ref=str(branch_event_id),
                    bucket=datetime(2026, 3, 1, tzinfo=UTC),
                    label="branch marker",
                ),
            ]
        )
        await session.commit()

    response = await client.delete(f"/api/v1/projects/{fx.slug}/branches/{branch_id}")
    assert response.status_code == 204, response.text

    async with TestSessionLocal() as session:
        anomalies = (await session.execute(select(MetricAnomaly))).scalars().all()
        annotations = (await session.execute(select(ChartAnnotation))).scalars().all()
    assert not [a for a in anomalies if a.event_id is None], (
        "a NULL event_id is the orphan that alerts past every filter"
    )
    assert str(branch_event_id) not in {a.scope_ref for a in anomalies}
    assert str(branch_event_id) not in {a.scope_ref for a in annotations}


# Revert's door is NOT covered behaviourally here, and that is a deliberate,
# recorded gap rather than an oversight. Driving `revert_change` end to end
# needs a branch diff entry whose (entity_type, name, parent) key matches what
# `_find_entity` looks the entity up by, and getting that fixture right cost
# more than the coverage was worth given the same executor is already proven
# through four other doors above. What IS pinned is that revert calls it at all:
# `test_every_caller_of_the_delete_path_is_pinned_here` in
# test_event_fk_classification.py fails if the call is removed.
