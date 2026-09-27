from __future__ import annotations

import logging
import mimetypes
import re
import uuid
from collections.abc import Iterable

from fastapi import HTTPException, UploadFile
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import settings
from tripl.models.domain_enums import EventPhotoKind
from tripl.models.event import Event
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.plan_branch import BranchKind, BranchStatus
from tripl.models.user import User
from tripl.services import notification_announce, subscription_service
from tripl.services._plan_branch_locks import hold_branch_for_plan_write
from tripl.services.mentions import excerpt, mentioned_user_ids
from tripl.services.project_service import get_project_id_by_slug
from tripl.storage import PhotoStorage, get_photo_storage, storage_for

logger = logging.getLogger(__name__)

PHOTO_KIND_PHOTO = EventPhotoKind.photo.value
PHOTO_KIND_FIGMA = EventPhotoKind.figma.value

# Match canonical figma.com URLs only — narrow on purpose so we don't render
# arbitrary cross-origin iframes for users.
_FIGMA_URL_RE = re.compile(
    r"^https://(?:www\.)?figma\.com/(?:file|proto|design|board|community/file)/[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)

# Every blob ``upload_photo`` writes lives under this prefix, and the orphan
# sweep lists nothing else: a bucket or directory may be shared with objects
# tripl did not write (tripl-0zpq.291).
PHOTO_KEY_PREFIX = "events/"

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _allowed_mime_types() -> set[str]:
    raw = settings.photo_allowed_mime or ""
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def max_size_mb() -> int:
    """The per-file upload limit in MiB, exactly as ``read_upload`` applies it."""
    return max(1, settings.photo_max_size_mb)


def _max_size_bytes() -> int:
    return max_size_mb() * 1024 * 1024


# Room for the multipart framing around the one file part: boundaries, part
# headers, the filename. Generous on purpose — the body cap exists to stop a
# multi-gigabyte request, not to police the last kilobyte; the exact limit is
# applied to the file itself by ``read_upload``.
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024


def upload_body_limit_bytes() -> int:
    """The most an upload REQUEST may carry: the file limit plus its framing."""
    return _max_size_bytes() + _MULTIPART_OVERHEAD_BYTES


def upload_too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"File too large (max {settings.photo_max_size_mb} MB)",
    )


def check_upload_content_type(content_type: str) -> str:
    """The upload's content type, normalised — or 415 when it is not allowed."""
    allowed = _allowed_mime_types()
    normalized_ct = (content_type or "").lower().split(";", 1)[0].strip()
    if normalized_ct not in allowed:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content type {content_type!r}. Allowed: {sorted(allowed)}",
        )
    return normalized_ct


async def read_upload(file: UploadFile) -> bytes:
    """The uploaded file's bytes, refused BEFORE they are buffered when they cannot be kept.

    The route used to call ``await file.read()`` ahead of every check, so a
    multi-gigabyte upload, or a video dropped on the photo zone, was loaded
    whole into one worker's memory before the service got to answer 413 or 415
    (tripl-0zpq.214, tripl-0zpq.236). The type is checked first, then the size
    Starlette counted while spooling the part, and the read itself never asks
    for more than one byte past the limit — so a file whose size is not known
    up front still cannot buffer more than that.
    """
    check_upload_content_type(file.content_type or "")
    limit = _max_size_bytes()
    if file.size is not None and file.size > limit:
        raise upload_too_large()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise upload_too_large()
    return data


def _resolve_extension(content_type: str, filename: str) -> str:
    ext = _EXT_BY_MIME.get(content_type.lower())
    if ext:
        return ext
    guess = mimetypes.guess_extension(content_type) or ""
    if guess:
        return guess
    # Last resort: trust the original filename's suffix if present.
    _, dot, tail = filename.rpartition(".")
    if dot and 1 <= len(tail) <= 8 and tail.isalnum():
        return f".{tail.lower()}"
    return ""


async def _get_event(session: AsyncSession, slug: str, event_id: uuid.UUID) -> Event:
    project_id = await get_project_id_by_slug(session, slug)
    row = await session.execute(
        select(Event).where(Event.id == event_id, Event.project_id == project_id)
    )
    event = row.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


async def _get_plan_writable_event(session: AsyncSession, slug: str, event_id: uuid.UUID) -> Event:
    """The event, or 409 when it belongs to a merged or closed working branch.

    Photos and Figma frames are plan content: a branch copies them, its merge
    carries them to main, and approval hashes include them. The ``?branch=``
    refusal in ``api/deps.py`` never ran here, because these routes address a
    branch's event by its own id, so a merged branch kept taking screenshots
    and drifted from the revision it merged (tripl-0zpq.145). Same statuses and
    same wording, read off the event's own branch. Main is stored with
    ``status="merged"``, so it is split off by kind first. Like the ``?branch=``
    refusal, the branch row is re-read ``FOR SHARE`` and held to the write's
    commit, main's included: a write arriving during a merge of its branch
    waits and then sees ``merged`` (tripl-0zpq.288), and one arriving on main
    during any merge waits and applies after it (tripl-0zpq.294).

    Comments do not come through here: discussion is not plan content, and
    approval hashes strip it. The routes' editor gate has already run, so a
    caller it refuses gets its 403, never this 409.
    """
    event = await _get_event(session, slug, event_id)
    branch = await hold_branch_for_plan_write(session, event.branch_id)
    if branch is None or branch.kind == BranchKind.main.value:
        return event
    if branch.status == BranchStatus.merged.value:
        raise HTTPException(
            status_code=409, detail=f"Branch '{branch.name}' is merged, so its plan is read-only"
        )
    if branch.status == BranchStatus.closed.value:
        raise HTTPException(
            status_code=409,
            detail=f"Branch '{branch.name}' is closed; reopen it before editing its plan",
        )
    return event


async def list_photos(session: AsyncSession, slug: str, event_id: uuid.UUID) -> list[EventPhoto]:
    event = await _get_event(session, slug, event_id)
    rows = await session.execute(
        select(EventPhoto)
        .where(EventPhoto.event_id == event.id)
        .order_by(EventPhoto.sort_order.asc(), EventPhoto.created_at.asc())
    )
    return list(rows.scalars().all())


async def upload_photo(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    *,
    data: bytes,
    content_type: str,
    original_filename: str,
    uploaded_by_user_id: uuid.UUID | None,
) -> EventPhoto:
    normalized_ct = check_upload_content_type(content_type)
    if not data:
        raise HTTPException(status_code=422, detail="Empty upload")
    if len(data) > _max_size_bytes():
        raise upload_too_large()

    event = await _get_plan_writable_event(session, slug, event_id)
    storage = get_photo_storage()

    photo_id = uuid.uuid4()
    ext = _resolve_extension(normalized_ct, original_filename)
    storage_key = f"{PHOTO_KEY_PREFIX}{event.id}/{photo_id}{ext}"

    await storage.save(storage_key, data, normalized_ct)

    next_order = await session.scalar(
        select(func.coalesce(func.max(EventPhoto.sort_order), -1) + 1).where(
            EventPhoto.event_id == event.id
        )
    )

    photo = EventPhoto(
        id=photo_id,
        project_id=event.project_id,
        event_id=event.id,
        uploaded_by_user_id=uploaded_by_user_id,
        original_filename=original_filename[:500],
        content_type=normalized_ct,
        size_bytes=len(data),
        storage_backend=storage.backend_name,
        storage_key=storage_key,
        sort_order=int(next_order or 0),
    )
    session.add(photo)
    try:
        await session.commit()
    except Exception:
        # If the DB write fails after the upload, best-effort clean the
        # orphaned object so the bucket / filesystem doesn't accumulate
        # leaked files. Deliberately swallowed here, where ``delete_photo``
        # lets a failed delete raise: the caller needs the original DB error,
        # not a cleanup failure raised on top of it. A failed cleanup leaves
        # one unreferenced blob, which is why it is logged rather than ignored
        # (tripl-jfm3.118).
        try:
            await storage.delete(storage_key)
        except Exception:
            logger.exception("Failed to clean up orphaned photo object %s", storage_key)
        raise
    await session.refresh(photo)
    return photo


async def get_photo(
    session: AsyncSession, slug: str, event_id: uuid.UUID, photo_id: uuid.UUID
) -> EventPhoto:
    return await _photo_on(session, await _get_event(session, slug, event_id), photo_id)


async def _photo_on(session: AsyncSession, event: Event, photo_id: uuid.UUID) -> EventPhoto:
    row = await session.execute(
        select(EventPhoto).where(
            EventPhoto.id == photo_id,
            EventPhoto.event_id == event.id,
        )
    )
    photo = row.scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return photo


async def _blob_is_referenced(
    session: AsyncSession,
    *,
    storage_backend: str | None,
    storage_key: str | None,
    other_than: uuid.UUID | None = None,
) -> bool:
    """Whether any attachment row points at this blob, the row ``other_than`` aside.

    One blob routinely backs several rows: branch creation copies
    ``storage_key`` onto every branch twin instead of duplicating the object,
    and a merge copies it back onto main the same way. Deliberately not scoped
    to the project or the event — any row holding the key keeps the blob.
    """
    clauses = [
        EventPhoto.storage_backend == storage_backend,
        EventPhoto.storage_key == storage_key,
    ]
    if other_than is not None:
        clauses.append(EventPhoto.id != other_than)
    return bool(await session.scalar(select(exists().where(*clauses))))


async def _blob_referenced_elsewhere(session: AsyncSession, photo: EventPhoto) -> bool:
    """Whether any OTHER attachment row still points at this photo's blob."""
    return await _blob_is_referenced(
        session,
        storage_backend=photo.storage_backend,
        storage_key=photo.storage_key,
        other_than=photo.id,
    )


async def delete_unreferenced_blobs(
    session: AsyncSession, blobs: Iterable[tuple[str, str]]
) -> None:
    """Delete each ``(storage_backend, storage_key)`` blob no attachment row points at any more.

    For a caller that has already COMMITTED the removal of rows holding these
    keys without going through ``delete_photo`` — today the branch merge, whose
    bulk delete of the photos a branch removed never touches storage. Deleting a
    screenshot on a branch leaves the blob to main's row, which still holds it
    (tripl-0zpq.146); the merge then deleted that row and the object stayed in
    the bucket with nothing pointing at it, for good. Each key is checked
    against the committed rows first, so one a twin on another branch — or a
    row the same merge inserted — still holds is left where it is.

    Best-effort by contract: the rows are gone and committed, so the worst a
    failure here can do is leave an object nobody points at. Logged, never
    raised. A ``delete_photo`` or a branch creation copying the same key at the
    same moment can still race this check (tripl-0zpq.291).
    """
    released = sorted(set(blobs))
    if not released:
        return
    for storage_backend, storage_key in released:
        # Each key in the store it was WRITTEN to. An instance switched between
        # backends still holds rows from the other one, and the same key there
        # names a different object, or none (tripl-0zpq.295).
        try:
            storage = storage_for(storage_backend)
        except Exception:
            logger.exception(
                "Cannot reach the %s backend; released photo blob %s left behind",
                storage_backend,
                storage_key,
            )
            continue
        try:
            referenced = await _blob_is_referenced(
                session, storage_backend=storage_backend, storage_key=storage_key
            )
        except Exception:
            # A failed read leaves the transaction unusable for the rest of the
            # list, so stop rather than log the same failure once per key.
            logger.exception(
                "Could not check %d released photo blob(s) for other references",
                len(released),
            )
            return
        if referenced:
            continue
        try:
            await storage.delete(storage_key)
        except Exception:
            logger.exception("Failed to delete released photo blob %s", storage_key)


async def delete_photo(
    session: AsyncSession, slug: str, event_id: uuid.UUID, photo_id: uuid.UUID
) -> None:
    event = await _get_plan_writable_event(session, slug, event_id)
    photo = await _photo_on(session, event, photo_id)
    # Figma-kind rows have no uploaded blob — skip storage cleanup. An uploaded
    # blob goes with the LAST row that references it, not the first: deleting a
    # screenshot on a branch used to delete the object main and every other
    # branch still pointed at, so their images 404ed on GCS and /file raised
    # FileNotFoundError on the local backend (tripl-0zpq.146). This is the only
    # place a blob is deleted for a row that exists, and the merge's
    # ``delete_unreferenced_blobs`` the only one for rows already gone; event,
    # branch and project deletes drop the rows by FK cascade and never touch
    # storage (tripl-0zpq.291). Still deleted BEFORE the row, so a failed
    # delete leaves the row to retry (tripl-jfm3.118).
    if (
        photo.kind == PHOTO_KIND_PHOTO
        and photo.storage_key
        and not await _blob_referenced_elsewhere(session, photo)
    ):
        # Through the backend the ROW names: after a backend switch the current
        # driver would look this key up in the wrong store (tripl-0zpq.295).
        storage = _storage_of(photo)
        if storage is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This photo is stored on the '{photo.storage_backend}' backend, which "
                    "this instance cannot reach, so its file cannot be deleted with it."
                ),
            )
        await storage.delete(photo.storage_key)
    await session.delete(photo)
    await session.commit()


async def attach_figma(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    *,
    external_url: str,
    title: str,
    uploaded_by_user_id: uuid.UUID | None,
) -> EventPhoto:
    normalized_url = external_url.strip()
    if not _FIGMA_URL_RE.match(normalized_url):
        raise HTTPException(
            status_code=422,
            detail="Only Figma URLs (figma.com/file, /design, /proto, /board) are supported",
        )

    event = await _get_plan_writable_event(session, slug, event_id)
    next_order = await session.scalar(
        select(func.coalesce(func.max(EventPhoto.sort_order), -1) + 1).where(
            EventPhoto.event_id == event.id
        )
    )

    photo = EventPhoto(
        project_id=event.project_id,
        event_id=event.id,
        uploaded_by_user_id=uploaded_by_user_id,
        kind=PHOTO_KIND_FIGMA,
        external_url=normalized_url,
        original_filename=(title or "Figma frame")[:500],
        content_type="application/x-figma-embed",
        size_bytes=0,
        storage_backend=None,
        storage_key=None,
        sort_order=int(next_order or 0),
    )
    session.add(photo)
    await session.commit()
    await session.refresh(photo)
    return photo


async def list_comments(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
) -> list[EventPhotoComment]:
    # Validates that the photo belongs to this event/project before returning
    # comments — keeps cross-project leakage from being possible via id-guess.
    await get_photo(session, slug, event_id, photo_id)
    rows = await session.execute(
        select(EventPhotoComment)
        .where(EventPhotoComment.photo_id == photo_id)
        .order_by(EventPhotoComment.created_at.asc())
    )
    return list(rows.scalars().all())


async def create_comment(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
    *,
    body: str,
    parent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> EventPhotoComment:
    event = await _get_event(session, slug, event_id)
    await _photo_on(session, event, photo_id)
    if parent_id is not None:
        parent = await session.get(EventPhotoComment, parent_id)
        if parent is None or parent.photo_id != photo_id:
            raise HTTPException(status_code=400, detail="parent_id must belong to this photo")

    comment = EventPhotoComment(
        photo_id=photo_id,
        parent_id=parent_id,
        user_id=user_id,
        body=body.strip(),
    )
    session.add(comment)
    await session.flush()
    # Best-effort, in a savepoint: a failed mention never fails the comment.
    await notification_announce.best_effort(
        session,
        "photo comment mentions",
        lambda: _announce_photo_comment_mentions(session, slug, event, comment),
    )
    await session.commit()
    await session.refresh(comment)
    return comment


async def _announce_photo_comment_mentions(
    session: AsyncSession, slug: str, event: Event, comment: EventPhotoComment
) -> None:
    """@mentions in a screenshot's thread (#259): the same pass as an event comment.

    Only mentions: a screenshot thread has no watchers of its own. The
    notification is about the event's discussion home (the main twin of a
    branch copy), the id an event's subscriptions and mutes are kept under.
    """
    if not mentioned_user_ids(comment.body):
        return
    home_id = await subscription_service.canonical_event_id(session, event.project_id, event)
    who = await notification_announce.actor_label(session, comment.user_id)
    label = event.name or "an event"
    await notification_announce.announce_mentions(
        session,
        body=comment.body,
        title=f"{who} mentioned you on a screenshot of {label}",
        common={
            "project_id": event.project_id,
            "entity_type": subscription_service.EVENT,
            "entity_id": home_id,
            "url": f"/p/{slug}/events/detail/{home_id}",
            "body": excerpt(comment.body),
            "actor_user_id": comment.user_id,
        },
    )


def ensure_comment_deletable(comment: EventPhotoComment, user: User) -> None:
    """Only the comment's author or an owner may delete it.

    The editor gate on the route answers "may this user write to the project";
    it does not make another editor's words theirs to remove. A comment whose
    author was deleted (``user_id`` NULL) is left to owners. Shared by the event
    and the photo threads, which are the same table.
    """
    if user.role == "owner":
        return
    if comment.user_id is not None and comment.user_id == user.id:
        return
    raise HTTPException(
        status_code=403,
        detail="Only the comment's author or an owner can delete it",
    )


async def delete_comment(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
    comment_id: uuid.UUID,
    *,
    user: User,
) -> None:
    await get_photo(session, slug, event_id, photo_id)
    comment = await session.get(EventPhotoComment, comment_id)
    if comment is None or comment.photo_id != photo_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    ensure_comment_deletable(comment, user)
    await session.delete(comment)
    await session.commit()


async def reorder_photos(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    photo_ids: list[uuid.UUID],
) -> list[EventPhoto]:
    # A repeated id passes the set comparison below and then takes the LAST
    # position it is listed at, so [A, B, A] answered 200 while putting B first
    # and listing A twice in the response (tripl-0zpq.237). Checked before the
    # lookup, like any other malformed body.
    if len(set(photo_ids)) != len(photo_ids):
        raise HTTPException(
            status_code=422,
            detail="photo_ids repeats a photo; list every photo on this event exactly once",
        )
    event = await _get_plan_writable_event(session, slug, event_id)

    rows = await session.execute(select(EventPhoto).where(EventPhoto.event_id == event.id))
    photos = list(rows.scalars().all())
    by_id = {photo.id: photo for photo in photos}
    if set(by_id.keys()) != set(photo_ids):
        raise HTTPException(
            status_code=400,
            detail="photo_ids must list every photo on this event exactly once",
        )

    for index, pid in enumerate(photo_ids):
        by_id[pid].sort_order = index

    await session.commit()
    return [by_id[pid] for pid in photo_ids]


# (backend, error type) pairs ``url_for`` has already logged a traceback for.
_PUBLIC_URL_FAILURES_LOGGED: set[tuple[str, str]] = set()


def _log_public_url_failure(backend_name: str, exc: Exception) -> None:
    marker = (backend_name, type(exc).__qualname__)
    if marker in _PUBLIC_URL_FAILURES_LOGGED:
        logger.debug("Direct photo URL unavailable on the %s backend: %r", backend_name, exc)
        return
    _PUBLIC_URL_FAILURES_LOGGED.add(marker)
    logger.warning(
        "Cannot build a direct photo URL on the %s backend; serving photos through the API",
        backend_name,
        exc_info=exc,
    )


def _storage_of(photo: EventPhoto) -> PhotoStorage | None:
    """The driver that can read this row's blob, or ``None`` if none can.

    ``storage_backend`` is recorded per row for exactly this: it names the store
    the key was written to, which an instance switched to the other backend can
    still read as long as it is configured. ``None`` for a row that names
    neither driver, and for a Figma row, which has no blob at all.

    Every failure to BUILD the driver answers ``None`` too, not an exception:
    ``GCSPhotoStorage`` raises without a bucket, and its client raises without
    credentials, so a stray ``gcs`` row on a local instance would otherwise turn
    every photo list into a 500 — the shape of tripl-0zpq.213. ``url_for`` then
    hands back the ``/file`` URL and ``read_blob`` answers a 409 that names the
    backend, which is a page that loads and an error that explains itself.
    """
    if photo.kind != PHOTO_KIND_PHOTO or not photo.storage_backend:
        return None
    try:
        return storage_for(photo.storage_backend)
    except Exception:
        logger.warning(
            "Photo %s names storage backend %r, which this instance cannot reach",
            photo.id,
            photo.storage_backend,
            exc_info=True,
        )
        return None


async def read_blob(photo: EventPhoto) -> bytes:
    """The photo's bytes, read through the backend the ROW names.

    Reading through the process's current driver instead sent every row written
    before a backend switch to the wrong store, where the key names nothing: a
    404 for all of them, with no hint that the switch was the cause
    (tripl-0zpq.295).
    """
    storage = _storage_of(photo)
    if storage is None or not photo.storage_key:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This photo is stored on the '{photo.storage_backend}' backend, which this "
                "instance cannot read. Restore that backend's configuration, or migrate the "
                "objects before switching."
            ),
        )
    return await storage.read(photo.storage_key)


async def url_for(photo: EventPhoto, slug: str) -> str:
    """Build the URL surfaced to clients for this photo.

    GCS returns a signed (or public) URL the browser can fetch directly.
    Local backend defers to the authenticated download endpoint exposed under
    the project router. Figma-kind rows simply return the embed URL the
    frontend iframes.
    """
    if photo.kind == PHOTO_KIND_FIGMA:
        return photo.external_url or ""

    # The row's OWN backend, not the one new uploads go to: an instance switched
    # from local to GCS (or back) still holds rows from the other store, and the
    # key only means anything there (tripl-0zpq.295). A row naming a backend
    # this build has no driver for keeps the /file URL, which says so properly.
    storage = _storage_of(photo)
    if photo.storage_key and storage is not None:
        try:
            external = await storage.public_url(photo.storage_key, photo.content_type)
        except Exception as exc:
            # The /file route below is the documented fallback when a direct URL
            # cannot be made, but it was only taken for a falsy return, which the
            # GCS driver never gives. Credentials that cannot sign — ADC on
            # Compute Engine or workload identity, gcloud user credentials —
            # raise instead, and that turned every photo list and every upload
            # response into a 500 (tripl-0zpq.213). /file reads through the
            # storage client, which those credentials can do. Logged once per
            # backend and error type: a canvas resolves every photo on every
            # list, and a traceback per photo per request says nothing new.
            _log_public_url_failure(storage.backend_name, exc)
        else:
            if external:
                return external

    return f"/api/v1/projects/{slug}/events/{photo.event_id}/photos/{photo.id}/file"
