"""Regressions for the data-source half of batch 11."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import inspect

from tripl.models.data_source import DataSource
from tripl.services import datasource_service


async def _create_source(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/api/v1/data-sources",
        json={
            "name": name,
            "db_type": "clickhouse",
            "host": "warehouse.example",
            "database_name": "analytics",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_rename_to_existing_source_returns_conflict(client: AsyncClient) -> None:
    first_id = await _create_source(client, "First warehouse")
    second_id = await _create_source(client, "Second warehouse")

    conflict = await client.patch(
        f"/api/v1/data-sources/{second_id}", json={"name": "First warehouse"}
    )
    assert conflict.status_code == 409, conflict.text

    unchanged = await client.get(f"/api/v1/data-sources/{second_id}")
    assert unchanged.json()["name"] == "Second warehouse"
    same_name = await client.patch(
        f"/api/v1/data-sources/{first_id}", json={"name": "First warehouse"}
    )
    assert same_name.status_code == 200, same_name.text


@pytest.mark.parametrize("field", ["name", "host", "port", "database_name", "username"])
async def test_patch_rejects_explicit_null_for_required_field(
    client: AsyncClient, field: str
) -> None:
    ds_id = await _create_source(client, f"Required {field}")
    response = await client.patch(f"/api/v1/data-sources/{ds_id}", json={field: None})
    assert response.status_code == 422, response.text


def test_libpq_auth_failure_is_classified_as_authentication() -> None:
    error = RuntimeError(
        'connection failed: connection to server at "db" (10.0.0.5), port 5432 failed: '
        'FATAL: password authentication failed for user "tripl"'
    )
    assert datasource_service._friendly_test_error(error) == (
        "Connection test failed: authentication was rejected — check the credentials."
    )


def test_datasource_does_not_eager_load_every_scan_and_job() -> None:
    assert inspect(DataSource).relationships.scan_configs.lazy == "select"


async def test_failed_reindex_rolls_back_before_next_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    attempted: list[uuid.UUID] = []

    class Session:
        rollbacks = 0

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = Session()

    async def resolve(_session: Session, project_id: uuid.UUID, _branch: None) -> uuid.UUID:
        if project_id == second:
            assert session.rollbacks == 1
        return project_id

    async def reindex(_session: Session, *, project_id: uuid.UUID, branch_id: uuid.UUID) -> None:
        assert branch_id == project_id
        attempted.append(project_id)
        if project_id == first:
            raise RuntimeError("database transaction aborted")

    monkeypatch.setattr(datasource_service, "resolve_branch_id", resolve)
    monkeypatch.setattr(datasource_service, "reindex_project_branch", reindex)

    await datasource_service._refresh_main_search_indexes(session, [first, second])  # type: ignore[arg-type]
    assert attempted == [first, second]
    assert session.rollbacks == 1
