"""Annotation ``source``/``url``, API-key access and create-time de-dup (GitHub #256).

CI posts deploy markers with a write-scoped key and ``source=api``; ``release``
is reserved to the metrics worker; a repeated ``api`` label inside 24h returns
the existing row with 200 instead of stacking a duplicate, a release label is
unique for good, and a manual annotation is never de-duplicated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update

from tripl.models.audit_log import AuditLog
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import ChartAnnotationSource
from tripl.services.chart_annotation_service import find_duplicate_annotation
from tripl.services.project_service import get_project_id_by_slug
from tripl.tests.conftest import TestSessionLocal


async def _project(client: AsyncClient, slug: str) -> str:
    resp = await client.post("/api/v1/projects", json={"name": "P", "slug": slug})
    assert resp.status_code == 201, resp.text
    return slug


async def _issue_key(client: AsyncClient, *, scope: str, project_slug: str | None = None) -> str:
    payload: dict[str, object] = {"name": "ci", "scope": scope}
    if project_slug is not None:
        payload["project_slug"] = project_slug
    resp = await client.post("/api/v1/me/api-keys", json=payload)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/annotations"


# ── source / url on the response ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_manual_annotation_reports_its_source_and_no_url(client: AsyncClient) -> None:
    slug = await _project(client, "src-manual")

    resp = await client.post(
        _url(slug), json={"bucket": "2026-09-01T10:00:00Z", "label": "Pricing change"}
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "manual"
    assert body["url"] is None
    listed = (await client.get(_url(slug))).json()
    assert [(item["source"], item["url"]) for item in listed] == [("manual", None)]


@pytest.mark.asyncio
async def test_an_api_annotation_keeps_its_source_and_url(client: AsyncClient) -> None:
    slug = await _project(client, "src-api")

    resp = await client.post(
        _url(slug),
        json={
            "bucket": "2026-09-25T12:00:00Z",
            "label": "Deployed web 2026.09.25",
            "source": "api",
            "url": " https://github.com/acme/web/releases/tag/2026.09.25 ",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "api"
    assert body["url"] == "https://github.com/acme/web/releases/tag/2026.09.25"


@pytest.mark.asyncio
async def test_a_client_cannot_claim_the_release_source(client: AsyncClient) -> None:
    slug = await _project(client, "src-release")

    resp = await client.post(
        _url(slug),
        json={"bucket": "2026-09-25T12:00:00Z", "label": "Release 9.9.9", "source": "release"},
    )

    assert resp.status_code == 422
    assert (await client.get(_url(slug))).json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/release",
        "https://",
        "not a url",
        "https://example.com/" + "a" * 500,
    ],
)
async def test_annotation_url_must_be_http_or_https(client: AsyncClient, url: str) -> None:
    slug = await _project(client, "src-url")

    resp = await client.post(
        _url(slug), json={"bucket": "2026-09-25T12:00:00Z", "label": "Deploy", "url": url}
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_an_unknown_source_is_rejected(client: AsyncClient) -> None:
    slug = await _project(client, "src-unknown")

    resp = await client.post(
        _url(slug), json={"bucket": "2026-09-25T12:00:00Z", "label": "Deploy", "source": "bot"}
    )

    assert resp.status_code == 422


# ── API-key access ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_project_scoped_write_key_can_post_a_deploy_marker(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    slug = await _project(client, "key-write")
    token = await _issue_key(client, scope="write", project_slug=slug)

    resp = await anon_client.post(
        _url(slug),
        json={
            "bucket": "2026-09-25T12:00:00Z",
            "label": "Deployed web 2026.09.25",
            "source": "api",
            "url": "https://ci.example.com/runs/42",
        },
        headers=_bearer(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["source"] == "api"


@pytest.mark.asyncio
async def test_a_read_key_cannot_post_an_annotation(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    slug = await _project(client, "key-read")
    token = await _issue_key(client, scope="read", project_slug=slug)

    resp = await anon_client.post(
        _url(slug),
        json={"bucket": "2026-09-25T12:00:00Z", "label": "Deploy", "source": "api"},
        headers=_bearer(token),
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_write_key_for_another_project_cannot_post_here(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    slug = await _project(client, "key-here")
    other = await _project(client, "key-other")
    token = await _issue_key(client, scope="write", project_slug=other)

    resp = await anon_client.post(
        _url(slug),
        json={"bucket": "2026-09-25T12:00:00Z", "label": "Deploy", "source": "api"},
        headers=_bearer(token),
    )

    assert resp.status_code == 403


# ── de-dup ────────────────────────────────────────────────────────────────────


async def _audit_creates() -> int:
    async with TestSessionLocal() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "chart_annotation.create")
            )
            or 0
        )


@pytest.mark.asyncio
async def test_a_repeated_source_and_label_returns_the_existing_row(
    client: AsyncClient,
) -> None:
    slug = await _project(client, "dedup")
    body = {"bucket": "2026-09-25T12:00:00Z", "label": "Deployed web 42", "source": "api"}

    first = await client.post(_url(slug), json=body)
    again = await client.post(_url(slug), json={**body, "bucket": "2026-09-25T13:00:00Z"})

    assert first.status_code == 201, first.text
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert len((await client.get(_url(slug))).json()) == 1
    # Nothing was written the second time, so nothing is audited either.
    assert await _audit_creates() == 1


@pytest.mark.asyncio
async def test_a_repeated_manual_annotation_is_created_every_time(
    client: AsyncClient,
) -> None:
    """A person adding the same note twice meant to: manual is never de-duplicated."""
    slug = await _project(client, "dedup-manual")
    body = {"bucket": "2026-09-25T12:00:00Z", "label": "Pricing change"}

    first = await client.post(_url(slug), json=body)
    again = await client.post(_url(slug), json=body)

    assert first.status_code == 201, first.text
    assert again.status_code == 201, again.text
    assert again.json()["id"] != first.json()["id"]
    assert len((await client.get(_url(slug))).json()) == 2
    assert await _audit_creates() == 2


@pytest.mark.asyncio
async def test_dedup_matches_the_trimmed_label(client: AsyncClient) -> None:
    slug = await _project(client, "dedup-trim")

    first = await client.post(
        _url(slug),
        json={"bucket": "2026-09-25T12:00:00Z", "label": "Deploy 7", "source": "api"},
    )
    again = await client.post(
        _url(slug),
        json={"bucket": "2026-09-25T12:00:00Z", "label": "  Deploy 7  ", "source": "api"},
    )

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_the_same_label_from_another_source_is_not_a_duplicate(
    client: AsyncClient,
) -> None:
    slug = await _project(client, "dedup-source")

    manual = await client.post(
        _url(slug), json={"bucket": "2026-09-25T12:00:00Z", "label": "Deploy 8"}
    )
    api = await client.post(
        _url(slug),
        json={"bucket": "2026-09-25T12:00:00Z", "label": "Deploy 8", "source": "api"},
    )

    assert manual.status_code == 201
    assert api.status_code == 201
    assert api.json()["id"] != manual.json()["id"]


@pytest.mark.asyncio
async def test_the_same_label_in_another_project_is_not_a_duplicate(
    client: AsyncClient,
) -> None:
    first_slug = await _project(client, "dedup-p1")
    second_slug = await _project(client, "dedup-p2")
    body = {"bucket": "2026-09-25T12:00:00Z", "label": "Deploy 9", "source": "api"}

    assert (await client.post(_url(first_slug), json=body)).status_code == 201
    assert (await client.post(_url(second_slug), json=body)).status_code == 201


@pytest.mark.asyncio
async def test_a_label_older_than_the_window_is_created_again(client: AsyncClient) -> None:
    slug = await _project(client, "dedup-window")
    body = {"bucket": "2026-09-25T12:00:00Z", "label": "Nightly deploy", "source": "api"}

    first = await client.post(_url(slug), json=body)
    assert first.status_code == 201
    async with TestSessionLocal() as session:
        await session.execute(
            update(ChartAnnotation).values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await session.commit()

    again = await client.post(_url(slug), json=body)

    assert again.status_code == 201, again.text
    assert again.json()["id"] != first.json()["id"]
    assert len((await client.get(_url(slug))).json()) == 2


@pytest.mark.asyncio
async def test_a_release_label_is_a_duplicate_at_any_age(client: AsyncClient) -> None:
    """Release markers are unique per project for good, not per 24h."""
    slug = await _project(client, "dedup-release")
    async with TestSessionLocal() as session:
        project_id = await get_project_id_by_slug(session, slug)
        session.add(
            ChartAnnotation(
                project_id=project_id,
                bucket=datetime(2026, 1, 1, tzinfo=UTC),
                label="Release 1.4.0",
                source=ChartAnnotationSource.release.value,
                created_at=datetime.now(UTC) - timedelta(days=90),
            )
        )
        await session.commit()

        found = await find_duplicate_annotation(
            session,
            project_id,
            source=ChartAnnotationSource.release.value,
            label="Release 1.4.0",
        )
        manual = await find_duplicate_annotation(
            session,
            project_id,
            source=ChartAnnotationSource.manual.value,
            label="Release 1.4.0",
        )

    assert found is not None
    assert found.label == "Release 1.4.0"
    assert manual is None
