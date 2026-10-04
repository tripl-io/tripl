"""Guards for the shape of a project lookup.

``Project.event_types`` / ``meta_field_definitions`` / ``relations`` /
``variables`` used to be declared ``lazy="selectin"``, so resolving a slug to a
project pulled the project's ENTIRE tracking plan into the session — four extra
round trips and, on the largest production project, ~1300 Variable rows — even
for handlers that only wanted ``project.id``. That is what made three
constant-size settings endpoints 4-7x slower on big projects than on small ones.

Two things have to stay true for that fix to hold:

* a slug lookup emits ONE select, and
* deleting a project still removes its plan (the collections exist purely for
  that ORM-level cascade, so making them lazy must not strand rows).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select

from tripl.models.event_type import EventType
from tripl.models.variable import Variable
from tripl.services.project_lookup import resolve_project
from tripl.tests.conftest import TestSessionLocal, engine


@contextlib.contextmanager
def captured_sql() -> Iterator[list[str]]:
    """Collect every statement the test engine executes inside the block."""
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

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def _seed_project_with_plan(client: AsyncClient, slug: str) -> str:
    """A project with enough plan attached that an eager load would show up."""
    project = await client.post("/api/v1/projects", json={"name": "Lookup Perf", "slug": slug})
    assert project.status_code == 201

    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page", "display_name": "Page"},
    )
    assert event_type.status_code == 201

    variable = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "screen_name", "variable_type": "string"},
    )
    assert variable.status_code == 201

    meta_field = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "app_version", "display_name": "App Version", "field_type": "string"},
    )
    assert meta_field.status_code == 201

    return str(project.json()["id"])


@pytest.mark.asyncio
async def test_project_slug_lookup_does_not_hydrate_the_plan(client: AsyncClient) -> None:
    slug = "lookup-perf"
    await _seed_project_with_plan(client, slug)

    async with TestSessionLocal() as session:
        with captured_sql() as statements:
            project = await resolve_project(session, slug)

    assert project.slug == slug

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, f"expected a single select, got: {selects}"

    plan_tables = ("event_types", "variables", "meta_field_definitions", "event_type_relations")
    emitted = " ".join(selects).lower()
    assert not any(table in emitted for table in plan_tables), emitted


@pytest.mark.asyncio
async def test_deleting_a_project_still_removes_its_plan(client: AsyncClient) -> None:
    slug = "lookup-perf-delete"
    project_id = uuid.UUID(await _seed_project_with_plan(client, slug))

    resp = await client.delete(f"/api/v1/projects/{slug}")
    assert resp.status_code == 204

    async with TestSessionLocal() as session:
        event_types = (
            await session.scalars(select(EventType).where(EventType.project_id == project_id))
        ).all()
        variables = (
            await session.scalars(select(Variable).where(Variable.project_id == project_id))
        ).all()

    assert event_types == []
    assert variables == []
