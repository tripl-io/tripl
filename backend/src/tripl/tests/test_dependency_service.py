"""Dependency graph resolver (GH #257, F04).

Covers every edge kind the service resolves, the direct / possible split (SQL
identifier matches are only ever ``possible``), the two-hop walk, branch
copies (new ids, references stored against main) and an entity whose row is
gone. The plan is seeded through the API; the project-wide rows that have no
plan editor (metrics, fact tables, alert rules, variable contexts) through the
ORM.
"""

import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.fact_table import FactTable
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.variable_value import VariableValue
from tripl.services import dependency_service
from tripl.services._dependency_model import Edge, impact_summary
from tripl.services._dependency_sql import sql_mentions
from tripl.services.dependency_service import EntityRef
from tripl.services.plan_branch_service import ensure_main_branch_id
from tripl.tests.conftest import TestSessionLocal


@dataclass
class Seeded:
    slug: str
    project_id: uuid.UUID
    track: uuid.UUID
    screen: uuid.UUID
    amount: uuid.UUID
    cart_id: uuid.UUID
    user_ref: uuid.UUID
    purchase: uuid.UUID
    refund: uuid.UUID
    variable: uuid.UUID
    relation: uuid.UUID
    composition_metric: uuid.UUID
    ratio_metric: uuid.UUID
    type_metric: uuid.UUID
    sql_metric: uuid.UUID
    literal_sql_metric: uuid.UUID
    fact_metric: uuid.UUID
    fact_orders: uuid.UUID
    fact_quoted: uuid.UUID
    rule_event: uuid.UUID
    rule_type: uuid.UUID
    rule_metric: uuid.UUID


async def _post(client: AsyncClient, url: str, body: dict[str, object]) -> dict[str, object]:
    resp = await client.post(url, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


async def seed_dependency_plan(client: AsyncClient, slug: str) -> Seeded:
    """A small plan with one of every reference the dependency graph reads."""
    await _post(client, "/api/v1/projects", {"name": slug, "slug": slug, "description": ""})
    base = f"/api/v1/projects/{slug}"
    track = str(
        (await _post(client, f"{base}/event-types", {"name": "track", "display_name": "T"}))["id"]
    )
    screen = str(
        (await _post(client, f"{base}/event-types", {"name": "screen", "display_name": "S"}))["id"]
    )
    amount = str(
        (
            await _post(
                client,
                f"{base}/event-types/{track}/fields",
                {"name": "amount", "display_name": "Amount", "field_type": "number"},
            )
        )["id"]
    )
    cart_id = str(
        (
            await _post(
                client,
                f"{base}/event-types/{track}/fields",
                {"name": "cart_id", "display_name": "Cart", "field_type": "string"},
            )
        )["id"]
    )
    user_ref = str(
        (
            await _post(
                client,
                f"{base}/event-types/{screen}/fields",
                {"name": "user_ref", "display_name": "User", "field_type": "string"},
            )
        )["id"]
    )
    variable = str((await _post(client, f"{base}/variables", {"name": "cart"}))["id"])
    purchase = str(
        (
            await _post(
                client,
                f"{base}/events",
                {
                    "event_type_id": track,
                    "name": "purchase",
                    "metric_breakdown_columns": ["amount"],
                    "field_values": [{"field_definition_id": cart_id, "value": "${cart}"}],
                },
            )
        )["id"]
    )
    refund = str(
        (await _post(client, f"{base}/events", {"event_type_id": track, "name": "refund"}))["id"]
    )
    relation = str(
        (
            await _post(
                client,
                f"{base}/relations",
                {
                    "source_event_type_id": track,
                    "target_event_type_id": screen,
                    "source_field_id": cart_id,
                    "target_field_id": user_ref,
                },
            )
        )["id"]
    )

    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
        main_branch_id = await ensure_main_branch_id(session, project_id)
        session.add(
            VariableValue(
                project_id=project_id,
                branch_id=main_branch_id,
                variable_id=uuid.UUID(variable),
                event_id=uuid.UUID(purchase),
                field_definition_id=uuid.UUID(cart_id),
                source_column="cart_id",
                value_kind="low",
                observed_count=3,
                values=["a", "b"],
            )
        )

        def metric(name: str, kind: str, **extra: object) -> MetricDefinition:
            return MetricDefinition(
                id=uuid.uuid4(),
                project_id=project_id,
                name=name,
                display_name=name.replace("_", " ").title(),
                kind=kind,
                status="active",
                interval="1h",
                **extra,
            )

        fact_orders = FactTable(
            id=uuid.uuid4(),
            project_id=project_id,
            name="orders",
            display_name="Orders",
            sql="SELECT ts, amount_total, user_id FROM orders -- amount lives elsewhere",
            timestamp_column="ts",
            columns=[
                {"name": "ts", "type": "timestamp"},
                {"name": "amount_total", "type": "number"},
            ],
        )
        fact_quoted = FactTable(
            id=uuid.uuid4(),
            project_id=project_id,
            name="payments",
            display_name="Payments",
            sql='SELECT ts, "amount" FROM payments',
            timestamp_column="ts",
            columns=[],
        )
        session.add_all([fact_orders, fact_quoted])
        await session.flush()
        composition = metric(
            "purchases",
            "event_composition",
            composition="single",
            numerator_event_id=uuid.UUID(purchase),
            breakdown_columns=["amount"],
            config={},
        )
        ratio = metric(
            "refund_rate",
            "event_composition",
            composition="ratio",
            numerator_event_id=uuid.UUID(refund),
            denominator_event_id=uuid.UUID(purchase),
            config={},
        )
        type_level = metric(
            "track_volume",
            "event_composition",
            composition="single",
            numerator_event_type_id=uuid.UUID(track),
            config={},
        )
        sql_metric = metric(
            "revenue_sql",
            "sql",
            config={"metric_sql": "SELECT ts AS bucket, sum(t.amount) AS value FROM orders t"},
        )
        literal_sql = metric(
            "notes_sql",
            "sql",
            config={
                "metric_sql": "SELECT ts AS bucket, count(*) AS value FROM n WHERE k = 'amount'"
            },
        )
        fact_metric = metric(
            "order_total",
            "fact",
            fact_table_id=fact_orders.id,
            config={"measure_column": "amount_total"},
        )
        session.add_all([composition, ratio, type_level, sql_metric, literal_sql, fact_metric])
        destination = AlertDestination(
            project_id=project_id,
            type="slack",
            name="Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        session.add(destination)
        await session.flush()

        def rule(name: str, field: str, value: uuid.UUID | str) -> AlertRule:
            created = AlertRule(
                id=uuid.uuid4(),
                destination_id=destination.id,
                name=name,
                enabled=True,
                min_percent_delta=0,
                min_absolute_delta=0,
                min_expected_count=0,
                cooldown_minutes=60,
            )
            session.add(created)
            session.add(
                AlertRuleFilter(
                    rule_id=created.id, field=field, operator="in", values=[str(value)], position=0
                )
            )
            return created

        rule_event = rule("Purchase watch", "event", purchase)
        rule_type = rule("Track watch", "event_type", track)
        rule_metric = rule("Purchases metric watch", "metric", composition.id)
        await session.commit()

        return Seeded(
            slug=slug,
            project_id=project_id,
            track=uuid.UUID(track),
            screen=uuid.UUID(screen),
            amount=uuid.UUID(amount),
            cart_id=uuid.UUID(cart_id),
            user_ref=uuid.UUID(user_ref),
            purchase=uuid.UUID(purchase),
            refund=uuid.UUID(refund),
            variable=uuid.UUID(variable),
            relation=uuid.UUID(relation),
            composition_metric=composition.id,
            ratio_metric=ratio.id,
            type_metric=type_level.id,
            sql_metric=sql_metric.id,
            literal_sql_metric=literal_sql.id,
            fact_metric=fact_metric.id,
            fact_orders=fact_orders.id,
            fact_quoted=fact_quoted.id,
            rule_event=rule_event.id,
            rule_type=rule_type.id,
            rule_metric=rule_metric.id,
        )


async def _resolve(
    seeded: Seeded,
    kind: str,
    entity_id: uuid.UUID,
    *,
    branch_id: uuid.UUID | None = None,
    depth: int = 1,
) -> dependency_service.Dependencies:
    async with TestSessionLocal() as session:
        return await dependency_service.resolve(
            session,
            seeded.project_id,
            branch_id,
            EntityRef(kind, entity_id),
            depth=depth,
            slug=seeded.slug,
        )


def _by_id(edges: list[Edge]) -> dict[uuid.UUID, Edge]:
    return {edge.id: edge for edge in edges}


# ── edge kinds ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_edges(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-event")
    deps = await _resolve(s, "event", s.purchase)

    assert deps.exists and deps.name == "purchase"
    down = _by_id(deps.downstream)
    assert down[s.composition_metric].relation == "metric uses event in its composition"
    assert down[s.composition_metric].certainty == "direct"
    assert down[s.composition_metric].url_hint == (
        f"/p/dep-event/monitoring/metric/{s.composition_metric}"
    )
    # The ratio reads it as its denominator.
    assert s.ratio_metric in down
    assert down[s.rule_event].kind == "alert_rule"
    assert down[s.rule_event].relation == "alert rule filters on event"
    # Type-level things are not the event's dependents.
    assert s.type_metric not in down and s.rule_type not in down

    up = _by_id(deps.upstream)
    assert up[s.track].relation == "event belongs to event type"
    assert up[s.amount].relation == "event metric breakdown uses column"
    assert up[s.variable].kind == "variable"


@pytest.mark.asyncio
async def test_event_type_edges(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-type")
    deps = await _resolve(s, "event_type", s.track)

    down = _by_id(deps.downstream)
    assert {s.purchase, s.refund} <= set(down)
    assert down[s.type_metric].relation == "metric uses event type in its composition"
    assert down[s.rule_type].relation == "alert rule filters on event type"
    assert down[s.relation].relation == "relation links event type"
    assert down[s.relation].name == "track.cart_id → screen.user_ref"
    assert all(edge.depth == 1 for edge in deps.downstream)


@pytest.mark.asyncio
async def test_field_edges_direct_and_possible(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-field")
    deps = await _resolve(s, "field", s.amount)

    assert deps.name == "track.amount"
    down = _by_id(deps.downstream)
    # Direct: a composition metric over an event of this type breaks down by it,
    # and the event's own metric breakdown names it.
    assert down[s.composition_metric].relation == "metric breakdown uses column"
    assert down[s.composition_metric].certainty == "direct"
    assert down[s.purchase].relation == "event metric breakdown uses column"
    assert down[s.purchase].certainty == "direct"
    # Possible: whole identifiers in free SQL, quoted or qualified.
    assert down[s.sql_metric].certainty == "possible"
    assert down[s.fact_quoted].certainty == "possible"
    # A string literal equal to the name is a (possible) JSON-accessor match.
    assert down[s.literal_sql_metric].certainty == "possible"
    # Not a match: a comment and a longer identifier.
    assert s.fact_orders not in down
    assert s.fact_metric not in down

    up = _by_id(deps.upstream)
    assert up[s.track].relation == "field belongs to event type"


@pytest.mark.asyncio
async def test_field_relation_and_variable_binding(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-field-rel")
    deps = await _resolve(s, "field", s.cart_id)

    down = _by_id(deps.downstream)
    assert down[s.relation].relation == "relation links field"
    assert down[s.variable].relation == "variable bound to field"
    assert down[s.variable].certainty == "direct"
    # The field's stored value names the variable with a ${token}.
    assert _by_id(deps.upstream)[s.variable].relation == "field value uses variable"


@pytest.mark.asyncio
async def test_variable_edges(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-var")
    deps = await _resolve(s, "variable", s.variable)

    down = _by_id(deps.downstream)
    assert down[s.purchase].kind == "event"
    assert down[s.cart_id].kind == "field"
    assert down[s.cart_id].relation == "variable bound to field"
    assert deps.upstream == []


@pytest.mark.asyncio
async def test_metric_fact_table_alert_rule_and_relation_edges(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-misc")

    metric = await _resolve(s, "metric", s.composition_metric)
    assert _by_id(metric.downstream)[s.rule_metric].relation == "alert rule is scoped to metric"
    assert _by_id(metric.upstream)[s.purchase].kind == "event"

    fact_metric = await _resolve(s, "metric", s.fact_metric)
    assert _by_id(fact_metric.upstream)[s.fact_orders].relation == "metric reads fact table"

    table = await _resolve(s, "fact_table", s.fact_orders)
    assert _by_id(table.downstream)[s.fact_metric].relation == "metric reads fact table"

    rule = await _resolve(s, "alert_rule", s.rule_event)
    assert _by_id(rule.upstream)[s.purchase].relation == "alert rule filters on event"
    assert rule.downstream == []

    relation = await _resolve(s, "relation", s.relation)
    up = _by_id(relation.upstream)
    assert {s.track, s.screen, s.cart_id, s.user_ref} <= set(up)


# ── depth, branches, deleted rows ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_depth_two_reaches_neighbours_of_neighbours(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-depth")
    one = await _resolve(s, "event_type", s.track)
    assert s.rule_event not in _by_id(one.downstream)

    two = await _resolve(s, "event_type", s.track, depth=2)
    down = _by_id(two.downstream)
    # track → purchase → the metric and the alert rule on purchase.
    assert down[s.rule_event].depth == 2
    assert down[s.composition_metric].depth == 2
    # A first-hop edge keeps depth 1 when it is also reachable in two.
    assert down[s.purchase].depth == 1
    # Never itself.
    assert s.track not in down


@pytest.mark.asyncio
async def test_branch_copy_resolves_against_main_references(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-branch")
    created = await client.post(f"/api/v1/projects/{s.slug}/branches", json={"name": "wip"})
    assert created.status_code == 201, created.text
    branch_id = uuid.UUID(created.json()["id"])

    async with TestSessionLocal() as session:
        copy_id = await session.scalar(
            select(Event.id).where(Event.branch_id == branch_id, Event.origin_id == s.purchase)
        )
        branch_amount = await session.scalar(
            select(FieldDefinition.id)
            .join(EventType, FieldDefinition.event_type_id == EventType.id)
            .where(EventType.branch_id == branch_id, FieldDefinition.name == "amount")
        )
    assert copy_id is not None and copy_id != s.purchase
    assert branch_amount is not None

    # The branch copy's own id still finds the metrics and rules stored against main.
    by_copy = await _resolve(s, "event", copy_id, branch_id=branch_id)
    assert by_copy.entity.id == copy_id
    assert {s.composition_metric, s.ratio_metric, s.rule_event} <= set(_by_id(by_copy.downstream))

    # The main id asked on the branch lands on the branch copy.
    by_main_id = await _resolve(s, "event", s.purchase, branch_id=branch_id)
    assert by_main_id.entity.id == copy_id

    # A branch field reaches the composition metric through the type's main twin.
    field = await _resolve(s, "field", branch_amount, branch_id=branch_id)
    assert s.composition_metric in _by_id(field.downstream)
    # ...and the event breakdown edge points at the branch's own event row.
    assert copy_id in _by_id(field.downstream)


@pytest.mark.asyncio
async def test_deleted_entity_reports_what_still_names_it(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-deleted")
    # Remove the row underneath the alert filter without the delete path's
    # cleanup, the way a dangling reference comes to exist.
    async with TestSessionLocal() as session:
        await session.execute(delete(Event).where(Event.id == s.purchase))
        await session.commit()

    deps = await _resolve(s, "event", s.purchase)
    assert deps.exists is False and deps.name is None
    assert s.rule_event in _by_id(deps.downstream)

    unknown = await _resolve(s, "metric", uuid.uuid4())
    assert unknown.exists is False
    assert unknown.upstream == [] and unknown.downstream == []


# ── pure helpers ────────────────────────────────────────────────────────────


def test_sql_identifier_matching_is_whole_word_and_case_insensitive() -> None:
    assert sql_mentions("SELECT Amount FROM t", "amount")
    assert sql_mentions('SELECT "amount" FROM t', "amount")
    assert sql_mentions("SELECT `proj.ds.t`.amount FROM `proj.ds.t`", "amount")
    assert sql_mentions("SELECT payload.user.id FROM t", "user.id")
    assert not sql_mentions("SELECT amount_total FROM t", "amount")
    assert not sql_mentions("SELECT 1 FROM t -- amount\n", "amount")
    assert not sql_mentions("SELECT 1 /* amount */ FROM t", "amount")
    assert not sql_mentions(None, "amount")


def test_sql_json_literal_matching() -> None:
    # A single-quoted literal names a column through a JSON accessor.
    assert sql_mentions("SELECT 1 FROM t WHERE k = 'amount'", "amount")
    assert sql_mentions("SELECT JSON_VALUE(payload, '$.Amount') FROM t", "amount")
    assert sql_mentions("SELECT get_json_object(p, '$.order.amount') FROM t", "amount")
    assert sql_mentions("SELECT payload->>'amount' FROM t", "amount")
    assert not sql_mentions("SELECT 1 FROM t WHERE k = 'amount_total'", "amount")
    assert not sql_mentions("SELECT 1 FROM t WHERE k = '$.amounts'", "amount")
    assert not sql_mentions("SELECT 1 FROM t WHERE k = 'the amount'", "amount")
    # ``#`` is not a comment (JSON paths, #legacySQL headers).
    assert sql_mentions("#legacySQL\nSELECT amount FROM t", "amount")


def _edge(kind: str, certainty: str = "direct") -> Edge:
    return Edge(kind=kind, id=uuid.uuid4(), name="x", relation="r", certainty=certainty)


def test_impact_summary_wording() -> None:
    assert impact_summary([]) == "Nothing depends on it"
    assert impact_summary([_edge("metric"), _edge("metric"), _edge("alert_rule")]) == (
        "2 metrics and 1 alert rule"
    )
    assert impact_summary([_edge("metric"), _edge("event"), _edge("relation")]) == (
        "1 metric, 1 event and 1 relation"
    )
    assert impact_summary([_edge("metric"), _edge("fact_table", "possible")]) == (
        "1 metric, plus 1 fact table that may use it"
    )
    assert impact_summary([_edge("metric", "possible")]) == "1 metric that may use it"


# ── isolation, meta values, scans, supersession, overrides ─────────────────


@pytest.mark.asyncio
async def test_foreign_project_ids_resolve_to_nothing(client: AsyncClient) -> None:
    mine = await seed_dependency_plan(client, "dep-iso-a")
    theirs = await seed_dependency_plan(client, "dep-iso-b")
    for kind, entity_id in (
        ("event", theirs.purchase),
        ("event_type", theirs.track),
        ("field", theirs.amount),
        ("variable", theirs.variable),
        ("metric", theirs.composition_metric),
        ("fact_table", theirs.fact_orders),
        ("alert_rule", theirs.rule_event),
        ("relation", theirs.relation),
    ):
        deps = await _resolve(mine, kind, entity_id, depth=2)
        assert deps.exists is False, kind
        assert deps.name is None, kind
        assert deps.upstream == [] and deps.downstream == [], kind


@pytest.mark.asyncio
async def test_meta_value_token_is_a_variable_reference(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-meta")
    base = f"/api/v1/projects/{s.slug}"
    promo = uuid.UUID(str((await _post(client, f"{base}/variables", {"name": "promo"}))["id"]))
    meta = str(
        (
            await _post(
                client,
                f"{base}/meta-fields",
                {"name": "campaign", "display_name": "Campaign", "field_type": "string"},
            )
        )["id"]
    )
    checkout = uuid.UUID(
        str(
            (
                await _post(
                    client,
                    f"{base}/events",
                    {
                        "event_type_id": str(s.track),
                        "name": "checkout",
                        "meta_values": [{"meta_field_definition_id": meta, "value": "${promo}"}],
                    },
                )
            )["id"]
        )
    )

    variable = await _resolve(s, "variable", promo)
    edge = _by_id(variable.downstream)[checkout]
    assert edge.kind == "event"
    assert edge.relation == "event uses variable in a meta value"

    event = await _resolve(s, "event", checkout)
    assert _by_id(event.upstream)[promo].relation == "event uses variable in a meta value"


def _scan(project_id: uuid.UUID, name: str, **extra: object) -> ScanConfig:
    return ScanConfig(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        time_column="ts",
        cardinality_threshold=100,
        interval="1h",
        **extra,
    )


@pytest.mark.asyncio
async def test_scan_column_settings_read_the_field(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-scan")
    async with TestSessionLocal() as session:
        source = DataSource(
            id=uuid.uuid4(),
            name=f"Dep DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add(source)
        await session.flush()
        bound = _scan(
            s.project_id,
            "Bound drift",
            data_source_id=source.id,
            event_type_id=s.track,
            base_query="SELECT * FROM events",
            distribution_drift_fields=["cart_id"],
        )
        unbound = _scan(
            s.project_id,
            "Unbound platform",
            data_source_id=source.id,
            event_type_id=None,
            base_query="SELECT * FROM events",
            platform_column="cart_id",
        )
        other_type = _scan(
            s.project_id,
            "Other type, SQL names it",
            data_source_id=source.id,
            event_type_id=s.screen,
            base_query="SELECT ts, cart_id FROM events",
            app_version_column="cart_id",
        )
        unrelated = _scan(
            s.project_id,
            "Other type, no mention",
            data_source_id=source.id,
            event_type_id=s.screen,
            base_query="SELECT * FROM events",
            distribution_drift_fields=["cart_id"],
        )
        session.add_all([bound, unbound, other_type, unrelated])
        await session.commit()

    deps = await _resolve(s, "field", s.cart_id)
    down = _by_id(deps.downstream)
    assert down[bound.id].kind == "scan_config"
    assert down[bound.id].certainty == "direct"
    assert down[bound.id].relation == "scan tracks distribution drift on column"
    assert down[unbound.id].certainty == "possible"
    assert down[unbound.id].relation == "scan reads column as its platform"
    # Another type's column setting is not ours; its SQL naming it is a hint.
    assert down[other_type.id].certainty == "possible"
    assert down[other_type.id].relation == "scan query mentions the column by name"
    assert unrelated.id not in down


@pytest.mark.asyncio
async def test_json_literal_in_metric_sql_is_a_possible_edge(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-json-sql")
    async with TestSessionLocal() as session:
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=s.project_id,
            name="carts_json",
            display_name="Carts JSON",
            kind="sql",
            status="active",
            interval="1h",
            config={
                "metric_sql": (
                    "SELECT ts AS bucket, count(JSON_VALUE(payload, '$.cart_id')) AS value FROM e"
                )
            },
        )
        session.add(metric)
        await session.commit()

    deps = await _resolve(s, "field", s.cart_id)
    edge = _by_id(deps.downstream)[metric.id]
    assert edge.certainty == "possible"
    assert edge.relation == "metric SQL mentions the column by name"


@pytest.mark.asyncio
async def test_superseded_by_is_an_edge_both_ways(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-supersede")
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event).where(Event.id == s.refund).values(superseded_by_event_id=s.purchase)
        )
        await session.commit()

    old = await _resolve(s, "event", s.refund)
    assert _by_id(old.upstream)[s.purchase].relation == "superseded by"
    assert s.purchase not in _by_id(old.downstream)

    new = await _resolve(s, "event", s.purchase)
    assert _by_id(new.downstream)[s.refund].relation == "supersedes"


@pytest.mark.asyncio
async def test_detection_override_is_a_downstream_edge(client: AsyncClient) -> None:
    s = await seed_dependency_plan(client, "dep-override")
    async with TestSessionLocal() as session:
        override = AnomalyScopeOverride(
            id=uuid.uuid4(),
            project_id=s.project_id,
            scan_config_id=None,
            scope_type="event",
            scope_ref=str(s.purchase),
            scope_name="purchase",
            sigma_threshold=3.5,
            min_expected_count=10,
        )
        session.add(override)
        await session.commit()

    deps = await _resolve(s, "event", s.purchase)
    edge = _by_id(deps.downstream)[override.id]
    assert edge.kind == "detection_override"
    assert edge.url_hint == f"/p/{s.slug}/settings/monitoring"
    # Another event is not touched by it.
    assert override.id not in _by_id((await _resolve(s, "event", s.refund)).downstream)
