import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_tracker_type_rejects_unimplemented_backend(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/projects",
        json={"name": "Tracker Contract", "slug": "tracker-contract"},
    )
    assert created.status_code == 201, created.text

    unsupported = await client.patch(
        "/api/v1/projects/tracker-contract/tracker-config",
        json={"tracker_type": "github"},
    )
    assert unsupported.status_code == 422, unsupported.text

    unchanged = await client.get("/api/v1/projects/tracker-contract/tracker-config")
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["tracker_type"] == "jira"
    assert unchanged.json()["id"] is None

    supported = await client.patch(
        "/api/v1/projects/tracker-contract/tracker-config",
        json={"tracker_type": "jira"},
    )
    assert supported.status_code == 200, supported.text
    assert supported.json()["tracker_type"] == "jira"
