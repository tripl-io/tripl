"""The docs catalog migration ``c3f5a7b9d1e2`` (F22, GH #299).

The suite builds its schema from the models, so this runs the revision itself on
SQLite and checks it creates what the models declare: the same columns, indexes
and constraints, the per-scope case-insensitive uniqueness, and a clean
downgrade.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError

from tripl.models import Base
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.tests._default_org import seed_default_organization
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.test_alembic_revisions import _load_migration

MIGRATION = "c3f5a7b9d1e2_docs_catalog.py"
DOC_TABLES = ("doc_links", "doc_revisions", "doc_files")
# Added on top by the sharing revision (c4e6a8b0d2f5, F24): dropped before the
# F22 tables (they reference ``doc_files``) and left out of the comparison.
SHARING_TABLES = ("doc_folder_shares", "doc_folder_settings", "doc_shares")
SHARING_COLUMNS = {"doc_files": {"visibility", "visibility_inherited"}}
SHARING_CHECKS = {"doc_files": {"ck_doc_files_visibility"}}


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load_migration("docs_catalog_migration", MIGRATION)


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        seed_default_organization(connection)
        for table in (*SHARING_TABLES, *DOC_TABLES):
            Base.metadata.tables[table].drop(connection)
    try:
        yield engine
    finally:
        engine.dispose()


def _run(engine: Engine, step: object) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            step()  # type: ignore[operator]


def test_revision_chain(migration: ModuleType) -> None:
    assert migration.revision == "c3f5a7b9d1e2"
    assert migration.down_revision == "b8d0f2a4c6e8"


def test_upgrade_matches_the_models(engine: Engine, migration: ModuleType) -> None:
    _run(engine, migration.upgrade)
    inspector = sa.inspect(engine)

    for name in DOC_TABLES:
        model = Base.metadata.tables[name]
        columns = {column["name"]: column for column in inspector.get_columns(name)}
        later = SHARING_COLUMNS.get(name, set())
        assert set(columns) == set(model.columns.keys()) - later, name
        for column in model.columns:
            if column.name in later:
                continue
            assert columns[column.name]["nullable"] == column.nullable, (name, column.name)
        migrated_indexes = {
            index["name"]: bool(index["unique"]) for index in inspector.get_indexes(name)
        }
        model_indexes = {index.name: bool(index.unique) for index in model.indexes}
        assert migrated_indexes == model_indexes, name
        migrated_checks = {check["name"] for check in inspector.get_check_constraints(name)}
        model_checks = {
            constraint.name
            for constraint in model.constraints
            if isinstance(constraint, sa.CheckConstraint)
        } - SHARING_CHECKS.get(name, set())
        assert migrated_checks == model_checks, name


def _doc_row(**values: object) -> dict[str, object]:
    now = datetime.now(UTC)
    row: dict[str, object] = {
        "id": uuid.uuid4(),
        "project_id": None,
        "organization_id": None,
        "path": "a.md",
        "path_key": "a.md",
        "content": "x",
        "content_sha256": "0" * 64,
        "size_bytes": 1,
        "title": "a",
        "description": "",
        "tags": [],
        "audience": "both",
        "revision": 1,
        "created_at": now,
        "updated_at": now,
    }
    row.update(values)
    return row


def test_scopes_and_uniqueness_are_enforced(engine: Engine, migration: ModuleType) -> None:
    _run(engine, migration.upgrade)
    project_id = uuid.uuid4()
    # The model's column types, limited to the columns this revision creates:
    # later revisions add more (F24 visibility), and the ORM defaults would
    # write them into a table that does not have them yet.
    model = Base.metadata.tables["doc_files"]
    present = {column["name"] for column in sa.inspect(engine).get_columns("doc_files")}
    docs = sa.table("doc_files", *(sa.column(c.name, c.type) for c in model.c if c.name in present))
    with engine.begin() as connection:
        connection.execute(
            sa.insert(Base.metadata.tables["projects"]).values(
                id=project_id, name="p", slug="p", organization_id=DEFAULT_ORG_ID
            )
        )
        connection.execute(sa.insert(docs).values(**_doc_row(project_id=project_id)))
        # The same path in the other scope is a different note.
        connection.execute(sa.insert(docs).values(**_doc_row(organization_id=DEFAULT_ORG_ID)))

    for bad in (
        _doc_row(project_id=project_id, path="A.md"),  # same path_key, same scope
        _doc_row(),  # neither scope
        _doc_row(project_id=project_id, organization_id=DEFAULT_ORG_ID, path_key="b.md"),
        _doc_row(project_id=project_id, path_key="c.md", audience="robots"),
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(sa.insert(docs).values(**bad))


def test_downgrade_removes_everything(engine: Engine, migration: ModuleType) -> None:
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    tables = set(sa.inspect(engine).get_table_names())
    assert tables.isdisjoint(DOC_TABLES)
    assert "projects" in tables
