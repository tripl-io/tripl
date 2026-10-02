"""Stored translations of docs catalog notes: asking for one, editing, serving.

A translation is asked for once (:func:`request_translation`): the language
the person typed is turned into a tag (by the model when it is a name), a
``pending`` row is written and a Celery task (``worker/tasks/docs_translate``)
makes the text with the organization's AI key. From then on it is stored text:
people edit it (:func:`write_translation`), every save is a revision, and it
never follows the original by itself. Reading it is :func:`served`, which picks
what a read of a note returns:

* a read that names a language gets that translation, even when it is behind
  the original (``translation_outdated``), or the original with
  ``translation_fallback`` saying why;
* a read that names none gets the project's AGENT default
  (``projects.docs_agent_lang``) when it is up to date, else the original;
* ``original`` is the original.

The app always names what it shows (the person's choice, or the project's
human default), so "names none" is an agent's read. A translation has no
access of its own: whoever may read (or edit) the note may read (or edit) its
translations.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.models.doc_translation import DocTranslation, DocTranslationRevision
from tripl.models.project import Project
from tripl.schemas.docs import (
    DocLanguageDefaults,
    DocLanguageDefaultsUpdate,
    DocTranslateRequest,
    DocTranslationFallback,
    DocTranslationRevisionDetail,
    DocTranslationRevisionSummary,
    DocTranslationSummary,
    DocTranslationWrite,
)
from tripl.services import _docs_store as store
from tripl.services import app_settings_service, audit_service, llm_service, project_lookup
from tripl.services._celery_dispatch import dispatch
from tripl.services._docs_translation_rows import (
    STALE_PENDING_ERROR,
    apply_text,
    effective_status,
)
from tripl.services.app_settings_service import AiConfig
from tripl.services.docs_access import (
    DocCaller,
    require_doc_writer,
    require_editable,
    require_readable,
)
from tripl.services.docs_frontmatter import DocContentError, parse_frontmatter
from tripl.services.docs_paths import (
    MAX_FILE_BYTES,
    DocPathError,
    DocScope,
    content_bytes,
    normalize_doc_path,
)
from tripl.services.docs_translate_text import (
    CHUNK_MAX_TOKENS,
    Complete,
    TranslationError,
    is_plain_code,
    normalize_lang,
    resolve_lang,
)

logger = logging.getLogger(__name__)

#: The ``lang`` a read passes for the original.
ORIGINAL = "original"
#: A translation may outgrow its original: Cyrillic or CJK text takes two to
#: three bytes a character where English took one.
MAX_TRANSLATION_BYTES = 3 * MAX_FILE_BYTES
TASK_NAME = "tripl.worker.tasks.docs_translate.translate_doc"
AI_OFF = "AI is not set up for this organization; an admin can add a key in Settings > AI"
TRANSLATION_NOT_FOUND = "Translation not found"
BEING_MADE = "The translation is being made; wait for it to finish"


def model_complete(config: AiConfig, *, max_tokens: int = CHUNK_MAX_TOKENS) -> Complete:
    """``complete(system, user)`` on the organization's model, for the text module."""
    return functools.partial(_complete, config, max_tokens)


def _complete(config: AiConfig, max_tokens: int, system: str, user: str) -> str | None:
    return llm_service.complete(system, user, max_tokens=max_tokens, config=config)


async def _ai_config(session: AsyncSession, project: Project) -> AiConfig:
    return await app_settings_service.get_ai_config(session, org_id=project.organization_id)


async def _enqueue(translation_id: uuid.UUID) -> None:
    from tripl.worker.celery_app import celery_app

    await dispatch(celery_app.send_task, TASK_NAME, args=[str(translation_id)])


def _path(raw: str) -> str:
    try:
        return normalize_doc_path(raw)
    except DocPathError as exc:
        raise store.unprocessable(exc) from exc


def lang_param(raw: str) -> str:
    """A stored tag from a ``lang`` the client sent; 422 when it is not one."""
    tag = normalize_lang(raw)
    if tag is None:
        raise HTTPException(
            status_code=422, detail=f"'{raw}' is not a language code such as 'en' or 'pt-br'"
        )
    return tag


async def _resolve(session: AsyncSession, project: Project, raw: str) -> str:
    """What the person typed as a tag: a code as is, a name through the model."""
    if is_plain_code(raw):
        return lang_param(raw)
    config = await _ai_config(session, project)
    if not llm_service.is_enabled(config):
        raise HTTPException(
            status_code=422,
            detail=f"Type a language code such as 'en' ('{raw}' needs the AI model, which is off)",
        )
    # End the read transaction: no connection is held open across the call.
    # The caller re-reads what it is about to change.
    await session.commit()
    try:
        return await asyncio.to_thread(resolve_lang, raw, model_complete(config, max_tokens=100))
    except TranslationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _find(
    session: AsyncSession, doc_id: uuid.UUID, lang: str, *, for_update: bool = False
) -> DocTranslation | None:
    query = select(DocTranslation).where(
        DocTranslation.doc_file_id == doc_id, DocTranslation.lang == lang
    )
    if for_update:
        query = query.with_for_update()
    found: DocTranslation | None = await session.scalar(query)
    return found


async def _get(session: AsyncSession, doc_id: uuid.UUID, lang: str) -> DocTranslation:
    found = await _find(session, doc_id, lang)
    if found is None:
        raise HTTPException(status_code=404, detail=TRANSLATION_NOT_FOUND)
    return found


async def translations_of(session: AsyncSession, doc_id: uuid.UUID) -> list[DocTranslation]:
    rows = await session.scalars(
        select(DocTranslation)
        .where(DocTranslation.doc_file_id == doc_id)
        .order_by(DocTranslation.lang)
    )
    return list(rows)


def summary(
    translation: DocTranslation, doc: DocFile, names: dict[uuid.UUID, str]
) -> DocTranslationSummary:
    status = effective_status(translation)
    error = translation.error
    if status == "failed" and translation.status == "pending":
        error = STALE_PENDING_ERROR
    return DocTranslationSummary(
        lang=translation.lang,
        status=status,
        revision=translation.revision,
        source_revision=translation.source_revision,
        outdated=translation.revision > 0 and translation.source_revision < doc.revision,
        machine=translation.machine,
        error=error,
        updated_at=translation.updated_at,
        updated_by_name=names.get(translation.updated_by) if translation.updated_by else None,
    )


async def summaries(
    session: AsyncSession, doc: DocFile, rows: list[DocTranslation]
) -> list[DocTranslationSummary]:
    names = await store.user_names(session, [row.updated_by for row in rows])
    return [summary(row, doc, names) for row in rows]


def language_defaults(project: Project) -> DocLanguageDefaults:
    return DocLanguageDefaults(
        agent_lang=project.docs_agent_lang, human_lang=project.docs_human_lang
    )


@dataclass(frozen=True)
class Served:
    """What a read of a note returns, translation-wise (see the module docstring)."""

    translation: DocTranslation | None
    requested: str | None
    fallback: DocTranslationFallback | None
    outdated: bool
    rows: list[DocTranslation]


async def served(session: AsyncSession, project: Project, doc: DocFile, lang: str | None) -> Served:
    rows = await translations_of(session, doc.id)
    explicit = lang is not None
    if lang is None:
        requested = project.docs_agent_lang
    elif lang.strip().lower() == ORIGINAL:
        requested = None
    else:
        requested = lang_param(lang)
    if requested is None:
        return Served(None, None, None, False, rows)
    found = next((row for row in rows if row.lang == requested), None)
    if found is None or found.revision == 0:
        fallback: DocTranslationFallback = "missing"
        if found is not None:
            fallback = "pending" if effective_status(found) == "pending" else "failed"
        return Served(None, requested, fallback, False, rows)
    outdated = found.source_revision < doc.revision
    if outdated and not explicit:
        return Served(None, requested, "outdated", False, rows)
    return Served(found, requested, None, outdated, rows)


async def _editable_doc(
    session: AsyncSession, slug: str, scope: DocScope, path: str, caller: DocCaller
) -> tuple[Project, DocFile]:
    project = await project_lookup.resolve_project(session, slug)
    await require_doc_writer(session, caller, scope, project.organization_id)
    doc = await store.get_doc(session, project, scope, _path(path))
    await require_editable(session, caller, project, doc)
    return project, doc


async def _readable_doc(
    session: AsyncSession, slug: str, scope: DocScope, path: str, caller: DocCaller
) -> tuple[Project, DocFile]:
    project = await project_lookup.resolve_project(session, slug)
    doc = await store.get_doc(session, project, scope, _path(path))
    await require_readable(session, caller, project, doc, what="translation")
    return project, doc


async def _single_summary(
    session: AsyncSession, doc: DocFile, translation: DocTranslation
) -> DocTranslationSummary:
    names = await store.user_names(session, [translation.updated_by])
    return summary(translation, doc, names)


async def request_translation(
    session: AsyncSession, slug: str, body: DocTranslateRequest, caller: DocCaller
) -> DocTranslationSummary:
    """Queue an AI translation of the note into the language typed."""
    project, doc = await _editable_doc(session, slug, body.scope, body.path, caller)
    config = await _ai_config(session, project)
    if not llm_service.is_enabled(config):
        raise HTTPException(status_code=409, detail=AI_OFF)
    if is_plain_code(body.language):
        lang = lang_param(body.language)
    else:
        lang = await _resolve(session, project, body.language)
        # The model call ended the transaction: check the note and the access again.
        project, doc = await _editable_doc(session, slug, body.scope, body.path, caller)
    user = caller.user
    now = datetime.now(UTC)
    translation = await _find(session, doc.id, lang, for_update=True)
    if translation is not None:
        if effective_status(translation, now) == "pending":
            raise HTTPException(
                status_code=409, detail=f"The note is already being translated into {lang}"
            )
        if translation.revision > 0 and not translation.machine and not body.overwrite:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "translation_edited",
                    "lang": lang,
                    "message": (
                        f"The {lang} translation has been edited by hand. Translating again "
                        "replaces it; the edited text stays in its history."
                    ),
                },
            )
        translation.status = "pending"
        translation.error = ""
        translation.requested_by = user.id
        translation.updated_at = now
    else:
        translation = DocTranslation(
            id=uuid.uuid4(),
            doc_file_id=doc.id,
            lang=lang,
            status="pending",
            content="",
            content_sha256="",
            source_revision=doc.revision,
            revision=0,
            machine=True,
            error="",
            requested_by=user.id,
            created_at=now,
            updated_at=now,
        )
        session.add(translation)
    await audit_service.record(
        session,
        user=user,
        action="doc.translate",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={"scope": body.scope, "path": doc.path, "lang": lang, "revision": doc.revision},
        commit=False,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"The note is already being translated into {lang}"
        ) from exc
    try:
        await _enqueue(translation.id)
    except Exception:
        logger.warning("Failed to queue the translation %s", translation.id, exc_info=True)
        translation.status = "failed"
        translation.error = "Could not queue the translation; try again"
        await session.commit()
    return await _single_summary(session, doc, translation)


async def write_translation(
    session: AsyncSession, slug: str, body: DocTranslationWrite, caller: DocCaller
) -> DocTranslationSummary:
    """Save a person's text as the translation (creating it when there is none)."""
    project, doc = await _editable_doc(session, slug, body.scope, body.path, caller)
    lang = lang_param(body.lang)
    if content_bytes(body.content) > MAX_TRANSLATION_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Translation is larger than {MAX_TRANSLATION_BYTES // 1024} KiB",
        )
    try:
        parse_frontmatter(body.content, doc.path)
    except DocContentError as exc:
        raise store.unprocessable(exc) from exc
    translation = await _find(session, doc.id, lang, for_update=True)
    if translation is None:
        now = datetime.now(UTC)
        translation = DocTranslation(
            id=uuid.uuid4(),
            doc_file_id=doc.id,
            lang=lang,
            status="ready",
            content="",
            content_sha256="",
            source_revision=doc.revision,
            revision=0,
            machine=False,
            error="",
            requested_by=caller.user.id,
            created_at=now,
            updated_at=now,
        )
        session.add(translation)
    elif effective_status(translation) == "pending":
        raise HTTPException(status_code=409, detail=BEING_MADE)
    if body.base_revision is not None and translation.revision != body.base_revision:
        raise HTTPException(
            status_code=409, detail="The translation changed concurrently; reload and retry"
        )
    unchanged = translation.revision > 0 and translation.content == body.content
    if unchanged and not body.mark_current:
        return await _single_summary(session, doc, translation)
    keeps_source = translation.revision > 0 and not body.mark_current
    apply_text(
        session.add,
        translation,
        content=body.content,
        source_revision=translation.source_revision if keeps_source else doc.revision,
        action="edit",
        user_id=caller.user.id,
    )
    await audit_service.record(
        session,
        user=caller.user,
        action="doc.translation_edit",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={"scope": body.scope, "path": doc.path, "lang": lang},
        commit=False,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise store.concurrent_write() from exc
    return await _single_summary(session, doc, translation)


async def delete_translation(
    session: AsyncSession, slug: str, scope: DocScope, path: str, lang: str, caller: DocCaller
) -> None:
    project, doc = await _editable_doc(session, slug, scope, path, caller)
    translation = await _get(session, doc.id, lang_param(lang))
    await audit_service.record(
        session,
        user=caller.user,
        action="doc.translation_delete",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={"scope": scope, "path": doc.path, "lang": translation.lang},
        commit=False,
    )
    await session.delete(translation)
    await session.commit()


def _revision_summary(
    revision: DocTranslationRevision, names: dict[uuid.UUID, str]
) -> DocTranslationRevisionSummary:
    return DocTranslationRevisionSummary(
        id=revision.id,
        number=revision.number,
        action=revision.action,
        source_revision=revision.source_revision,
        author_name=names.get(revision.author_id) if revision.author_id else None,
        created_at=revision.created_at,
    )


async def list_revisions(
    session: AsyncSession, slug: str, scope: DocScope, path: str, lang: str, caller: DocCaller
) -> list[DocTranslationRevisionSummary]:
    _project, doc = await _readable_doc(session, slug, scope, path, caller)
    translation = await _get(session, doc.id, lang_param(lang))
    rows = list(
        await session.scalars(
            select(DocTranslationRevision)
            .where(DocTranslationRevision.translation_id == translation.id)
            .order_by(DocTranslationRevision.number.desc())
        )
    )
    names = await store.user_names(session, [row.author_id for row in rows])
    return [_revision_summary(row, names) for row in rows]


async def _revision_in(
    session: AsyncSession, doc: DocFile, revision_id: uuid.UUID
) -> tuple[DocTranslation, DocTranslationRevision]:
    row = await session.get(DocTranslationRevision, revision_id)
    translation = await session.get(DocTranslation, row.translation_id) if row else None
    if row is None or translation is None or translation.doc_file_id != doc.id:
        raise HTTPException(status_code=404, detail="Translation revision not found")
    return translation, row


async def get_revision(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    path: str,
    revision_id: uuid.UUID,
    caller: DocCaller,
) -> DocTranslationRevisionDetail:
    _project, doc = await _readable_doc(session, slug, scope, path, caller)
    _translation, row = await _revision_in(session, doc, revision_id)
    names = await store.user_names(session, [row.author_id])
    return DocTranslationRevisionDetail(
        **_revision_summary(row, names).model_dump(), content=row.content
    )


async def restore_revision(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    path: str,
    revision_id: uuid.UUID,
    caller: DocCaller,
) -> DocTranslationSummary:
    """Save an old revision's text again, as it was (with its source revision)."""
    project, doc = await _editable_doc(session, slug, scope, path, caller)
    translation, row = await _revision_in(session, doc, revision_id)
    if effective_status(translation) == "pending":
        raise HTTPException(status_code=409, detail=BEING_MADE)
    apply_text(
        session.add,
        translation,
        content=row.content,
        source_revision=row.source_revision,
        action="restore",
        user_id=caller.user.id,
    )
    await audit_service.record(
        session,
        user=caller.user,
        action="doc.translation_restore",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={"scope": scope, "path": doc.path, "lang": translation.lang, "number": row.number},
        commit=False,
    )
    await session.commit()
    return await _single_summary(session, doc, translation)


async def get_language_defaults(session: AsyncSession, slug: str) -> DocLanguageDefaults:
    return language_defaults(await project_lookup.resolve_project(session, slug))


async def update_language_defaults(
    session: AsyncSession, slug: str, body: DocLanguageDefaultsUpdate, caller: DocCaller
) -> DocLanguageDefaults:
    """Set both defaults; a name goes through the model, empty means the original."""
    project = await project_lookup.resolve_project(session, slug)
    await require_doc_writer(session, caller, "project", project.organization_id)
    values: dict[str, str | None] = {}
    for field in ("agent_lang", "human_lang"):
        raw = getattr(body, field)
        if raw is None or not raw.strip() or raw.strip().lower() == ORIGINAL:
            values[field] = None
        else:
            values[field] = await _resolve(session, project, raw)
    if any(raw and not is_plain_code(raw) for raw in (body.agent_lang, body.human_lang)):
        # A name went to the model, which ended the transaction: look again.
        project = await project_lookup.resolve_project(session, slug)
        await require_doc_writer(session, caller, "project", project.organization_id)
    before = language_defaults(project)
    project.docs_agent_lang = values["agent_lang"]
    project.docs_human_lang = values["human_lang"]
    await audit_service.record(
        session,
        user=caller.user,
        action="project.docs_languages",
        target_type="project",
        target_id=project.id,
        target_name=project.slug,
        project=project,
        payload={"before": before.model_dump(), "after": values},
        commit=False,
    )
    await session.commit()
    return language_defaults(project)
