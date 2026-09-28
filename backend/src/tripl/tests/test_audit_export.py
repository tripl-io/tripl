"""Exporting an organization's audit log (F20, GH #273).

``GET /orgs/{org}/audit/export`` takes the audit feed's gate (owner or admin,
browser session; any API key 403, a stranger 404), streams CSV or NDJSON in
keyset pages of 1000, escapes spreadsheet formulas, caps the range at 366
days, and holds the organization's rows and its projects' rows — never a
platform-scope row, never another organization's.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, null, select

from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.services import audit_export_service
from tripl.services.audit_rows import COLUMNS
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal, engine

PASSWORD = "Password123!"
API = "/api/v1"
ACME = "acme"
GLOBEX = "globex"
EXPORT_URL = f"{API}/orgs/{ACME}/audit/export"
# Far in the past, so the rows the API itself files while seeding (now) fall
# outside every range these tests export.
BASE = datetime(2020, 3, 1, tzinfo=UTC)
RANGE = {"from": "2020-03-01T00:00:00Z", "to": "2020-03-02T00:00:00Z"}


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@dataclass
class World:
    """``root`` owns acme and globex; ``bob`` is acme's admin, ``mia`` a member.

    ``carol`` belongs to neither. ``shop`` is acme's project.
    """

    clients: dict[str, AsyncClient]
    ids: dict[str, uuid.UUID]
    acme_id: uuid.UUID
    globex_id: uuid.UUID
    shop_id: uuid.UUID

    def __getitem__(self, name: str) -> AsyncClient:
        return self.clients[name]


@pytest.fixture
async def world() -> AsyncIterator[World]:
    clients: dict[str, AsyncClient] = {}
    ids: dict[str, uuid.UUID] = {}
    for name in ("root", "bob", "mia", "carol"):
        client = _new_client()
        resp = await client.post(
            f"{API}/auth/register",
            json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
        )
        assert resp.status_code == 201, resp.text
        clients[name] = client
        ids[name] = uuid.UUID(resp.json()["id"])
    try:
        root = clients["root"]
        acme = await root.post(f"{API}/orgs", json={"slug": ACME, "name": "Acme"})
        assert acme.status_code == 201, acme.text
        globex = await root.post(f"{API}/orgs", json={"slug": GLOBEX, "name": "Globex"})
        assert globex.status_code == 201, globex.text
        acme_id = uuid.UUID(acme.json()["id"])
        async with TestSessionLocal() as session:
            await add_org_member(session, ids["bob"], "admin", org_id=acme_id)
            await add_org_member(session, ids["mia"], "member", org_id=acme_id)
        shop = await root.post(f"{API}/orgs/{ACME}/projects", json={"name": "Shop", "slug": "shop"})
        assert shop.status_code == 201, shop.text
        yield World(
            clients=clients,
            ids=ids,
            acme_id=acme_id,
            globex_id=uuid.UUID(globex.json()["id"]),
            shop_id=uuid.UUID(shop.json()["id"]),
        )
    finally:
        for client in clients.values():
            await client.aclose()


async def _add_rows(rows: list[dict[str, Any]]) -> None:
    async with TestSessionLocal() as session:
        for index, values in enumerate(rows):
            organization_id = values.pop("organization_id")
            entry = AuditLog(
                user_email=values.pop("user_email", "root@example.com"),
                action=values.pop("action", "event.update"),
                target_type=values.pop("target_type", "event"),
                target_id=values.pop("target_id", uuid.uuid4()),
                target_name=values.pop("target_name", f"row_{index}"),
                payload=values.pop("payload", {"n": index}),
                created_at=values.pop("created_at", BASE + timedelta(minutes=index)),
                **values,
            )
            # ``None`` would let the column default (the default org) fill it.
            entry.organization_id = null() if organization_id is None else organization_id
            session.add(entry)
        await session.commit()


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ── the gate ─────────────────────────────────────────────────────────────────


async def _mint_key(client: AsyncClient, scope: str) -> str:
    resp = await client.post(
        f"{API}/orgs/{ACME}/me/api-keys", json={"name": f"export-{scope}", "scope": scope}
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"])


async def test_owners_and_admins_export_members_and_keys_do_not(world: World) -> None:
    assert (await world["root"].get(EXPORT_URL, params=RANGE)).status_code == 200
    assert (await world["bob"].get(EXPORT_URL, params=RANGE)).status_code == 200
    assert (await world["mia"].get(EXPORT_URL, params=RANGE)).status_code == 403
    # Another organization does not exist for a stranger.
    assert (await world["carol"].get(EXPORT_URL, params=RANGE)).status_code == 404

    # The feed's rule: an API key is refused whatever its scope, the owner's too.
    for scope in ("read", "write"):
        token = await _mint_key(world["root"], scope)
        async with _new_client() as bearer:
            resp = await bearer.get(
                EXPORT_URL, params=RANGE, headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 403, f"{scope}: {resp.text}"


# ── the file ─────────────────────────────────────────────────────────────────


async def test_csv_quotes_every_cell_and_defuses_formulas(world: World) -> None:
    hostile = ["=cmd|' /C calc'!A0", "+1", "-1", "@x", "\tx", "\rx", "plain"]
    await _add_rows(
        [
            {"organization_id": world.acme_id, "target_name": name, "user_email": "=evil@x"}
            for name in hostile
        ]
    )

    resp = await world["root"].get(EXPORT_URL, params=RANGE)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == (
        'attachment; filename="audit-acme-20200301-20200302.csv"'
    )
    text = resp.text
    header, *rows = _csv_rows(text)
    assert header == list(COLUMNS)
    # QUOTE_ALL: the header line is quoted field by field.
    assert text.startswith('"id","created_at","org_slug"')
    names = [row[COLUMNS.index("target_name")] for row in rows]
    assert names == ["'=cmd|' /C calc'!A0", "'+1", "'-1", "'@x", "'\tx", "'\rx", "plain"]
    assert {row[COLUMNS.index("user_email")] for row in rows} == {"'=evil@x"}
    # The payload is one JSON string cell.
    payload = json.loads(rows[0][COLUMNS.index("payload")])
    assert payload == {"n": 0}
    assert {row[COLUMNS.index("org_slug")] for row in rows} == {ACME}


def test_escape_cell_covers_the_owasp_prefixes() -> None:
    for value in ("=1", "+1", "-1", "@1", "\t1", "\r1"):
        assert audit_export_service.escape_cell(value) == "'" + value
    for value in ("1", "a=b", "", " =1"):
        assert audit_export_service.escape_cell(value) == value


async def test_ndjson_is_one_object_per_row_oldest_first(world: World) -> None:
    await _add_rows(
        [
            {
                "organization_id": world.acme_id,
                "created_at": BASE + timedelta(hours=2),
                "target_name": "second",
                "payload": {"k": [1, 2]},
            },
            {
                "organization_id": world.acme_id,
                "created_at": BASE + timedelta(hours=1),
                "target_name": "first",
                "target_id": None,
            },
        ]
    )

    resp = await world["root"].get(EXPORT_URL, params={**RANGE, "format": "json"})

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["content-disposition"].endswith('.ndjson"')
    lines = resp.text.splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["target_name"] for r in records] == ["first", "second"]
    assert list(records[0]) == list(COLUMNS)
    assert records[0]["target_id"] is None
    assert records[1]["payload"] == {"k": [1, 2]}
    assert records[0]["created_at"] == "2020-03-01T01:00:00Z"
    assert records[0]["org_slug"] == ACME


# ── the range ────────────────────────────────────────────────────────────────


async def test_the_range_must_be_forward_and_at_most_366_days(world: World) -> None:
    root = world["root"]
    reversed_range = {"from": "2020-03-02T00:00:00Z", "to": "2020-03-01T00:00:00Z"}
    assert (await root.get(EXPORT_URL, params=reversed_range)).status_code == 422
    empty = {"from": "2020-03-01T00:00:00Z", "to": "2020-03-01T00:00:00Z"}
    assert (await root.get(EXPORT_URL, params=empty)).status_code == 422
    too_wide = {"from": "2020-01-01T00:00:00Z", "to": "2021-01-02T00:00:01Z"}
    assert (await root.get(EXPORT_URL, params=too_wide)).status_code == 422
    widest = {"from": "2020-01-01T00:00:00Z", "to": "2021-01-01T00:00:00Z"}
    assert (await root.get(EXPORT_URL, params=widest)).status_code == 200
    assert (await root.get(EXPORT_URL, params={"from": "yesterday"})).status_code == 422
    assert (await root.get(EXPORT_URL, params={"format": "xml"})).status_code == 422
    # No range at all: the last 30 days.
    assert (await root.get(EXPORT_URL)).status_code == 200


def test_resolve_range_defaults_and_reads_naive_as_utc() -> None:
    now = datetime(2020, 6, 1, 12, tzinfo=UTC)
    span = audit_export_service.resolve_range(None, None, now=now)
    assert span.end == now
    assert span.start == now - timedelta(days=30)
    naive = audit_export_service.resolve_range(datetime(2020, 1, 1), datetime(2020, 1, 2), now=now)
    assert naive.start == datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(audit_export_service.ExportRangeError):
        audit_export_service.resolve_range(now, now - timedelta(seconds=1), now=now)


# ── what is in it ────────────────────────────────────────────────────────────


async def test_the_org_and_its_projects_only_never_platform_or_other_orgs(world: World) -> None:
    await _add_rows(
        [
            {"organization_id": world.acme_id, "target_name": "acme_row"},
            {
                "organization_id": world.acme_id,
                "project_id": world.shop_id,
                "project_slug": "shop",
                "target_name": "shop_row",
            },
            # A row of acme's project filed in another organization (a legacy
            # row): the project is acme's, so is the row.
            {
                "organization_id": DEFAULT_ORG_ID,
                "project_id": world.shop_id,
                "project_slug": "shop",
                "target_name": "shop_legacy_row",
            },
            {"organization_id": None, "target_name": "platform_row"},
            {"organization_id": world.globex_id, "target_name": "globex_row"},
            {"organization_id": DEFAULT_ORG_ID, "target_name": "default_row"},
            # Outside the range.
            {
                "organization_id": world.acme_id,
                "target_name": "late_row",
                "created_at": BASE + timedelta(days=1),
            },
        ]
    )

    resp = await world["root"].get(EXPORT_URL, params={**RANGE, "format": "json"})

    assert resp.status_code == 200, resp.text
    names = {json.loads(line)["target_name"] for line in resp.text.splitlines()}
    assert names == {"acme_row", "shop_row", "shop_legacy_row"}

    filtered = await world["root"].get(
        EXPORT_URL, params={**RANGE, "format": "json", "action": "nothing.matches"}
    )
    assert filtered.status_code == 200
    assert filtered.text == ""


async def test_the_export_is_audited_when_it_starts(world: World) -> None:
    resp = await world["bob"].get(EXPORT_URL, params={**RANGE, "format": "json"})
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "org.audit_export"))
    assert row is not None
    assert row.organization_id == world.acme_id
    assert row.user_email == "bob@example.com"
    assert row.payload["format"] == "json"
    assert row.payload["from"].startswith("2020-03-01T00:00:00")
    assert row.payload["to"].startswith("2020-03-02T00:00:00")


# ── streaming ────────────────────────────────────────────────────────────────


async def test_more_than_a_page_is_read_in_keyset_pages(world: World) -> None:
    total = audit_export_service.CHUNK_SIZE + 5
    await _add_rows(
        [
            {
                "organization_id": world.acme_id,
                "created_at": BASE + timedelta(seconds=index),
                "target_name": f"bulk_{index:05d}",
            }
            for index in range(total)
        ]
    )

    statements: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if "FROM audit_log" in statement and "LIMIT" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        resp = await world["root"].get(EXPORT_URL, params=RANGE)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)

    assert resp.status_code == 200, resp.text
    _header, *rows = _csv_rows(resp.text)
    assert len(rows) == total
    names = [row[COLUMNS.index("target_name")] for row in rows]
    assert names == sorted(names)
    # Two pages, the second one keyset-bounded: never all rows in one read.
    assert len(statements) == 2, statements
    bound = "audit_log.created_at >"
    assert statements[1].count(bound) == statements[0].count(bound) + 1

    # The generator itself yields one page at a time.
    span = audit_export_service.ExportRange(start=BASE, end=BASE + timedelta(days=1))
    async with TestSessionLocal() as session:
        sizes = [
            len(chunk)
            async for chunk in audit_export_service.iter_chunks(
                session, org_id=world.acme_id, span=span
            )
        ]
    assert sizes == [audit_export_service.CHUNK_SIZE, 5]


async def test_ties_on_created_at_are_paged_by_id(world: World) -> None:
    """Rows of one transaction share ``created_at``; the id breaks the tie."""
    same = BASE + timedelta(hours=3)
    await _add_rows([{"organization_id": world.acme_id, "created_at": same} for _ in range(7)])
    span = audit_export_service.ExportRange(start=BASE, end=BASE + timedelta(days=1))
    async with TestSessionLocal() as session:
        chunks = [
            [row.id for row in chunk]
            async for chunk in audit_export_service.iter_chunks(
                session, org_id=world.acme_id, span=span, chunk_size=3
            )
        ]
    assert [len(chunk) for chunk in chunks] == [3, 3, 1]
    flat = [row_id for chunk in chunks for row_id in chunk]
    assert len(set(flat)) == 7


# ── repairs ──────────────────────────────────────────────────────────────────


async def test_each_page_is_its_own_transaction(world: World) -> None:
    """No connection sits "idle in transaction" while the consumer holds a page."""
    await _add_rows(
        [
            {"organization_id": world.acme_id, "created_at": BASE + timedelta(seconds=index)}
            for index in range(7)
        ]
    )
    span = audit_export_service.ExportRange(start=BASE, end=BASE + timedelta(days=1))
    open_while_yielded: list[bool] = []
    async with TestSessionLocal() as session:
        async for _chunk in audit_export_service.iter_chunks(
            session, org_id=world.acme_id, span=span, chunk_size=3
        ):
            open_while_yielded.append(session.in_transaction())
    assert open_while_yielded == [False, False, False]


async def test_to_is_exclusive_and_the_day_after_includes_the_last_day(world: World) -> None:
    """The settings page sends the day after the last day picked; the API is ``[from, to)``."""
    last_day = datetime(2020, 3, 5, tzinfo=UTC)
    await _add_rows(
        [
            {
                "organization_id": world.acme_id,
                "created_at": last_day + timedelta(hours=23, minutes=59),
                "target_name": "late_on_the_last_day",
            },
            {
                "organization_id": world.acme_id,
                "created_at": last_day + timedelta(days=1),
                "target_name": "midnight_after",
            },
        ]
    )
    root = world["root"]

    exclusive = await root.get(EXPORT_URL, params={"from": "2020-03-01", "to": "2020-03-05"})
    assert exclusive.status_code == 200, exclusive.text
    assert "late_on_the_last_day" not in exclusive.text

    inclusive = await root.get(EXPORT_URL, params={"from": "2020-03-01", "to": "2020-03-06"})
    assert inclusive.status_code == 200, inclusive.text
    assert "late_on_the_last_day" in inclusive.text
    assert "midnight_after" not in inclusive.text


async def test_exports_are_rate_limited(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.config import settings
    from tripl.middleware.rate_limit import AUDIT_EXPORT_RATE_LIMIT_PER_MINUTE

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    for _ in range(AUDIT_EXPORT_RATE_LIMIT_PER_MINUTE):
        assert (await world["root"].get(EXPORT_URL, params=RANGE)).status_code == 200
    limited = await world["root"].get(EXPORT_URL, params=RANGE)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
