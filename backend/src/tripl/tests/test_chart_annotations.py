"""CRUD + scope filtering for chart annotations."""

import pytest
from httpx import AsyncClient


async def _setup_project(client: AsyncClient, slug: str = "ann-proj") -> str:
    await client.post("/api/v1/projects", json={"name": "P", "slug": slug})
    return slug


@pytest.mark.asyncio
async def test_create_and_list_project_wide_annotation(client: AsyncClient) -> None:
    slug = await _setup_project(client)

    resp = await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={
            "bucket": "2026-05-01T10:00:00Z",
            "label": "v1.4 deploy",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["label"] == "v1.4 deploy"
    assert body["scope_type"] is None
    assert body["color"] == "#ef4444"

    listed = await client.get(f"/api/v1/projects/{slug}/annotations")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


@pytest.mark.asyncio
async def test_scope_filter_includes_project_wide_and_matching_scope(
    client: AsyncClient,
) -> None:
    slug = await _setup_project(client, slug="ann-scope")

    await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={"bucket": "2026-05-01T10:00:00Z", "label": "global"},
    )
    await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={
            "bucket": "2026-05-02T10:00:00Z",
            "label": "scoped",
            "scope_type": "event_type",
            "scope_ref": "00000000-0000-0000-0000-000000000001",
        },
    )
    await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={
            "bucket": "2026-05-03T10:00:00Z",
            "label": "other-scope",
            "scope_type": "event_type",
            "scope_ref": "00000000-0000-0000-0000-000000000002",
        },
    )

    resp = await client.get(
        f"/api/v1/projects/{slug}/annotations",
        params={
            "scope_type": "event_type",
            "scope_ref": "00000000-0000-0000-0000-000000000001",
        },
    )
    assert resp.status_code == 200
    labels = sorted(item["label"] for item in resp.json())
    assert labels == ["global", "scoped"]


@pytest.mark.asyncio
async def test_create_and_list_metric_scope_annotation(client: AsyncClient) -> None:
    slug = await _setup_project(client, slug="ann-metric")
    metric_id = "00000000-0000-0000-0000-00000000000a"

    resp = await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={
            "bucket": "2026-05-01T10:00:00Z",
            "label": "v2.0 release",
            "scope_type": "metric",
            "scope_ref": metric_id,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["scope_type"] == "metric"
    assert body["scope_ref"] == metric_id

    listed = await client.get(
        f"/api/v1/projects/{slug}/annotations",
        params={"scope_type": "metric", "scope_ref": metric_id},
    )
    assert listed.status_code == 200
    assert [item["label"] for item in listed.json()] == ["v2.0 release"]


@pytest.mark.asyncio
async def test_metric_scope_filter_excludes_other_refs(client: AsyncClient) -> None:
    slug = await _setup_project(client, slug="ann-metric-filter")

    await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={"bucket": "2026-05-01T10:00:00Z", "label": "global"},
    )
    await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={
            "bucket": "2026-05-02T10:00:00Z",
            "label": "mine",
            "scope_type": "metric",
            "scope_ref": "00000000-0000-0000-0000-00000000000a",
        },
    )
    await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={
            "bucket": "2026-05-03T10:00:00Z",
            "label": "other-metric",
            "scope_type": "metric",
            "scope_ref": "00000000-0000-0000-0000-00000000000b",
        },
    )

    resp = await client.get(
        f"/api/v1/projects/{slug}/annotations",
        params={
            "scope_type": "metric",
            "scope_ref": "00000000-0000-0000-0000-00000000000a",
        },
    )
    assert resp.status_code == 200
    labels = sorted(item["label"] for item in resp.json())
    assert labels == ["global", "mine"]


@pytest.mark.asyncio
async def test_partial_scope_is_rejected(client: AsyncClient) -> None:
    slug = await _setup_project(client, slug="ann-partial")

    resp = await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={
            "bucket": "2026-05-01T10:00:00Z",
            "label": "bad",
            "scope_type": "event_type",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_annotation_removes_it(client: AsyncClient) -> None:
    slug = await _setup_project(client, slug="ann-del")

    create = await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={"bucket": "2026-05-01T10:00:00Z", "label": "to delete"},
    )
    annotation_id = create.json()["id"]

    deleted = await client.delete(f"/api/v1/projects/{slug}/annotations/{annotation_id}")
    assert deleted.status_code == 204

    listed = await client.get(f"/api/v1/projects/{slug}/annotations")
    assert listed.json() == []


@pytest.mark.asyncio
async def test_list_rejects_unknown_scope_type(client: AsyncClient) -> None:
    """The list filter binds against the native chart_annotation_scope_type
    column, so while it was declared ``str`` a typo reached the driver as a 500.
    Garbage now 422s and every member that worked before still 200s."""
    slug = await _setup_project(client, slug="ann-enum")

    rejected = await client.get(
        f"/api/v1/projects/{slug}/annotations",
        params={"scope_type": "BOGUS", "scope_ref": "00000000-0000-0000-0000-000000000001"},
    )
    assert rejected.status_code == 422, rejected.text

    # MetricScopeType members that annotations never carry are rejected too —
    # this endpoint's enum is deliberately the narrower annotation one.
    metric_only = await client.get(
        f"/api/v1/projects/{slug}/annotations",
        params={"scope_type": "distribution", "scope_ref": "00000000-0000-0000-0000-000000000001"},
    )
    assert metric_only.status_code == 422, metric_only.text

    for scope_type in ("project_total", "event_type", "event", "metric"):
        accepted = await client.get(
            f"/api/v1/projects/{slug}/annotations",
            params={"scope_type": scope_type, "scope_ref": "00000000-0000-0000-0000-000000000001"},
        )
        assert accepted.status_code == 200, f"{scope_type} was rejected: {accepted.text}"


@pytest.mark.asyncio
async def test_the_list_names_each_scoped_series(client: AsyncClient) -> None:
    """The project-wide Annotations page shows which chart a marker is on."""
    slug = await _setup_project(client, slug="ann-names")
    event_type = (
        await client.post(
            f"/api/v1/projects/{slug}/event-types",
            json={"name": "screen_view", "display_name": "Screen View"},
        )
    ).json()
    await client.post(
        f"/api/v1/projects/{slug}/annotations",
        json={"bucket": "2026-05-01T10:00:00Z", "label": "global"},
    )
    for label, ref in (
        ("named", event_type["id"]),
        ("gone", "00000000-0000-0000-0000-000000000009"),
    ):
        await client.post(
            f"/api/v1/projects/{slug}/annotations",
            json={
                "bucket": "2026-05-02T10:00:00Z",
                "label": label,
                "scope_type": "event_type",
                "scope_ref": ref,
            },
        )

    rows = {
        row["label"]: row
        for row in (await client.get(f"/api/v1/projects/{slug}/annotations")).json()
    }
    assert rows["global"]["scope_name"] is None
    assert rows["named"]["scope_name"] == "Screen View"
    assert rows["gone"]["scope_name"] is None
