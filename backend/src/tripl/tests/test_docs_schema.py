"""Doc catalog constraints that must hold in the final schema."""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tripl.models import Base
from tripl.models.doc_file import DocFile
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.project import Project
from tripl.tests._default_org import seed_default_organization
from tripl.tests._sqlite import enable_sqlite_foreign_keys


def _doc(**overrides: object) -> dict[str, object]:
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
    }
    return row | overrides


def test_doc_scope_and_path_uniqueness_are_enforced() -> None:
    engine = sa.create_engine("sqlite://")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    project_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            seed_default_organization(connection)
            connection.execute(
                sa.insert(Project.__table__).values(
                    id=project_id, name="p", slug="p", organization_id=DEFAULT_ORG_ID
                )
            )
            connection.execute(sa.insert(DocFile.__table__).values(_doc(project_id=project_id)))
            connection.execute(
                sa.insert(DocFile.__table__).values(_doc(organization_id=DEFAULT_ORG_ID))
            )

        for invalid in (
            _doc(project_id=project_id, path="A.md"),
            _doc(),
            _doc(project_id=project_id, organization_id=DEFAULT_ORG_ID, path_key="b.md"),
            _doc(project_id=project_id, path_key="c.md", audience="robots"),
        ):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(sa.insert(DocFile.__table__).values(invalid))
    finally:
        engine.dispose()
