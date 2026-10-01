"""Make a docs translation with the organization's AI model (``services.docs_translations``).

The request path wrote a ``pending`` row and queued this task with its id. The
task reads the note as it is NOW, so the text matches the revision it records
as ``source_revision``; the model calls run with no transaction open, and the
result lands only if the row is still the ``pending`` run that queued it (a
deleted note or translation, or a person's save in between, wins).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from tripl.models.doc_file import DocFile
from tripl.models.doc_translation import DocTranslation
from tripl.models.project import Project
from tripl.services import app_settings_service, llm_service
from tripl.services._docs_translation_rows import apply_text, mark_failed
from tripl.services.docs_paths import content_bytes
from tripl.services.docs_translate_text import TranslationError, translate_content
from tripl.services.docs_translations import AI_OFF, MAX_TRANSLATION_BYTES, model_complete
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session

logger = logging.getLogger(__name__)


def _org_of(session: Session, doc: DocFile) -> uuid.UUID | None:
    if doc.organization_id is not None:
        return doc.organization_id
    project = session.get(Project, doc.project_id)
    return project.organization_id if project else None


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.docs_translate.translate_doc",
    acks_late=True,
)
def translate_doc(translation_id: str) -> str:
    tid = uuid.UUID(translation_id)
    with _get_sync_session() as session:
        translation = session.get(DocTranslation, tid)
        if translation is None or translation.status != "pending":
            return "skipped"
        doc = session.get(DocFile, translation.doc_file_id)
        if doc is None:
            return "skipped"
        marker = translation.updated_at
        lang, content, path, source_revision = translation.lang, doc.content, doc.path, doc.revision
        config = app_settings_service.get_ai_config_sync(session, org_id=_org_of(session, doc))
        session.rollback()  # no transaction held across the model calls

    error: str | None = None
    translated = ""
    if not llm_service.is_enabled(config):
        error = AI_OFF
    else:
        try:
            translated = translate_content(content, path, lang, model_complete(config))
            if content_bytes(translated) > MAX_TRANSLATION_BYTES:
                error = f"The translation is larger than {MAX_TRANSLATION_BYTES // 1024} KiB"
        except TranslationError as exc:
            error = str(exc)
        except Exception:
            logger.exception("Translating %s failed", translation_id)
            error = "The translation failed unexpectedly; try again"

    with _get_sync_session() as session:
        translation = session.get(DocTranslation, tid, with_for_update=True)
        if (
            translation is None
            or translation.status != "pending"
            or translation.updated_at != marker
        ):
            session.rollback()
            return "superseded"
        if error is not None:
            mark_failed(translation, error)
            session.commit()
            return "failed"
        apply_text(
            session.add,
            translation,
            content=translated,
            source_revision=source_revision,
            action="translate",
            user_id=translation.requested_by,
        )
        session.commit()
    return "ready"
