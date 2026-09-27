"""Photo attachments, batch 2 of the backend review sweep (tripl-0zpq).

* .146 — one blob backs a main photo and every branch twin of it, so deleting
  any one row must leave the blob to the others.
* .214 / .236 — an upload is refused on type and size before it is buffered,
  and the request body is capped before it is spooled.
* .213 — a signing failure falls back to the /file route instead of a 500.
* .237 — a reorder that repeats a photo is refused.
* .211 — the shipped images can create the default photo directory.
* .145 — a merged or closed branch's event takes no photo or spec writes,
  although these routes never see ``?branch=``; its photos can still be
  discussed, and main, stored as merged, stays writable.
"""

from __future__ import annotations

import io
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fastapi import HTTPException
from google.auth import exceptions as google_auth_exceptions
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from tripl.config import Settings, settings
from tripl.main import app
from tripl.models.event import Event
from tripl.models.event_photo import EventPhoto
from tripl.services import event_photo_service
from tripl.storage import photo_storage
from tripl.storage.photo_storage import GCSPhotoStorage, reset_photo_storage
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import (
    _approve_and_merge,
    _attach_main_figma,
    _create_branch,
    _seed_plan,
    _transition,
)
from tripl.tests.test_rbac import _register, _set_role

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_MIB = 1024 * 1024
_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def local_photos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The local backend, rooted in a directory of this test's own."""
    monkeypatch.setattr(settings, "photo_storage_backend", "local")
    monkeypatch.setattr(settings, "photo_local_dir", str(tmp_path))
    reset_photo_storage()
    yield tmp_path
    reset_photo_storage()


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The ``size`` argument of every read of an uploaded file (-1 = the whole file)."""
    sizes: list[int] = []
    original = StarletteUploadFile.read

    async def spying_read(self: StarletteUploadFile, size: int = -1) -> bytes:
        sizes.append(size)
        return await original(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", spying_read)
    return sizes


@pytest.fixture
def spooled(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How many bytes each write put into an upload's temporary file."""
    sizes: list[int] = []
    original = StarletteUploadFile.write

    async def spying_write(self: StarletteUploadFile, data: bytes) -> None:
        sizes.append(len(data))
        await original(self, data)

    monkeypatch.setattr(StarletteUploadFile, "write", spying_write)
    return sizes


async def _main_event_id(client: AsyncClient, slug: str) -> str:
    events = await client.get(f"/api/v1/projects/{slug}/events")
    return str(events.json()["items"][0]["id"])


def _photos_url(slug: str, event_id: str) -> str:
    return f"/api/v1/projects/{slug}/events/{event_id}/photos"


async def _upload(
    client: AsyncClient,
    slug: str,
    event_id: str,
    data: bytes = _PNG,
    *,
    filename: str = "shot.png",
    content_type: str = "image/png",
) -> Any:
    return await client.post(
        _photos_url(slug, event_id), files={"file": (filename, data, content_type)}
    )


async def _branch_photo(branch_id: str) -> tuple[str, str]:
    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .one()
        )
        photo = (
            (
                await session.execute(
                    select(EventPhoto).where(EventPhoto.event_id == branch_event.id)
                )
            )
            .scalars()
            .one()
        )
        return str(branch_event.id), str(photo.id)


# --- tripl-0zpq.146: a blob shared by several rows ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [("main", "one", "two"), ("one", "main", "two"), ("two", "one", "main")],
    ids=["main-first", "branch-first", "other-branch-first"],
)
async def test_a_shared_blob_outlives_every_row_but_the_last(
    client: AsyncClient, local_photos: Path, order: tuple[str, str, str]
) -> None:
    """Deleting a screenshot on a branch deleted the object main still pointed
    at, and every other branch's twin with it — and deleting on main broke
    every branch. Whichever holder deletes first, the others keep serving the
    image; the blob goes only with the last of them."""
    slug = "photo-shared-blob"
    await _seed_plan(client, slug)
    main_event_id = await _main_event_id(client, slug)
    uploaded = await _upload(client, slug, main_event_id)
    assert uploaded.status_code == 201, uploaded.text
    holders = {"main": (main_event_id, str(uploaded.json()["id"]))}
    for name in ("one", "two"):
        branch_id = await _create_branch(client, slug, f"feature-{name}")
        holders[name] = await _branch_photo(branch_id)

    async with TestSessionLocal() as session:
        keys = list((await session.execute(select(EventPhoto.storage_key))).scalars().all())
    # The premise: branch creation copied the key, not the object.
    assert len(keys) == 3
    assert len(set(keys)) == 1
    blob = local_photos / str(keys[0])
    assert blob.read_bytes() == _PNG

    for name in order:
        event_id, photo_id = holders.pop(name)
        deleted = await client.delete(f"{_photos_url(slug, event_id)}/{photo_id}")
        assert deleted.status_code == 204
        for other_event_id, other_photo_id in holders.values():
            served = await client.get(f"{_photos_url(slug, other_event_id)}/{other_photo_id}/file")
            assert served.status_code == 200
            assert served.content == _PNG
        assert blob.exists() == bool(holders)


# --- tripl-0zpq.214 / .236: refused before it is buffered --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("clip.mp4", "video/mp4"),
        ("vector.svg", "image/svg+xml"),
        ("blob.bin", "application/octet-stream"),
    ],
)
async def test_an_unsupported_type_is_refused_before_the_file_is_read(
    client: AsyncClient,
    local_photos: Path,
    reads: list[int],
    filename: str,
    content_type: str,
) -> None:
    slug = "photo-type-unread"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)

    resp = await _upload(
        client, slug, event_id, b"\x00" * 4096, filename=filename, content_type=content_type
    )

    assert resp.status_code == 415
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("excess", [1, 512 * 1024], ids=["one-byte-over", "half-a-mib-over"])
async def test_an_oversized_file_is_refused_before_it_is_read(
    client: AsyncClient,
    local_photos: Path,
    reads: list[int],
    monkeypatch: pytest.MonkeyPatch,
    excess: int,
) -> None:
    monkeypatch.setattr(settings, "photo_max_size_mb", 1)
    slug = "photo-size-unread"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)

    resp = await _upload(client, slug, event_id, b"\x00" * (_MIB + excess))

    assert resp.status_code == 413
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [len(_PNG), _MIB], ids=["small", "exactly-the-limit"])
async def test_an_accepted_upload_is_never_read_unbounded(
    client: AsyncClient,
    local_photos: Path,
    reads: list[int],
    monkeypatch: pytest.MonkeyPatch,
    size: int,
) -> None:
    monkeypatch.setattr(settings, "photo_max_size_mb", 1)
    slug = "photo-size-bounded"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    data = (_PNG * (size // len(_PNG) + 1))[:size]

    resp = await _upload(client, slug, event_id, data)

    assert resp.status_code == 201, resp.text
    assert resp.json()["size_bytes"] == size
    assert reads
    assert all(0 < requested <= _MIB + 1 for requested in reads)
    served = await client.get(resp.json()["url"])
    assert served.content == data


@pytest.mark.asyncio
async def test_a_file_of_unknown_size_is_never_buffered_past_the_limit(
    reads: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starlette counts a part's size while spooling it, but an ``UploadFile``
    need not carry one; the read is bounded either way."""
    monkeypatch.setattr(settings, "photo_max_size_mb", 1)
    upload = StarletteUploadFile(
        io.BytesIO(b"\x00" * (3 * _MIB)),
        size=None,
        headers=Headers({"content-type": "image/png"}),
    )

    with pytest.raises(HTTPException) as refused:
        await event_photo_service.read_upload(upload)  # type: ignore[arg-type]

    assert refused.value.status_code == 413
    assert reads
    assert all(0 < requested <= _MIB + 1 for requested in reads)


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_it_is_spooled_even_anonymously(
    anon_client: AsyncClient, spooled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FastAPI parses the form before any dependency runs, so the body used to
    be spooled to disk in full before the route learned the caller was not even
    signed in (401). A declared length over the cap is now refused unread."""
    monkeypatch.setattr(settings, "photo_max_size_mb", 1)
    body = b"\x00" * (event_photo_service.upload_body_limit_bytes() + 1)

    resp = await anon_client.post(
        _photos_url("no-such-project", str(uuid.uuid4())),
        files={"file": ("big.png", body, "image/png")},
    )

    assert resp.status_code == 413
    assert spooled == []


@pytest.mark.asyncio
async def test_a_body_without_a_declared_length_is_cut_off_at_the_cap(
    client: AsyncClient,
    local_photos: Path,
    spooled: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "photo_max_size_mb", 1)
    cap = event_photo_service.upload_body_limit_bytes()
    slug = "photo-chunked-cap"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    boundary = "tripl-cap-test"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    chunk = b"\x00" * (64 * 1024)

    async def body() -> AsyncIterator[bytes]:
        yield head
        for _ in range(3 * cap // len(chunk)):
            yield chunk
        yield tail

    resp = await client.post(
        _photos_url(slug, event_id),
        content=body(),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )

    # The premise: nothing was declared, so only counting can stop it.
    assert "content-length" not in resp.request.headers
    assert resp.status_code == 413
    assert sum(spooled) <= cap


# --- tripl-0zpq.213: a URL that cannot be signed -----------------------------

_TOKEN_ONLY = "token-only-credentials"


def _real_signer() -> Callable[..., str]:
    """Sign through the real client library with credentials that cannot sign.

    A bare access token is what ADC yields on Compute Engine, under workload
    identity, or from gcloud user credentials; the library refuses it locally,
    before any network call.
    """
    from google.cloud import storage as gcs
    from google.oauth2.credentials import Credentials

    bucket = gcs.Client(project="tripl-test", credentials=Credentials(token="t")).bucket("b")

    def sign(key: str, **kwargs: Any) -> str:
        return str(bucket.blob(key).generate_signed_url(**kwargs))

    return sign


def _raising_signer(error: Exception) -> Callable[..., str]:
    def sign(key: str, **kwargs: Any) -> str:
        raise error

    return sign


class _MemoryBlob:
    def __init__(self, store: dict[str, bytes], key: str, sign: Callable[..., str]) -> None:
        self._store = store
        self._key = key
        self._sign = sign

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        del content_type
        self._store[self._key] = data

    def download_as_bytes(self) -> bytes:
        return self._store[self._key]

    def delete(self) -> None:
        del self._store[self._key]

    def generate_signed_url(self, **kwargs: Any) -> str:
        return self._sign(self._key, **kwargs)


class _MemoryBucket:
    def __init__(self, sign: Callable[..., str]) -> None:
        self.store: dict[str, bytes] = {}
        self._sign = sign

    def blob(self, key: str) -> _MemoryBlob:
        return _MemoryBlob(self.store, key, self._sign)


def _private_gcs(sign: Callable[..., str]) -> GCSPhotoStorage:
    """The real GCS driver on a private bucket, with the bucket held in memory."""
    backend = object.__new__(GCSPhotoStorage)
    backend._bucket = _MemoryBucket(sign)  # type: ignore[assignment]
    backend._bucket_name = "b"
    backend._public = False
    backend._ttl = 3600
    return backend


def test_the_premise_token_only_credentials_cannot_sign() -> None:
    with pytest.raises(AttributeError, match="private key"):
        _real_signer()("events/x/y.png", version="v4", expiration=timedelta(minutes=5))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signer",
    [
        _TOKEN_ONLY,
        google_auth_exceptions.TransportError("signBlob unreachable"),
        google_auth_exceptions.RefreshError("token refresh failed"),
    ],
    ids=[_TOKEN_ONLY, "iam-transport-error", "refresh-error"],
)
async def test_a_url_that_cannot_be_signed_falls_back_to_the_api(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    signer: str | Exception,
) -> None:
    sign = _raising_signer(signer) if isinstance(signer, Exception) else _real_signer()
    # Registered under the backend's NAME: uploads follow the setting, and a
    # row's reads follow the name it recorded (tripl-0zpq.295).
    monkeypatch.setitem(photo_storage._BY_NAME, "gcs", _private_gcs(sign))
    monkeypatch.setattr(settings, "photo_storage_backend", "gcs")
    monkeypatch.setattr(event_photo_service, "_PUBLIC_URL_FAILURES_LOGGED", set())
    slug = "photo-unsigned"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    base = _photos_url(slug, event_id)

    with caplog.at_level(logging.WARNING, logger=event_photo_service.__name__):
        first = await _upload(client, slug, event_id)
        second = await _upload(client, slug, event_id, filename="other.png")
        listed = await client.get(base)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["storage_backend"] == "gcs"
    assert first.json()["url"] == f"{base}/{first.json()['id']}/file"
    assert listed.status_code == 200
    assert len(listed.json()) == 2
    for row in listed.json():
        assert row["url"] == f"{base}/{row['id']}/file"
        served = await client.get(row["url"])
        assert served.status_code == 200
        assert served.content == _PNG
    # Four failed signings (two upload responses, one list of two), one traceback.
    warnings = [
        record
        for record in caplog.records
        if record.name == event_photo_service.__name__ and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


# --- tripl-0zpq.237: a reorder that repeats a photo --------------------------


async def _two_figma_frames(client: AsyncClient, slug: str) -> tuple[str, dict[str, str]]:
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    ids = {
        "a": await _attach_main_figma(
            client, slug, event_id, "https://www.figma.com/file/a/A", "A"
        ),
        "b": await _attach_main_figma(
            client, slug, event_id, "https://www.figma.com/file/b/B", "B"
        ),
    }
    return event_id, ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern",
    [("a", "b", "a"), ("b", "a", "b"), ("a", "a", "b"), ("a", "b", "b", "a")],
    ids=["aba", "bab", "aab", "abba"],
)
async def test_reorder_refuses_a_repeated_photo(
    client: AsyncClient, pattern: tuple[str, ...]
) -> None:
    """Every pattern here names each photo, so it passed the set comparison and
    then took the LAST position of the repeated id — [A, B, A] put B first."""
    slug = "photo-reorder-repeat"
    event_id, ids = await _two_figma_frames(client, slug)
    base = _photos_url(slug, event_id)

    resp = await client.patch(f"{base}/reorder", json={"photo_ids": [ids[p] for p in pattern]})

    assert resp.status_code == 422
    listed = await client.get(base)
    assert [row["id"] for row in listed.json()] == [ids["a"], ids["b"]]


@pytest.mark.asyncio
async def test_reorder_still_applies_a_permutation_and_refuses_an_incomplete_one(
    client: AsyncClient,
) -> None:
    slug = "photo-reorder-permutation"
    event_id, ids = await _two_figma_frames(client, slug)
    base = _photos_url(slug, event_id)

    incomplete = await client.patch(f"{base}/reorder", json={"photo_ids": [ids["b"]]})
    assert incomplete.status_code == 400

    resp = await client.patch(f"{base}/reorder", json={"photo_ids": [ids["b"], ids["a"]]})
    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()] == [ids["b"], ids["a"]]
    listed = await client.get(base)
    assert [row["id"] for row in listed.json()] == [ids["b"], ids["a"]]


# --- tripl-0zpq.145: a merged or closed branch's photos ----------------------

_WRITES = ("upload", "figma", "reorder", "delete")
_FIGMA_FRAME = {"url": "https://www.figma.com/file/late/Late", "title": "Late"}


async def _read_only_copy(client: AsyncClient, slug: str, state: str) -> tuple[str, str, str]:
    """``(branch id, event id, photo id)`` of a branch's copy of a screenshot
    uploaded on main, the branch then merged or closed."""
    await _seed_plan(client, slug)
    uploaded = await _upload(client, slug, await _main_event_id(client, slug))
    assert uploaded.status_code == 201, uploaded.text
    branch_id = await _create_branch(client, slug, "shipped")
    if state == "merged":
        merged = await _approve_and_merge(client, slug, branch_id)
        assert merged.status_code == 200, merged.text
    else:
        assert (await _transition(client, slug, branch_id, "close"))["status"] == "closed"
    event_id, photo_id = await _branch_photo(branch_id)
    return branch_id, event_id, photo_id


async def _photo_write(client: AsyncClient, base: str, write: str, photo_id: str) -> Response:
    if write == "upload":
        return await client.post(base, files={"file": ("late.png", _PNG, "image/png")})
    if write == "figma":
        return await client.post(f"{base}/figma", json=_FIGMA_FRAME)
    if write == "reorder":
        return await client.patch(f"{base}/reorder", json={"photo_ids": [photo_id]})
    return await client.delete(f"{base}/{photo_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["merged", "closed"])
async def test_a_read_only_branch_takes_no_photo_or_spec_writes(
    client: AsyncClient, local_photos: Path, state: str
) -> None:
    """These routes address the branch's event by its own id and never see
    ``?branch=``, so the refusal in ``api/deps.py`` did not reach them: each
    write landed, and a merged branch drifted from the revision it merged, with
    a photos change in its diff that could never reach main. Refused in the
    words a ``?branch=`` write gets, before anything is stored."""
    slug = f"photo-ro-{state}"
    branch_id, event_id, photo_id = await _read_only_copy(client, slug, state)
    base = _photos_url(slug, event_id)
    blobs = sorted(local_photos.rglob("*.png"))
    via_branch = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}",
        json={"description": "edited"},
    )
    assert via_branch.status_code == 409, via_branch.text

    wrong: dict[str, str] = {}
    for write in _WRITES:
        resp = await _photo_write(client, base, write, photo_id)
        if resp.status_code != 409 or resp.json()["detail"] != via_branch.json()["detail"]:
            wrong[write] = f"{resp.status_code} {resp.text[:120]}"

    assert not wrong, wrong
    listed = await client.get(base)
    assert [row["id"] for row in listed.json()] == [photo_id]
    assert sorted(local_photos.rglob("*.png")) == blobs


@pytest.mark.asyncio
async def test_main_takes_photo_writes_although_it_is_stored_as_merged(
    client: AsyncClient, local_photos: Path
) -> None:
    """Main's row carries ``status="merged"``, so the refusal keys on a working
    branch's status, never on main's."""
    slug = "photo-main-merged"
    await _seed_plan(client, slug)
    branches = await client.get(f"/api/v1/projects/{slug}/branches")
    assert next(b for b in branches.json()["items"] if b["kind"] == "main")["status"] == "merged"
    event_id = await _main_event_id(client, slug)
    base = _photos_url(slug, event_id)

    uploaded = await _upload(client, slug, event_id)
    assert uploaded.status_code == 201, uploaded.text
    figma = await client.post(f"{base}/figma", json=_FIGMA_FRAME)
    assert figma.status_code == 201, figma.text
    order = [figma.json()["id"], uploaded.json()["id"]]
    reordered = await client.patch(f"{base}/reorder", json={"photo_ids": order})
    assert reordered.status_code == 200, reordered.text
    deleted = await client.delete(f"{base}/{uploaded.json()['id']}")
    assert deleted.status_code == 204, deleted.text

    listed = await client.get(base)
    assert [row["id"] for row in listed.json()] == [figma.json()["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["merged", "closed"])
async def test_a_read_only_branchs_photos_can_still_be_discussed(
    client: AsyncClient, local_photos: Path, state: str
) -> None:
    """Discussion is not plan content, and approval hashes strip it, so a
    photo on a merged or closed branch still takes and loses comments."""
    slug = f"photo-ro-talk-{state}"
    _, event_id, photo_id = await _read_only_copy(client, slug, state)
    comments = f"{_photos_url(slug, event_id)}/{photo_id}/comments"

    posted = await client.post(comments, json={"body": "why did this ship?"})
    assert posted.status_code == 201, posted.text
    reply = {"body": "it was approved", "parent_id": posted.json()["id"]}
    replied = await client.post(comments, json=reply)
    assert replied.status_code == 201, replied.text
    deleted = await client.delete(f"{comments}/{replied.json()['id']}")
    assert deleted.status_code == 204, deleted.text

    listed = await client.get(comments)
    assert [row["id"] for row in listed.json()] == [posted.json()["id"]]


@pytest.mark.asyncio
async def test_the_editor_gate_answers_before_the_read_only_409(
    client: AsyncClient, local_photos: Path
) -> None:
    """The refusal lives in the service, behind each route's editor gate, so a
    viewer is told it may not edit rather than to reopen a branch it has no
    right to reopen."""
    slug = "photo-ro-viewer"
    _, event_id, photo_id = await _read_only_copy(client, slug, "merged")
    base = _photos_url(slug, event_id)

    wrong: dict[str, str] = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as viewer:
        await _register(viewer, "viewer@example.com")
        # A viewer member, so the answer is the project-role gate's 403, not the
        # membership 404 (F20 PR4: write rights live on the project row).
        await add_member_by_slug(slug, "viewer@example.com", "viewer")
        # A role change ends the user's sessions, so sign in again after it.
        await _set_role(client, "viewer@example.com", "viewer")
        login = await viewer.post(
            "/api/v1/auth/login",
            json={"email": "viewer@example.com", "password": "Password123!"},
        )
        assert login.status_code == 200, login.text
        for write in _WRITES:
            resp = await _photo_write(viewer, base, write, photo_id)
            if (
                resp.status_code != 403
                or resp.json()["detail"] != "Editor access to this project is required"
            ):
                wrong[write] = f"{resp.status_code} {resp.text[:120]}"

    assert not wrong, wrong


# --- tripl-0zpq.211: the image can write its own photo directory -------------


def _instructions(text: str) -> list[str]:
    """A Dockerfile's instructions, continuations joined and comments dropped."""
    instructions: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        instructions.append(current + line)
        current = ""
    if current:
        instructions.append(current)
    return instructions


def _runtime_stage(path: Path) -> list[str]:
    instructions = _instructions(path.read_text())
    starts = [
        index
        for index, line in enumerate(instructions)
        if re.match(r"FROM\s+\S+\s+AS\s+runtime$", line, re.IGNORECASE)
    ]
    assert starts, f"{path} has no runtime stage"
    stage: list[str] = []
    for line in instructions[starts[-1] + 1 :]:
        if re.match(r"FROM\s", line, re.IGNORECASE):
            break
        stage.append(line)
    return stage


def _keyword(line: str) -> str:
    return line.split(None, 1)[0].upper()


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "backend/Dockerfile"])
def test_the_runtime_image_can_write_the_default_photo_directory(dockerfile: str) -> None:
    """The default ``photo_local_dir`` resolves under WORKDIR, which is owned
    by root, while the process runs as ``app`` — so the first upload died on
    mkdir. The directory must exist and belong to ``app`` before ``USER app``,
    without handing the rest of the tree over."""
    stage = _runtime_stage(_REPO_ROOT / dockerfile)
    user_at = next(index for index, line in enumerate(stage) if _keyword(line) == "USER")
    assert stage[user_at].split()[1] == "app"
    workdir = PurePosixPath(
        [line.split()[1] for line in stage[:user_at] if _keyword(line) == "WORKDIR"][-1]
    )
    # Where the running app will look: the configured default, resolved
    # against the directory the process starts in.
    photo_dir = workdir / Settings.model_fields["photo_local_dir"].default

    commands = [
        command.strip()
        for line in stage[:user_at]
        if _keyword(line) == "RUN"
        for command in re.split(r"&&|;", line.split(None, 1)[1])
    ]
    assert any(
        command.split()[0] == "mkdir" and str(photo_dir) in command.split()
        for command in commands
        if command
    ), f"{dockerfile} never creates {photo_dir}"

    handed_over = False
    for command in commands:
        argv = command.split()
        if not argv or argv[0] != "chown":
            continue
        flags = [arg for arg in argv[1:] if arg.startswith("-")]
        owner, *targets = [arg for arg in argv[1:] if not arg.startswith("-")]
        paths = [PurePosixPath(target) for target in targets]
        assert all(path not in (PurePosixPath("/"), workdir) for path in paths), (
            f"{dockerfile} hands the whole tree to the runtime user"
        )
        recursive = "-R" in flags or "--recursive" in flags
        if owner.split(":")[0] in ("app", "1000") and any(
            path == photo_dir or (recursive and path in photo_dir.parents) for path in paths
        ):
            handed_over = True
    assert handed_over, f"{dockerfile} never gives {photo_dir} to the app user"


# --- tripl-0zpq.295: a photo is read through the backend its ROW names --------


class _FakeGCS:
    """A driver for a backend this instance switched TO; it holds no old keys."""

    backend_name = "gcs"

    def __init__(self) -> None:
        self.asked_for: list[str] = []

    async def read(self, key: str) -> bytes:
        self.asked_for.append(key)
        raise FileNotFoundError(key)

    async def delete(self, key: str) -> None:
        self.asked_for.append(key)

    async def public_url(self, key: str, content_type: str) -> str | None:
        self.asked_for.append(key)
        return f"https://storage.example/{key}"

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        raise AssertionError("this test never uploads through the new backend")


async def _main_photo(client: AsyncClient, slug: str) -> tuple[str, str]:
    """``(event id, photo id)`` of one screenshot uploaded onto main."""
    events = await client.get(f"/api/v1/projects/{slug}/events")
    event_id = str(events.json()["items"][0]["id"])
    uploaded = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos",
        files={"file": ("shot.png", _PNG, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return event_id, uploaded.json()["id"]


@pytest.mark.asyncio
async def test_a_photo_survives_a_switch_of_the_storage_backend(
    client: AsyncClient, local_photos: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner moves the instance from local to GCS. Everything uploaded
    before that keeps `storage_backend="local"`, and the files are still on the
    volume — but /file read through the process's CURRENT driver, so it looked
    every one of those keys up in the bucket and 404ed the lot."""
    slug = "photo-backend-switch"
    await _seed_plan(client, slug)
    event_id, photo_id = await _main_photo(client, slug)

    new_backend = _FakeGCS()
    monkeypatch.setitem(photo_storage._BY_NAME, "gcs", new_backend)
    monkeypatch.setattr(settings, "photo_storage_backend", "gcs")

    served = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/photos/{photo_id}/file")
    assert served.status_code == 200
    assert served.content == _PNG
    assert new_backend.asked_for == [], "the old row must not be looked up in the new store"


@pytest.mark.asyncio
async def test_a_photo_whose_backend_is_no_longer_configured_says_so(
    client: AsyncClient, local_photos: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of a switch: moved back to local and the bucket setting
    dropped, so the rows written to GCS have no driver to read them through.
    That is a 409 naming the backend, not a 404 blaming the photo — and the rest
    of the page still loads, which is why building a driver never raises here."""
    slug = "photo-backend-gone"
    await _seed_plan(client, slug)
    event_id, photo_id = await _main_photo(client, slug)
    async with TestSessionLocal() as session:
        row = await session.get(EventPhoto, uuid.UUID(photo_id))
        assert row is not None
        row.storage_backend = "gcs"
        await session.commit()
    monkeypatch.setattr(settings, "gcs_photo_bucket", "")

    listed = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/photos")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["url"].endswith("/file")

    served = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/photos/{photo_id}/file")
    assert served.status_code == 409
    assert "gcs" in served.json()["detail"]
