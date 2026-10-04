import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_explicit_null_is_ignored_on_anomaly_settings_patch(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/projects",
        json={"name": "Null anomaly", "slug": "null-anomaly", "description": ""},
    )
    assert created.status_code == 201

    response = await client.patch(
        "/api/v1/projects/null-anomaly/anomaly-settings",
        json={"sigma_threshold": None, "recent_signal_window_hours": None, "detect_events": False},
    )
    assert response.status_code == 200
    assert response.json()["sigma_threshold"] == 4.0
    assert response.json()["recent_signal_window_hours"] == 24
    assert response.json()["detect_events"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "invalid"),
    [
        ({}, {"min_history_buckets": 15}),
        (
            {"baseline_window_buckets": 21, "min_history_buckets": 20},
            {"baseline_window_buckets": 19},
        ),
    ],
)
async def test_anomaly_history_cannot_exceed_baseline_window(
    client: AsyncClient, initial: dict[str, int], invalid: dict[str, int]
) -> None:
    created = await client.post(
        "/api/v1/projects",
        json={"name": "History bounds", "slug": "history-bounds", "description": ""},
    )
    assert created.status_code == 201
    if initial:
        setup = await client.patch("/api/v1/projects/history-bounds/anomaly-settings", json=initial)
        assert setup.status_code == 200

    response = await client.patch("/api/v1/projects/history-bounds/anomaly-settings", json=invalid)
    assert response.status_code == 422
    assert "min_history_buckets" in response.json()["detail"]
    assert "baseline_window_buckets" in response.json()["detail"]

    stored = await client.get("/api/v1/projects/history-bounds/anomaly-settings")
    assert stored.status_code == 200
    assert stored.json()["baseline_window_buckets"] == initial.get("baseline_window_buckets", 14)
    assert stored.json()["min_history_buckets"] == initial.get("min_history_buckets", 7)


@pytest.mark.asyncio
async def test_history_may_equal_baseline_window(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/projects",
        json={"name": "History equal", "slug": "history-equal", "description": ""},
    )
    assert created.status_code == 201
    response = await client.patch(
        "/api/v1/projects/history-equal/anomaly-settings",
        json={"min_history_buckets": 14},
    )
    assert response.status_code == 200
    assert response.json()["min_history_buckets"] == 14
