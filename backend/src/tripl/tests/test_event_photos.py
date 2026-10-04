"""Coverage for figma-spec attachments and threaded comments on event_photos.

The existing image upload + URL resolution paths are exercised indirectly via
the wider suite; these tests focus on the new feature surface: figma kind,
URL validation, and the comments thread."""

import pytest
from httpx import AsyncClient


async def _setup_event(client: AsyncClient, slug: str = "ph-proj") -> tuple[str, str]:
    await client.post("/api/v1/projects", json={"name": "P", "slug": slug})
    et_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    et_id = et_resp.json()["id"]
    event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "Home Page View"},
    )
    return slug, event_resp.json()["id"]


class _StubBlob:
    def __init__(self, error: Exception | None) -> None:
        self._error = error
        self.deleted = False

    def delete(self) -> None:
        if self._error is not None:
            raise self._error
        self.deleted = True


class _StubBucket:
    def __init__(self, blob: _StubBlob) -> None:
        self._blob = blob

    def blob(self, key: str) -> _StubBlob:
        del key
        return self._blob


def _gcs_backend_with(blob: _StubBlob) -> object:
    """A GCSPhotoStorage with only the bucket wired — no google client needed."""
    from tripl.storage.photo_storage import GCSPhotoStorage

    backend = object.__new__(GCSPhotoStorage)
    backend._bucket = _StubBucket(blob)  # type: ignore[attr-defined]
    return backend


def test_gcs_delete_treats_a_missing_object_as_done() -> None:
    from google.api_core import exceptions as gcs_exceptions

    blob = _StubBlob(gcs_exceptions.NotFound("gone"))
    backend = _gcs_backend_with(blob)

    backend._delete("photos/abc.png")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("no delete permission"),
        ConnectionError("network down"),
        RuntimeError("quota exceeded"),
    ],
)
def test_gcs_delete_reports_a_real_failure(error: Exception) -> None:
    """A delete that did not happen must not be reported as success.

    This was ``suppress(Exception)``, so a permission, network or quota failure
    left the object fetchable at a stable key while the API answered 204 and the
    row was removed — the one state where the UI and the bucket disagree.
    """
    backend = _gcs_backend_with(_StubBlob(error))

    with pytest.raises(type(error)):
        backend._delete("photos/abc.png")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_attach_figma_creates_kind_figma_row(client: AsyncClient) -> None:
    slug, event_id = await _setup_event(client)

    resp = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://www.figma.com/file/abc123/Onboarding", "title": "Onboarding"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "figma"
    assert body["external_url"] == "https://www.figma.com/file/abc123/Onboarding"
    assert body["url"] == "https://www.figma.com/file/abc123/Onboarding"
    assert body["storage_backend"] is None
    assert body["original_filename"] == "Onboarding"


@pytest.mark.asyncio
async def test_attach_figma_rejects_non_figma_url(client: AsyncClient) -> None:
    slug, event_id = await _setup_event(client, slug="ph-bad")

    resp = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://example.com/file/abc", "title": ""},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_includes_figma_rows(client: AsyncClient) -> None:
    slug, event_id = await _setup_event(client, slug="ph-list")
    await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://www.figma.com/proto/xyz/Flow", "title": "Flow"},
    )

    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/photos")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["kind"] == "figma"


@pytest.mark.asyncio
async def test_delete_figma_row_succeeds(client: AsyncClient) -> None:
    slug, event_id = await _setup_event(client, slug="ph-del")
    create = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://www.figma.com/file/del/Spec", "title": ""},
    )
    photo_id = create.json()["id"]

    resp = await client.delete(f"/api/v1/projects/{slug}/events/{event_id}/photos/{photo_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_comments_thread_create_list_reply_delete(client: AsyncClient) -> None:
    slug, event_id = await _setup_event(client, slug="ph-cmt")
    create_photo = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://www.figma.com/file/cmt/Spec", "title": ""},
    )
    photo_id = create_photo.json()["id"]

    base = f"/api/v1/projects/{slug}/events/{event_id}/photos/{photo_id}/comments"

    resp = await client.post(base, json={"body": "Top comment"})
    assert resp.status_code == 201
    parent_id = resp.json()["id"]
    assert resp.json()["parent_id"] is None

    reply = await client.post(base, json={"body": "Reply", "parent_id": parent_id})
    assert reply.status_code == 201
    assert reply.json()["parent_id"] == parent_id

    listed = await client.get(base)
    assert listed.status_code == 200
    bodies = [item["body"] for item in listed.json()]
    assert bodies == ["Top comment", "Reply"]

    deleted = await client.delete(f"{base}/{parent_id}")
    assert deleted.status_code == 204
    # Parent comment should be gone. (Postgres cascades the reply via
    # ON DELETE CASCADE; SQLite in tests doesn't enforce FK cascades by
    # default, so we assert on the parent only.)
    after = await client.get(base)
    remaining_ids = {item["id"] for item in after.json()}
    assert parent_id not in remaining_ids


@pytest.mark.asyncio
async def test_comment_parent_must_belong_to_same_photo(client: AsyncClient) -> None:
    slug, event_id = await _setup_event(client, slug="ph-cmt-cross")
    photo_a = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://www.figma.com/file/a/A", "title": ""},
    )
    photo_b = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://www.figma.com/file/b/B", "title": ""},
    )

    base_a = f"/api/v1/projects/{slug}/events/{event_id}/photos/{photo_a.json()['id']}/comments"
    base_b = f"/api/v1/projects/{slug}/events/{event_id}/photos/{photo_b.json()['id']}/comments"

    top = await client.post(base_a, json={"body": "On A"})
    parent_id = top.json()["id"]

    cross = await client.post(base_b, json={"body": "Wrong parent", "parent_id": parent_id})
    assert cross.status_code == 400
