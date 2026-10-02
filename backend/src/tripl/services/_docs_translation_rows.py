"""Row writes of docs translations shared by the API and the Celery task.

Neither function queries; each changes the ORM objects it is handed and asks
``add`` to stage the revision, so the async request path and the sync worker
write a translation the same way.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from tripl.models.doc_translation import DocTranslation, DocTranslationRevision

#: A run still ``pending`` after this long has died (a lost queue message, a
#: killed worker): it reads as ``failed`` and may be asked for again.
PENDING_STALE_AFTER = timedelta(minutes=30)
STALE_PENDING_ERROR = "The translation did not finish; try again"


def effective_status(translation: DocTranslation, now: datetime | None = None) -> str:
    """``status``, with a long-``pending`` run counted as ``failed``."""
    if translation.status != "pending":
        return translation.status
    updated = translation.updated_at
    if updated is None:
        return "pending"
    if updated.tzinfo is None:  # SQLite hands timestamps back naive
        updated = updated.replace(tzinfo=UTC)
    if (now or datetime.now(UTC)) - updated > PENDING_STALE_AFTER:
        return "failed"
    return "pending"


def apply_text(
    add: Callable[[object], None],
    translation: DocTranslation,
    *,
    content: str,
    source_revision: int,
    action: str,
    user_id: uuid.UUID | None,
) -> DocTranslationRevision:
    """Save ``content`` as the translation's next revision; it is ``ready`` after."""
    now = datetime.now(UTC)
    translation.content = content
    translation.content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    translation.revision = translation.revision + 1
    translation.source_revision = source_revision
    translation.machine = action == "translate"
    translation.status = "ready"
    translation.error = ""
    translation.updated_by = user_id
    translation.updated_at = now
    revision = DocTranslationRevision(
        id=uuid.uuid4(),
        translation_id=translation.id,
        number=translation.revision,
        action=action,
        content=content,
        source_revision=source_revision,
        author_id=user_id,
        created_at=now,
    )
    add(revision)
    return revision


def mark_failed(translation: DocTranslation, message: str) -> None:
    """The run produced no text; whatever text the translation had stays."""
    translation.status = "failed"
    translation.error = message[:1000]
    translation.updated_at = datetime.now(UTC)
