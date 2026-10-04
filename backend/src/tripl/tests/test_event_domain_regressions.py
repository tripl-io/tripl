"""The events domain core.

nothing checked that ``event_type_id`` (or
``meta_field_definition_id``) belongs to the project AND branch the event is
written to, so a row authored on a branch against main's type held MAIN's scan
identity and quietly intercepted main's field values, contexts and metrics.

on a branch, "Silent > N days" and "Busiest first" read the
COPY's frozen ``last_seen_at`` and its nonexistent metric rows, while the Last
seen column beside them showed the main twin's live value.

a JSON token the scan writes raw — a display-name collision
fallback, an excluded path, a legacy variable — failed the web form's
identifier-grammar check, so every later save of that event returned 422.

the ``scan_identity`` create skipped name generation but still
enforced required field values, which a shadow candidate can never carry, so
Reconciliation → Accept 422'd on any event type with one required field.

``scan_configs.event_type_id`` is ``ON DELETE SET NULL``, so
deleting a bound event type turns its config project-wide and its name format
starts governing every type nothing else binds.

only the web form lower-cased a tag, so every other door stored
the spelling it was handed — invisible to the ``?tag=`` equality the docs
promise — and a repeated or over-long tag reached the database as a 500.

the bulk paste took the same ``EventCreate`` the single create
takes and then dropped ``owner_id`` and ``reviewed`` on the floor, so one
payload meant two different things depending on which door it came through.

``PATCH`` with ``metric_breakdown_columns: null`` assigned None
to a NOT NULL column and then reached ``" ".join(None)`` in the search document
builder inside the same transaction, so the save rolled back as a 500 instead of
clearing the list.

the previous schema took the unbounded meta value into a unique btree,
where an entry over 2704 bytes is ProgramLimitExceeded — a 500 on the event save
for a value that stored fine while the key was the two uuids alone.

the template-warning pass selected the mapped ``Variable``, so
every event create and PATCH pulled the branch's whole variable graph — each
``value_contexts`` row with its JSON values, and each of those rows' field
definition — across the async request path to read three scalars.

``EventBulkUpdate`` judged "was anything provided?" by the
values rather than the fields sent, so a bulk ``owner_id: null`` was a 422 on
its own and was dropped in silence beside another field — the one clearing the
API offered could not be spelled, while the same body on metrics unassigns.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import select, update

from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_tag import EventTag
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW, ShadowEventCandidate
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.services import event_service
from tripl.tests.conftest import TestSessionLocal, engine

# A moment nothing in this file runs at, so "stamped long ago" can never be
# confused with "stamped by this test a second ago".
_LONG_AGO = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


# --- helpers ------------------------------------------------------------------


async def _project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _event_type(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _field(
    client: AsyncClient,
    slug: str,
    event_type_id: str,
    name: str,
    *,
    field_type: str = "string",
    is_required: bool = False,
) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={
            "name": name,
            "display_name": name.title(),
            "field_type": field_type,
            "is_required": is_required,
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _branch(client: AsyncClient, slug: str, name: str = "feature") -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": name})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _branch_event_type(client: AsyncClient, slug: str, branch_id: str, name: str) -> str:
    """The branch's OWN copy of an event type — a deep copy carries a new id."""
    resp = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    return str(next(et["id"] for et in resp.json() if et["name"] == name))


async def _meta_field(
    client: AsyncClient, slug: str, name: str, *, link_template: str | None = None
) -> str:
    body: dict[str, object] = {
        "name": name,
        "display_name": name.title(),
        "field_type": "string",
    }
    if link_template is not None:
        body["link_template"] = link_template
    resp = await client.post(f"/api/v1/projects/{slug}/meta-fields", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _branch_meta_field(client: AsyncClient, slug: str, branch_id: str, name: str) -> str:
    resp = await client.get(f"/api/v1/projects/{slug}/meta-fields?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    return str(next(mf["id"] for mf in resp.json() if mf["name"] == name))


async def _event_names(client: AsyncClient, slug: str, query: str = "") -> list[str]:
    resp = await client.get(f"/api/v1/projects/{slug}/events{query}")
    assert resp.status_code == 200, resp.text
    return [item["name"] for item in resp.json()["items"]]


async def _scan_config(
    project_id: uuid.UUID,
    *,
    event_type_id: str | None = None,
    event_name_format: str | None = None,
    event_type_column: str | None = None,
) -> uuid.UUID:
    async with TestSessionLocal() as session:
        data_source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            event_type_id=uuid.UUID(event_type_id) if event_type_id else None,
            name=f"scan-{uuid.uuid4().hex[:8]}",
            base_query="SELECT * FROM events",
            time_column="time",
            event_type_column=event_type_column,
            event_name_format=event_name_format,
        )
        session.add(config)
        await session.commit()
        return config.id


@pytest.mark.asyncio
async def test_authoring_on_a_branch_refuses_mains_event_type_id(client: AsyncClient) -> None:
    """The type has to belong to the branch the event is written to.

    ``uq_event_scan_identity`` is ``(event_type_id, source_name)`` because a type
    lives on exactly one branch, so posting main's type id with ``?branch=``
    used to put a branch row in charge of MAIN's identity: the scan's own INSERT
    then collided with it and ``insert_event_claiming_identity`` wrote main's
    values onto the branch row.
    """
    slug = "b7a-type-scope"
    await _project(client, slug)
    main_type = await _event_type(client, slug, "track")
    branch_id = await _branch(client, slug)
    branch_type = await _branch_event_type(client, slug, branch_id, "track")
    assert branch_type != main_type, "the deep copy must carry its own id"

    refused = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": main_type, "name": "checkout:new"},
    )
    assert refused.status_code == 422, refused.text
    assert "not on this project's branch" in refused.json()["detail"]
    assert await _event_names(client, slug, f"?branch={branch_id}") == []

    accepted = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_type, "name": "checkout:new"},
    )
    assert accepted.status_code == 201, accepted.text
    assert await _event_names(client, slug, f"?branch={branch_id}") == ["checkout:new"]


@pytest.mark.asyncio
async def test_a_bulk_paste_names_the_item_whose_event_type_is_on_another_branch(
    client: AsyncClient,
) -> None:
    """The batch door applies the same rule, and says which of thirty is at fault."""
    slug = "b7a-type-scope-bulk"
    await _project(client, slug)
    main_type = await _event_type(client, slug, "track")
    branch_id = await _branch(client, slug)
    branch_type = await _branch_event_type(client, slug, branch_id, "track")

    refused = await client.post(
        f"/api/v1/projects/{slug}/events/bulk?branch={branch_id}",
        json=[
            {"event_type_id": branch_type, "name": "checkout:new"},
            {"event_type_id": main_type, "name": "checkout:done"},
        ],
    )
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail.startswith("Event 2 of 2: "), detail
    assert "not on this project's branch" in detail
    # The whole paste is refused, not the offending item alone.
    assert await _event_names(client, slug, f"?branch={branch_id}") == []


@pytest.mark.asyncio
async def test_a_branch_event_refuses_a_meta_field_belonging_to_main(
    client: AsyncClient,
) -> None:
    """A branch deep-copies meta field definitions under new ids too.

    Main's id used to fall through ``_normalize_meta_values``'s
    ``(None, False, "")`` default: no link stripping, the "one value here" rule
    silently off, and a branch row pointing at another branch's definition.
    """
    slug = "b7a-meta-scope"
    await _project(client, slug)
    await _event_type(client, slug, "track")
    main_meta = await _meta_field(client, slug, "jira")
    branch_id = await _branch(client, slug)
    branch_type = await _branch_event_type(client, slug, branch_id, "track")
    branch_meta = await _branch_meta_field(client, slug, branch_id, "jira")
    assert branch_meta != main_meta

    refused = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={
            "event_type_id": branch_type,
            "name": "checkout:new",
            "meta_values": [{"meta_field_definition_id": main_meta, "value": "PROJ-4770"}],
        },
    )
    assert refused.status_code == 422, refused.text
    assert "is not on this project's branch" in refused.json()["detail"]

    accepted = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={
            "event_type_id": branch_type,
            "name": "checkout:new",
            "meta_values": [{"meta_field_definition_id": branch_meta, "value": "PROJ-4770"}],
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert [mv["value"] for mv in accepted.json()["meta_values"]] == ["PROJ-4770"]


@pytest.mark.asyncio
async def test_a_bulk_paste_names_the_item_whose_meta_field_is_on_another_branch(
    client: AsyncClient,
) -> None:
    """The meta-field rule has to be refused where the event-type rule is.

    It was checked from the children loop, which runs after ``session.add_all``
    and ``flush`` have already INSERTed every row of the paste, and outside the
    ``try`` that prefixes "Event N of M" — so a twenty-item paste whose seventh
    item carried main's meta field id was answered with a bare "Meta field
    <uuid> is not on this project's branch", having paid for an insert of the
    whole batch to produce a message naming no item.

    RED on a revert, twice over: move the check back into the children loop and
    the detail loses its ``Event 2 of 3:`` prefix, so the ``startswith``
    assertion fails; the same revert also puts the events INSERT back on the
    wire ahead of the refusal, so the statement assertion fails too.
    """
    slug = "b7a-meta-scope-bulk"
    await _project(client, slug)
    await _event_type(client, slug, "track")
    main_meta = await _meta_field(client, slug, "jira")
    branch_id = await _branch(client, slug)
    branch_type = await _branch_event_type(client, slug, branch_id, "track")
    branch_meta = await _branch_meta_field(client, slug, branch_id, "jira")
    assert branch_meta != main_meta

    def item(name: str, meta_field_id: str) -> dict[str, object]:
        return {
            "event_type_id": branch_type,
            "name": name,
            "meta_values": [{"meta_field_definition_id": meta_field_id, "value": "PROJ-4770"}],
        }

    with _captured_sql() as statements:
        refused = await client.post(
            f"/api/v1/projects/{slug}/events/bulk?branch={branch_id}",
            json=[
                item("checkout:new", branch_meta),
                item("checkout:done", main_meta),
                item("checkout:failed", branch_meta),
            ],
        )
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail.startswith("Event 2 of 3: "), detail
    assert "is not on this project's branch" in detail

    # Refused BEFORE the write, not rolled back after it.
    inserts = [s for s in statements if s.lstrip().upper().startswith("INSERT INTO EVENTS ")]
    assert inserts == [], inserts
    assert await _event_names(client, slug, f"?branch={branch_id}") == []

    # Not vacuous: the identical paste lands once every item names the branch's
    # own copy of the meta field.
    accepted = await client.post(
        f"/api/v1/projects/{slug}/events/bulk?branch={branch_id}",
        json=[item("checkout:new", branch_meta), item("checkout:done", branch_meta)],
    )
    assert accepted.status_code == 201, accepted.text
    assert sorted(await _event_names(client, slug, f"?branch={branch_id}")) == [
        "checkout:done",
        "checkout:new",
    ]


@pytest.mark.asyncio
async def test_silent_since_days_on_a_branch_reads_the_main_twins_last_seen(
    client: AsyncClient,
) -> None:
    """The filter has to agree with the Last seen column beside it.

    A deep copy is stamped with the twin's ``last_seen_at`` once and never bumped
    again — only main rows are. So the copy's own column says "silent" for every
    event as soon as the branch itself is older than the cutoff, while the row
    renders the twin's fresh timestamp through ``attach_main_last_seen``.
    """
    slug = "b7a-silent-twin"
    project_id = await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    main_ids: dict[str, str] = {}
    for name in ("purchase:success", "checkout:abandoned"):
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type, "name": name},
        )
        assert created.status_code == 201, created.text
        main_ids[name] = created.json()["id"]

    # Both stamped long ago, so the branch copies inherit a stale value.
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event).where(Event.project_id == project_id).values(last_seen_at=_LONG_AGO)
        )
        await session.commit()

    branch_id = await _branch(client, slug)

    # After the copy only MAIN's row is bumped, because a scan only ever sees
    # main. Keyed on the id, not the name: the branch now holds a copy of both.
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event)
            .where(Event.id == uuid.UUID(main_ids["purchase:success"]))
            .values(last_seen_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    listed = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    assert listed.status_code == 200, listed.text
    shown = {item["name"]: item["last_seen_at"] for item in listed.json()["items"]}
    # What the list DISPLAYS, and therefore what the filter has to agree with.
    assert shown["purchase:success"] is not None
    assert not str(shown["purchase:success"]).startswith("2020-"), shown

    silent = await client.get(
        f"/api/v1/projects/{slug}/events?branch={branch_id}&silent_since_days=7"
    )
    assert silent.status_code == 200, silent.text
    body = silent.json()
    assert [item["name"] for item in body["items"]] == ["checkout:abandoned"]
    assert body["total"] == 1

    # Main answers the same question the same way.
    assert await _event_names(client, slug, "?silent_since_days=7") == ["checkout:abandoned"]


@pytest.mark.asyncio
async def test_order_by_volume_on_a_branch_sums_the_main_twins_metrics(
    client: AsyncClient,
) -> None:
    """Metrics are written for main ids only, so a branch sort keyed on the copy
    gave every row volume 0 and silently degraded to id order.

    The busiest event is deliberately the branch row with the HIGHEST id, so the
    id-ordered fallback puts it last and this assertion cannot pass by accident.
    """
    slug = "b7a-volume-twin"
    project_id = await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    for name in ("e1", "e2", "e3"):
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type, "name": name},
        )
        assert created.status_code == 201, created.text

    branch_id = await _branch(client, slug)
    branch_rows = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()[
        "items"
    ]
    assert len(branch_rows) == 3
    by_id = sorted(branch_rows, key=lambda row: uuid.UUID(row["id"]))
    busiest, *rest = by_id[::-1]
    quiet_in_id_order = [row["name"] for row in sorted(rest, key=lambda r: uuid.UUID(r["id"]))]

    config_id = await _scan_config(project_id, event_type_id=event_type)
    async with TestSessionLocal() as session:
        main_id = await session.scalar(
            select(Event.id).where(
                Event.project_id == project_id,
                Event.name == busiest["name"],
                Event.branch_id != uuid.UUID(branch_id),
            )
        )
        assert main_id is not None
        session.add(
            EventMetric(
                scan_config_id=config_id,
                event_id=main_id,
                bucket=datetime.now(UTC) - timedelta(hours=1),
                count=500,
            )
        )
        await session.commit()

    ordered = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}&order_by=volume")
    assert ordered.status_code == 200, ordered.text
    assert [item["name"] for item in ordered.json()["items"]] == [
        busiest["name"],
        *quiet_in_id_order,
    ]


@pytest.mark.asyncio
async def test_two_main_rows_under_one_key_give_the_branch_one_agreed_twin(
    client: AsyncClient,
) -> None:
    """When main holds two rows under one key, ONE of them is the twin.

    Nothing stops main from holding two events with one name under one event
    type, and nothing refuses that state: the plan diff warns and the merge goes
    through. While ``silent_since_days`` read ``max(last_seen_at)``
    over the pair and ``order_by=volume`` read the lowest-id row's metrics, the
    filter and the sort could answer about two different main rows for the same
    branch row. Both now read the lowest-id twin, by the same subquery shape.

    RED on a revert: put ``select(func.max(twin.last_seen_at))`` back and the
    filter reads the FRESH namesake instead of the stale lowest-id one, so the
    page comes back empty and both the item assertion and ``total == 1`` fail.
    """
    slug = "b7a-twin-ambiguous"
    project_id = await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    for _ in range(2):
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type, "name": "purchase"},
        )
        assert created.status_code == 201, created.text

    # Which of the namesakes is the lowest id is what decides the twin, so the
    # roles are assigned FROM the ids rather than from the creation order.
    async with TestSessionLocal() as session:
        namesakes = sorted(
            (
                await session.scalars(
                    select(Event.id).where(Event.project_id == project_id, Event.name == "purchase")
                )
            ).all()
        )
        assert len(namesakes) == 2
        lowest, highest = namesakes
        await session.execute(
            update(Event)
            .where(Event.id == lowest)
            .values(last_seen_at=_LONG_AGO, description="lowest id, never seen since 2020")
        )
        await session.execute(
            update(Event)
            .where(Event.id == highest)
            .values(
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
                description="highest id, seen an hour ago",
            )
        )
        await session.commit()

    branch_id = await _branch(client, slug)

    silent = await client.get(
        f"/api/v1/projects/{slug}/events?branch={branch_id}&silent_since_days=7"
    )
    assert silent.status_code == 200, silent.text
    body = silent.json()
    assert [item["description"] for item in body["items"]] == ["lowest id, never seen since 2020"]
    # The COUNT carries the same clause, so it has to agree with the page.
    assert body["total"] == 1

    # Main answers the same question the same way, off its own column.
    on_main = await client.get(f"/api/v1/projects/{slug}/events?silent_since_days=7")
    assert on_main.status_code == 200, on_main.text
    assert [item["description"] for item in on_main.json()["items"]] == [
        "lowest id, never seen since 2020"
    ]


@pytest.mark.asyncio
async def test_a_json_value_holding_a_raw_scan_written_token_saves_and_saves_again(
    client: AsyncClient,
) -> None:
    """The token grammar has to accept everything the scan can write.

    A JSON-path variable keeps its raw path whenever ``derive_display_name``
    cannot sanitise it into a free name, and an excluded or legacy path is never
    rewritten at all. The form re-sends every field value on save, so the old
    identifier grammar 422'd every later edit — including one that touched only
    the description.
    """
    slug = "b7a-json-token"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    payload = await _field(client, slug, event_type, "payload", field_type="json")
    scan_written = (
        '{"city": "${property.Albany, OR}", '
        '"where": "${property.Москва}", '
        '"blank": "${property.  }"}'
    )

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "screen:view",
            "field_values": [{"field_definition_id": payload, "value": scan_written}],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["field_values"][0]["value"] == scan_written
    event_id = created.json()["id"]

    # The headline scenario: EventForm re-sends every value while editing only
    # the description.
    edited = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={
            "description": "renamed in the city screen",
            "field_values": [{"field_definition_id": payload, "value": scan_written}],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["field_values"][0]["value"] == scan_written

    # Still not a blanket accept: a token that would break the JSON it is
    # spliced back into, and one that names nothing, are both refused.
    for broken in ('{"variant": ${bad"token}}', '{"variant": "${}"}'):
        refused = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": event_type,
                "name": f"broken:{broken[:12]}",
                "field_values": [{"field_definition_id": payload, "value": broken}],
            },
        )
        assert refused.status_code == 422, refused.text
        assert "invalid variable token" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_token_holding_a_raw_control_character_is_refused(
    client: AsyncClient,
) -> None:
    """The widened grammar accepts everything a scan can write, not everything.

    A token is spliced back into the dumped JSON VERBATIM, and the stash happens
    before ``json.loads`` ever sees the value — so a raw tab, newline or
    carriage return inside ``${...}`` would be stored as a control character
    inside a JSON string, which ``json.loads`` and the browser's ``JSON.parse``
    both refuse while this function went on accepting the value at every later
    save. Nothing would ever have reported it.

    RED on a revert: drop ``\\x00-\\x1f`` from the two patterns and each of these
    is accepted with 201, so every ``== 422`` below fails.
    """
    slug = "b7a-json-control"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    payload = await _field(client, slug, event_type, "payload", field_type="json")

    for label, broken in (
        ("tab", '{"city": "${property.x\ty}"}'),
        ("newline", '{"city": "${property.x\ny}"}'),
        ("carriage-return", '{"city": "${property.x\ry}"}'),
    ):
        refused = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": event_type,
                "name": f"screen:{label}",
                "field_values": [{"field_definition_id": payload, "value": broken}],
            },
        )
        assert refused.status_code == 422, (label, refused.text)
        assert "invalid variable token" in refused.json()["detail"], label

    # Not a retreat to the identifier grammar: an ordinary space inside a token
    # is exactly what the token-grammar fix made saveable, and still is.
    accepted = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "screen:spaced",
            "field_values": [
                {"field_definition_id": payload, "value": '{"city": "${property.x y}"}'}
            ],
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["field_values"][0]["value"] == '{"city": "${property.x y}"}'


@pytest.mark.asyncio
async def test_accepting_a_shadow_candidate_survives_a_required_field(
    client: AsyncClient,
) -> None:
    """A candidate carries no field values and the accept form offers nowhere to
    type one, so a required field could only ever refuse an event the warehouse
    has ALREADY seen. Name generation is skipped on this path for exactly that
    reason; the required check had been left behind.
    """
    slug = "b7a-shadow-required"
    project_id = await _project(client, slug)
    event_type = await _event_type(client, slug, "pv")
    await _field(client, slug, event_type, "screen", is_required=True)
    config_id = await _scan_config(project_id, event_type_id=event_type)

    async with TestSessionLocal() as session:
        candidate = ShadowEventCandidate(
            project_id=project_id,
            scan_config_id=config_id,
            event_type_id=uuid.UUID(event_type),
            event_name="screen | checkout",
            observed_count=42,
            first_seen_at=_LONG_AGO,
            last_seen_at=datetime.now(UTC),
            status=SHADOW_STATUS_NEW,
        )
        session.add(candidate)
        await session.commit()
        candidate_id = candidate.id

    accepted = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate_id}/accept",
        json={"name": "Checkout Screen"},
    )
    assert accepted.status_code == 200, accepted.text
    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(accepted.json()["event_id"]))
        assert event is not None
        assert event.name == "Checkout Screen"
        assert event.source_name == "screen | checkout"

    # The ordinary authoring door still demands the required field.
    refused = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type, "name": "typed by hand"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "Required field 'screen' is missing"


@pytest.mark.asyncio
async def test_a_scan_config_orphaned_by_a_deleted_event_type_names_nothing(
    client: AsyncClient,
) -> None:
    """``scan_configs.event_type_id`` is ``ON DELETE SET NULL``.

    Deleting the type a config binds — or merging a branch that removed it —
    leaves the config bound to nothing, and the ``IS NULL`` arm of the governing
    lookup then handed its name format to every type nothing else binds.
    Authoring on an unrelated type 422'd asking for columns that type has never
    had. A config with no binding governs the project only when it DISCOVERS its
    types from the data, through ``event_type_column``.
    """
    slug = "b7a-orphan-config"
    project_id = await _project(client, slug)
    bound_type = await _event_type(client, slug, "se")
    await _field(client, slug, bound_type, "category")
    await _field(client, slug, bound_type, "action")
    unbound_type = await _event_type(client, slug, "screen")
    config_id = await _scan_config(
        project_id, event_type_id=bound_type, event_name_format="{category}:{action}"
    )

    # The state ON DELETE SET NULL leaves behind. Written directly because the
    # cascade is a database behaviour, not the behaviour under test.
    async with TestSessionLocal() as session:
        await session.execute(
            update(ScanConfig).where(ScanConfig.id == config_id).values(event_type_id=None)
        )
        await session.commit()

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": unbound_type, "name": "Screen viewed"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Screen viewed"
    assert created.json()["source_name"] is None

    # A REAL grouped scan still governs every type in the project, so this is
    # not "unbound configs are ignored".
    async with TestSessionLocal() as session:
        await session.execute(
            update(ScanConfig)
            .where(ScanConfig.id == config_id)
            .values(event_type_column="event_type")
        )
        await session.commit()

    governed = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": unbound_type, "name": "Screen viewed again"},
    )
    assert governed.status_code == 422, governed.text
    assert "fill field values for: action, category" in governed.json()["detail"]


@pytest.mark.asyncio
async def test_a_tag_is_stored_lower_cased_and_trimmed_whichever_door_writes_it(
    client: AsyncClient,
) -> None:
    """``?tag=`` is an equality, so the stored spelling IS the lookup key.

    The feature reference says tags are lower-cased, and only ``EventForm.addTag``
    did it. MCP ``create_event``, the bulk paste and any raw client stored what
    they were handed, so ``Checkout`` never answered ``?tag=checkout`` and showed
    up in the tag list as a second label beside it.

    RED on a revert: drop the ``tags`` validator from ``EventCreate`` and the
    create stores ``Checkout``, ``  checkout `` and ``UX`` as three separate
    rows — the tag list below reads ``["  checkout ", "Checkout", "UX"]`` and the
    ``?tag=checkout`` filter finds nothing. Drop it from ``EventUpdate`` and the
    PATCH leaves two rows, ``BETA`` and ``Beta ``.
    """
    slug = "b7a-tag-case"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:start",
            "tags": ["Checkout", "  checkout ", "UX"],
        },
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    assert sorted(tag["name"] for tag in created.json()["tags"]) == ["checkout", "ux"]

    # The filter the docs promise, on the spelling a human typed.
    found = await client.get(f"/api/v1/projects/{slug}/events?tag=checkout")
    assert found.status_code == 200, found.text
    assert [item["name"] for item in found.json()["items"]] == ["checkout:start"]

    # One label, not three, on the facet the events page offers.
    tags = await client.get(f"/api/v1/projects/{slug}/events/tags")
    assert tags.json() == ["checkout", "ux"]

    # The update door holds the same rule — the form re-sends the whole list.
    patched = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"tags": ["BETA", "Beta "]},
    )
    assert patched.status_code == 200, patched.text
    assert [tag["name"] for tag in patched.json()["tags"]] == ["beta"]


@pytest.mark.asyncio
async def test_a_tag_repeated_in_one_payload_is_stored_once_instead_of_500ing(
    client: AsyncClient,
) -> None:
    """``uq_event_tag`` is ``(event_id, name)`` and the flush that trips it is unguarded.

    ``create_event``'s ``IntegrityError`` arm covers the event INSERT only, so a
    repeated tag raised at the later child flush and fell through to the generic
    handler in ``main.py`` as a bare 500. Deduping in the schema means the
    constraint is never reached by a client that simply said the same thing twice.

    RED on a revert: drop the ``tags`` validator and this create adds two
    ``EventTag`` rows with the same ``(event_id, 'ux')``, so the flush raises
    ``IntegrityError`` and the request answers 500 (or the exception escapes the
    in-process transport) rather than 201.
    """
    slug = "b7a-tag-repeat"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type, "name": "checkout:start", "tags": ["ux", "ux"]},
    )
    assert created.status_code == 201, created.text
    assert [tag["name"] for tag in created.json()["tags"]] == ["ux"]


@pytest.mark.asyncio
async def test_a_tag_longer_than_the_column_is_refused_rather_than_overflowing_it(
    client: AsyncClient,
) -> None:
    """``event_tags.name`` is ``String(100)``.

    Postgres raises ``DataError`` on the 101st character, which reached the
    client as a 500 with no hint at which tag was at fault. It is refused, not
    truncated: two long labels cut to the same 100 characters would then collide
    on ``uq_event_tag`` and fail the save of the whole event instead.

    RED on a revert: drop the ``tags`` validator and SQLite — which does not
    enforce the width — stores the 101-character tag and answers 201, so the
    ``== 422`` below fails.
    """
    slug = "b7a-tag-length"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")

    refused = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type, "name": "checkout:start", "tags": ["t" * 101]},
    )
    assert refused.status_code == 422, refused.text
    assert "longer than 100 characters" in refused.text

    accepted = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type, "name": "checkout:done", "tags": ["t" * 100]},
    )
    assert accepted.status_code == 201, accepted.text


@pytest.mark.asyncio
async def test_a_tag_stored_before_normalisation_is_still_reachable_from_the_facet(
    client: AsyncClient,
) -> None:
    """Normalising the write side does nothing for the rows already stored.

    No migration rewrites ``event_tags.name``, so every tag an MCP tool or a raw
    client wrote earlier keeps its spelling — and the tag facet only
    ever offers the lower-cased form now, so a pre-existing ``Checkout`` was a
    row the list displays that no filter the UI can spell could find. The read
    side case-folds instead: the facet lower-cases, and ``?tag=`` compares
    lower-cased on both sides.

    RED on a revert: put ``EventTag.name == tag`` back in ``list_events`` and
    each of the three spellings below reaches at most one of the two events, so
    every ``sorted(...) == [...]`` and every ``total == 2`` fails; put
    ``select(EventTag.name)`` back in ``list_tags`` and the facet answers with
    two entries for one label, so the one-label assertion fails.
    """
    slug = "b7a-tag-legacy"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")

    modern = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type, "name": "checkout:new", "tags": ["Checkout"]},
    )
    assert modern.status_code == 201, modern.text
    assert [tag["name"] for tag in modern.json()["tags"]] == ["checkout"]

    legacy = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type, "name": "checkout:old"},
    )
    assert legacy.status_code == 201, legacy.text
    # The row an un-normalised door left behind. Written directly because no
    # door writes one any more — that is the whole point of the fix.
    async with TestSessionLocal() as session:
        session.add(EventTag(event_id=uuid.UUID(legacy.json()["id"]), name="Checkout"))
        await session.commit()

    # One label on the facet, and it is the one the filter takes.
    facet = await client.get(f"/api/v1/projects/{slug}/events/tags")
    assert facet.status_code == 200, facet.text
    assert facet.json() == ["checkout"]

    for spelling in ("checkout", "Checkout", "CHECKOUT"):
        found = await client.get(f"/api/v1/projects/{slug}/events?tag={spelling}")
        assert found.status_code == 200, (spelling, found.text)
        body = found.json()
        assert sorted(item["name"] for item in body["items"]) == [
            "checkout:new",
            "checkout:old",
        ], spelling
        # The COUNT carries the same clause, so it has to agree with the page.
        assert body["total"] == 2, spelling

    # Still an equality, not a substring: a tag is free text and the filter must
    # not turn it into a pattern.
    narrow = await client.get(f"/api/v1/projects/{slug}/events?tag=check")
    assert narrow.status_code == 200, narrow.text
    assert narrow.json()["items"] == []


@pytest.mark.asyncio
async def test_a_bulk_paste_keeps_the_owner_and_reviewed_flag_it_was_given(
    client: AsyncClient,
) -> None:
    """One ``EventCreate`` payload, two doors, one meaning.

    ``bulk_create_events`` built its ``Event`` rows by hand and left ``owner_id``
    and ``reviewed`` out, so a paste that assigned an owner and marked the events
    reviewed was answered 201 with the model defaults — unassigned, unreviewed —
    while the very same body through ``POST /events`` kept both.

    RED on a revert: remove ``owner_id=data.owner_id`` and ``reviewed=data.reviewed``
    from the ``Event(...)`` in ``bulk_create_events`` and the bulk row comes back
    ``owner_id=None, reviewed=False``, so both the 201 body and the re-read below
    stop matching the single-create row.
    """
    slug = "b7a-bulk-owner"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    owner_id = (await client.get("/api/v1/users")).json()[0]["id"]

    pasted = await client.post(
        f"/api/v1/projects/{slug}/events/bulk",
        json=[
            {
                "event_type_id": event_type,
                "name": "checkout:start",
                "owner_id": owner_id,
                "reviewed": True,
            }
        ],
    )
    assert pasted.status_code == 201, pasted.text
    assert pasted.json()[0]["owner_id"] == owner_id
    assert pasted.json()[0]["reviewed"] is True

    # Stored, not just echoed: the 201 body is built from the refreshed rows,
    # and this reads them again through the detail endpoint.
    reread = await client.get(f"/api/v1/projects/{slug}/events/{pasted.json()[0]['id']}")
    assert reread.status_code == 200, reread.text
    assert reread.json()["owner_id"] == owner_id
    assert reread.json()["reviewed"] is True

    # The single door, for the same body — this is the contract that change broke.
    single = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:done",
            "owner_id": owner_id,
            "reviewed": True,
        },
    )
    assert single.status_code == 201, single.text
    assert single.json()["owner_id"] == owner_id
    assert single.json()["reviewed"] is True


@pytest.mark.asyncio
async def test_patching_metric_breakdown_columns_to_null_clears_them_instead_of_500ing(
    client: AsyncClient,
) -> None:
    """A sent ``null`` is "no breakdown columns", and the column it lands in is NOT NULL.

    ``EventUpdate.metric_breakdown_columns`` is ``list[str] | None``, so the
    published schema lets a client send ``null``; ``exclude_unset`` keeps the key
    and ``update_event`` assigned it straight through. The same transaction then
    rebuilds the search index, where ``_event_document`` does
    ``" ".join(event.metric_breakdown_columns)`` — TypeError on None, the whole
    save rolled back, 500 for what should have been a clear.

    RED on a revert: put the ``if value is None: return None`` arm back in the
    ``EventUpdate`` validator and the PATCH below assigns None to
    ``events.metric_breakdown_columns``, which is NOT NULL and enforced as such
    by SQLite too, so the autoflush inside ``_reindex_branch_documents`` raises
    IntegrityError, the save rolls back and the request answers 500 — the
    ``== 200`` assertion fails. Not the TypeError the finding was filed for: the
    same change gave ``_search_documents._event_document`` its ``or []``, so the
    join no longer blows up and the column refuses the value one layer down.
    """
    slug = "b7a-breakdown-null"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:start",
            "metric_breakdown_columns": ["country"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["metric_breakdown_columns"] == ["country"]
    event_id = created.json()["id"]

    cleared = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"metric_breakdown_columns": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["metric_breakdown_columns"] == []

    # Stored as the empty list rather than echoed: this reads the row back.
    reread = await client.get(f"/api/v1/projects/{slug}/events/{event_id}")
    assert reread.status_code == 200, reread.text
    assert reread.json()["metric_breakdown_columns"] == []

    # "Leave them alone" is the field the client does not send — that is the
    # meaning ``null`` is not competing with, and it still holds.
    restored = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"metric_breakdown_columns": ["platform"]},
    )
    assert restored.status_code == 200, restored.text
    untouched = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"description": "collected by platform"},
    )
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["metric_breakdown_columns"] == ["platform"]


@pytest.mark.asyncio
async def test_a_meta_value_too_long_for_its_unique_index_is_refused_not_500(
    client: AsyncClient,
) -> None:
    """``uq_event_meta_value_event_meta_value`` holds the STORED value in a btree.

    The previous schema widened that key from the two uuids to ``(event_id,
    meta_field_definition_id, value)``. A btree entry cannot exceed 2704 bytes,
    so once the value stopped fitting Postgres raised ProgramLimitExceeded at the
    INSERT and the event save came back 500 — for a payload that stored fine
    under the old key.

    The cap is measured on what the event will STORE, which for a link-templated
    field is only the part the template wraps: ``strip_link_template`` runs
    first, and the prefix it removes never reaches the index.

    RED on a revert, in both directions: delete the ``META_VALUE_MAX_BYTES``
    check from ``_normalize_meta_values_against`` and SQLite — which has no btree
    entry limit — stores the oversized value and answers 201, so all three
    ``== 422`` assertions fail; move the check back onto ``EventMetaValueIn`` so
    it reads the PASTED text, and the last block fails instead, because the full
    URL is 2022 characters around a 1990-byte ticket id.
    """
    slug = "b7a-meta-length"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    meta_field = await _meta_field(client, slug, "ticket")

    refused = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:start",
            "meta_values": [{"meta_field_definition_id": meta_field, "value": "a" * 2001}],
        },
    )
    assert refused.status_code == 422, refused.text

    # The index counts BYTES, so a value inside the character bound can still be
    # outside the real one: 1001 Cyrillic characters are 2002 bytes.
    refused_multibyte = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:start",
            "meta_values": [{"meta_field_definition_id": meta_field, "value": "я" * 1001}],
        },
    )
    assert refused_multibyte.status_code == 422, refused_multibyte.text
    assert "2000 bytes" in refused_multibyte.text

    assert await _event_names(client, slug) == []

    accepted = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:start",
            "meta_values": [{"meta_field_definition_id": meta_field, "value": "a" * 2000}],
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["meta_values"][0]["value"] == "a" * 2000

    # A link-templated field is the case the placement is about: the browser
    # gives you the whole address, and only the ticket id is stored.
    prefix = "https://jira.example.com/browse/"
    assert len(prefix) == 32
    linked = await _meta_field(client, slug, "jira", link_template=f"{prefix}${{value}}")

    pasted = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:linked",
            "meta_values": [{"meta_field_definition_id": linked, "value": f"{prefix}{'b' * 1990}"}],
        },
    )
    assert pasted.status_code == 201, pasted.text
    # 2022 characters in, 1990 bytes stored — and 1990 is what the index counts.
    assert pasted.json()["meta_values"][0]["value"] == "b" * 1990

    # The rule itself has not moved: a ticket id that really is too long for the
    # key is still refused, however it was pasted.
    over = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:too-long",
            "meta_values": [{"meta_field_definition_id": linked, "value": f"{prefix}{'b' * 2001}"}],
        },
    )
    assert over.status_code == 422, over.text
    assert "2000 bytes" in over.text
    # It names the field, which a payload-shaped refusal could not.
    assert "jira" in over.text


@contextlib.contextmanager
def _captured_sql() -> Iterator[list[str]]:
    """Every statement the test engine runs inside the block.

    The idiom test_project_lookup_perf.py already uses for the same question:
    how many round trips did that take, and against which tables?
    """
    statements: list[str] = []

    def _record(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    sa_event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", _record)


@pytest.mark.asyncio
async def test_template_warnings_read_three_columns_not_the_variable_graph(
    client: AsyncClient,
) -> None:
    """One SELECT, over ``variables`` alone — no contexts, no field definitions.

    ``_attach_template_warnings`` runs on every event create and every PATCH and
    wants three columns: ``name``, ``source_name`` and ``bindings``. Selecting
    the mapped ``Variable`` instead brought ``value_contexts``
    (``lazy="selectin"``) and, per context, its ``FieldDefinition``
    (``lazy="selectin"`` as well), so the branch's entire variable graph —
    including every context's JSON ``values`` list — was hydrated on the async
    request path to read three scalars.

    RED on a revert: put ``select(Variable)`` back and this one call emits three
    statements instead of one — ``variables``, then ``variable_values``, then
    ``field_definitions`` — so the count assertion and both table assertions
    fail. The seeded context row below is what guarantees the second and third
    fire; a variable with no contexts would still emit two.

    The token assertions hold either way. They are here so the cheaper query is
    not allowed to be a wrong one: each of the three columns is proved to still
    be read, by a token that would otherwise be reported as unknown.
    """
    slug = "b7a-warning-query-shape"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")
    field = await _field(client, slug, event_type, "screen")

    variable = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "screen_name", "variable_type": "string", "bindings": ["page.screen"]},
    )
    assert variable.status_code == 201, variable.text
    variable_id = uuid.UUID(variable.json()["id"])

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type,
            "name": "checkout:start",
            "field_values": [
                {
                    "field_definition_id": field,
                    # One token per column the query still has to read, and one
                    # no variable in this branch answers to.
                    "value": "${screen_name} ${page.screen} ${scan_source} ${mystery}",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    event_id = uuid.UUID(created.json()["id"])

    async with TestSessionLocal() as session:
        seeded = await session.get(Event, event_id)
        assert seeded is not None
        # ``source_name`` is backfilled by the scan, not by any API door.
        await session.execute(
            update(Variable).where(Variable.id == variable_id).values(source_name="scan_source")
        )
        # A context row, so the entity load of the old code has somewhere to
        # cascade: its second query returns this row and its third goes after
        # this row's FieldDefinition.
        session.add(
            VariableValue(
                project_id=seeded.project_id,
                branch_id=seeded.branch_id,
                variable_id=variable_id,
                event_id=event_id,
                field_definition_id=uuid.UUID(field),
                source_column="page.screen",
                observed_count=1,
                values=["home", "cart"],
            )
        )
        await session.commit()

    async with TestSessionLocal() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        # Loaded — with its selectin field and meta values — BEFORE the capture
        # opens, so what the capture holds is the warning pass and nothing else.
        with _captured_sql() as statements:
            await event_service._attach_template_warnings(session, event)
        warnings = list(event.warnings)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, f"expected a single select, got: {selects}"
    emitted = selects[0].lower()
    assert "variable_values" not in emitted, emitted
    assert "field_definitions" not in emitted, emitted

    # Cheaper and still right: the name, the source name and the binding are all
    # still known tokens, so only the one nothing answers to is reported.
    assert warnings == ["Unknown variable token: ${mystery}"]


@pytest.mark.asyncio
async def test_bulk_update_clears_owner_and_sunset_on_an_explicit_null(
    client: AsyncClient,
) -> None:
    """``owner_id: null`` is the selection-wide unassign, not "nothing provided".

    ``validate_has_update`` judged by the VALUES, so a body whose only
    instruction was ``{"owner_id": null}`` came back 422 claiming nothing had
    been given; and ``bulk_update_events`` dumped with ``exclude_none``, so the
    same null sent beside ``reviewed`` was dropped in silence — 204, review flag
    written, every owner still in place. ``MetricDefinitionBulkUpdate`` has
    answered the identical body by unassigning since it was written.

    RED on a revert: restore the value check in ``validate_has_update`` and the
    first POST below is 422 instead of 204; restore ``exclude_none`` in
    ``bulk_update_events`` and the second read-back still finds the owner and
    the sunset date the request asked to clear.
    """
    slug = "b7a-bulk-null"
    await _project(client, slug)
    event_type = await _event_type(client, slug, "track")

    users = await client.get("/api/v1/users")
    assert users.status_code == 200, users.text
    owner_id = users.json()[0]["id"]

    event_ids = []
    for name in ("checkout:start", "checkout:done"):
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": event_type,
                "name": name,
                "owner_id": owner_id,
                "sunset_at": "2030-01-01T00:00:00Z",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["owner_id"] == owner_id
        event_ids.append(created.json()["id"])

    # A null ON ITS OWN is an instruction, and the only one this body carries.
    alone = await client.post(
        f"/api/v1/projects/{slug}/events/bulk-update",
        json={"event_ids": event_ids, "owner_id": None},
    )
    assert alone.status_code == 204, alone.text

    unassigned = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    assert [item["owner_id"] for item in unassigned] == [None, None]
    # Sent fields only: ``sunset_at`` was not in the body and did not move.
    assert all(item["sunset_at"] is not None for item in unassigned)

    reassigned = await client.post(
        f"/api/v1/projects/{slug}/events/bulk-update",
        json={"event_ids": event_ids, "owner_id": owner_id},
    )
    assert reassigned.status_code == 204, reassigned.text

    # And a null BESIDE another field is still the clear, not a dropped key.
    beside = await client.post(
        f"/api/v1/projects/{slug}/events/bulk-update",
        json={"event_ids": event_ids, "reviewed": True, "owner_id": None, "sunset_at": None},
    )
    assert beside.status_code == 204, beside.text

    cleared = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    assert [item["owner_id"] for item in cleared] == [None, None]
    assert [item["sunset_at"] for item in cleared] == [None, None]
    assert all(item["reviewed"] is True for item in cleared)

    # The two NOT NULL columns keep refusing a null, ahead of the database
    # rather than as an IntegrityError from inside ``values()``.
    for not_null_field in ("status", "reviewed"):
        refused = await client.post(
            f"/api/v1/projects/{slug}/events/bulk-update",
            json={"event_ids": event_ids, not_null_field: None},
        )
        assert refused.status_code == 422, refused.text
        assert "cannot be null" in refused.text

    # A body that instructs nothing at all is still refused — the check moved
    # from the values to the field set, it did not go away.
    empty = await client.post(
        f"/api/v1/projects/{slug}/events/bulk-update",
        json={"event_ids": event_ids},
    )
    assert empty.status_code == 422, empty.text
    assert "must be provided" in empty.text
