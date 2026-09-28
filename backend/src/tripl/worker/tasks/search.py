from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from celery import Task
from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from tripl.models.search_document import SearchDocument
from tripl.services import app_settings_service
from tripl.services._search_documents import (
    DOCUMENT_BUILDER_VERSION,
    EMBED_TEXT_MAX_CHARS,
    embed_text_for,
)
from tripl.services.app_settings_service import AiConfig
from tripl.services.embedding_service import embed_texts, embedding_provenance
from tripl.services.search_service import sanitize_embedding
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session
from tripl.worker.search_reindex import reindex_branch_from_worker

logger = logging.getLogger(__name__)

# Backwards-compatible alias; the recipe (and its rationale) lives next to the
# document builders in ``_search_documents`` so the demo fixture pipeline
# embeds byte-identical text.
_EMBED_TEXT_MAX_CHARS = EMBED_TEXT_MAX_CHARS

# Delay before retrying a batch whose embedding request failed outright.
_BATCH_RETRY_COUNTDOWN_SECONDS = 30


class _BatchEmbeddingFailedError(Exception):
    """The embedding request failed for the entire batch (network/HTTP error)."""


def _embed_text(doc: SearchDocument) -> str:
    """Build the text that gets embedded for one search document."""
    return embed_text_for(
        title=doc.title,
        subtitle=doc.subtitle,
        keywords=doc.keywords,
        body=doc.body,
    )


def _embed_documents(
    session: Session,
    docs: list[SearchDocument],
    ai_config: AiConfig,
) -> tuple[int, int]:
    """Embed a batch of documents; return ``(embedded, failed)`` counts.

    Raises :class:`_BatchEmbeddingFailedError` when ``embed_texts`` returns an
    empty list for a non-empty batch: that is a request-level failure
    (network/429/400) which says nothing about the individual documents, so
    none of them is touched — they stay ``pending`` for the caller to retry.

    Per-item behavior is unchanged: an embedding that fails sanitization marks
    that document ``failed``; a short response (fewer embeddings than
    documents) marks the unmatched tail ``failed``.
    """
    texts = [_embed_text(doc) for doc in docs]
    embeddings = embed_texts(texts, config=ai_config)
    if not embeddings:
        raise _BatchEmbeddingFailedError

    embedded = 0
    failed = 0
    for doc, raw_embedding in zip(docs, embeddings, strict=False):
        embedding = sanitize_embedding(raw_embedding)
        if not embedding:
            doc.embedding_status = "failed"
            failed += 1
            continue
        vector = "[" + ",".join(f"{value:.8f}" for value in embedding) + "]"
        session.execute(
            text(
                """
                UPDATE search_documents
                SET embedding = CAST(:embedding AS vector),
                    embedding_status = 'ready',
                    embedding_model = :embedding_model,
                    updated_at = now()
                WHERE id = :document_id
                """
            ),
            {
                "embedding": vector,
                "embedding_model": embedding_provenance(ai_config),
                "document_id": doc.id,
            },
        )
        embedded += 1

    if len(embeddings) < len(docs):
        for doc in docs[len(embeddings) :]:
            doc.embedding_status = "failed"
            failed += 1
    return embedded, failed


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.search.embed_search_documents",
    bind=True,
    max_retries=2,
)
def embed_search_documents(
    self: Task,
    project_id: str,
    branch_id: str,
    limit: int = 100,
) -> dict[str, int]:
    session = _get_sync_session()
    try:
        ai_config = app_settings_service.get_embedding_config_sync(session)
        if not ai_config.search_embeddings_enabled:
            return {"embedded": 0, "failed": 0}
        if session.get_bind().dialect.name != "postgresql":
            return {"embedded": 0, "failed": 0}

        docs = list(
            session.execute(
                select(SearchDocument)
                .where(
                    SearchDocument.project_id == uuid.UUID(project_id),
                    SearchDocument.branch_id == uuid.UUID(branch_id),
                    SearchDocument.embedding_status == "pending",
                )
                .order_by(SearchDocument.updated_at.asc(), SearchDocument.id.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        if not docs:
            return {"embedded": 0, "failed": 0}

        try:
            embedded, failed = _embed_documents(session, docs, ai_config)
        except _BatchEmbeddingFailedError:
            # The whole request failed — a transient outage, rate limit or a
            # rejected payload. Do NOT mark the documents failed: leave them
            # 'pending' and retry the task so a transient failure self-heals.
            try:
                raise self.retry(countdown=_BATCH_RETRY_COUNTDOWN_SECONDS) from None
            except MaxRetriesExceededError:
                # Retries exhausted: the docs stay 'pending' so a future
                # reindex or manual embedding run picks them up.
                logger.warning(
                    "Search embedding batch failed after retries; "
                    "leaving %d documents pending (project=%s branch=%s)",
                    len(docs),
                    project_id,
                    branch_id,
                )
                return {"embedded": 0, "failed": 0}

        session.commit()
        remaining = session.scalar(
            select(SearchDocument.id)
            .where(
                SearchDocument.project_id == uuid.UUID(project_id),
                SearchDocument.branch_id == uuid.UUID(branch_id),
                SearchDocument.embedding_status == "pending",
            )
            .limit(1)
        )
        if remaining is not None:
            embed_search_documents.delay(project_id, branch_id, limit)
        return {"embedded": embedded, "failed": failed}
    except Retry:
        raise
    except Exception:
        session.rollback()
        logger.exception("Failed to embed search documents")
        raise
    finally:
        session.close()


# How many (project, branch) pairs one sweep pass rebuilds.
#
# Deliberately small. A rebuild reads the whole branch — acme-ios carries eight
# working branches at 3200-4100 documents each — so an unbounded sweep would turn
# one builder bump into a stampede against the same database the API is serving
# from. Two per pass against the schedule below drains a 10-branch instance
# inside an hour, which is the same order as the delay main already has (it waits
# for the next scan).
STALE_REINDEX_BRANCHES_PER_RUN = 2


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.search.reindex_stale_search_documents",
)
def reindex_stale_search_documents() -> dict[str, int]:
    """Rebuild branches with stale builders or embedding provenance.

    WHY THIS EXISTS (tripl-uji9)
    ----------------------------
    A change to how documents are BUILT reaches a main branch on its own: the
    worker reindexes main after every scan and every metrics collection. Nothing
    does that for a working branch — it is rebuilt only when somebody edits its
    content — so a builder change split the corpus into two generations and left
    it that way.

    That is not hypothetical. Eight days after the keywords fix shipped, measured
    on production: all three main branches were correct, and eight acme-ios
    working branches still held 7117 documents built by the previous generation,
    ranking them by text the fix had already removed.

    WHAT IT COSTS, AND WHAT IT DOES NOT
    -----------------------------------
    Unchanged documents keep their vectors when their provenance matches.
    Switching the model or endpoint invalidates old vectors and schedules fresh
    embedding on every branch, including working branches.
    """
    session = _get_sync_session()
    try:
        ai_config = app_settings_service.get_embedding_config_sync(session)
        stale_embedding = and_(
            SearchDocument.embedding_status == "ready",
            or_(
                SearchDocument.embedding_model.is_(None),
                SearchDocument.embedding_model != embedding_provenance(ai_config),
            ),
        )
        stale = SearchDocument.builder_version < DOCUMENT_BUILDER_VERSION
        if ai_config.search_embeddings_enabled:
            stale = or_(stale, stale_embedding)
        pairs = session.execute(
            select(SearchDocument.project_id, SearchDocument.branch_id)
            .where(stale)
            .distinct()
            .limit(STALE_REINDEX_BRANCHES_PER_RUN)
        ).all()
        for project_id, branch_id in pairs:
            reindex_branch_from_worker(session, project_id, branch_id)
        return {"branches_reindexed": len(pairs)}
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.search.reindex_search_branch",
)
def reindex_search_branch(project_id: str, branch_id: str) -> dict[str, int]:
    """Rebuild ONE named branch's index, off the request path (tripl-zbv0).

    This is what the search read path enqueues the first time this process
    searches a branch that has never been indexed (see
    ``search_service._ensure_index_exists``); before it existed the read path had
    nothing to hand the work to and built the index inline, inside a GET.

    The sweep above cannot stand in for it: it selects on ``builder_version``, so
    it only ever sees branches that already have rows, and a never-indexed branch
    has none. Nothing here is scheduled by beat — one enqueue per branch, from
    the reader that noticed.
    """
    session = _get_sync_session()
    try:
        reindex_branch_from_worker(session, uuid.UUID(project_id), uuid.UUID(branch_id))
        return {"branches_reindexed": 1}
    finally:
        session.close()


# Docs stuck in `pending` longer than this are stranded: the follow-up queue
# message was lost (broker/worker down at enqueue time) or the batch retries
# were exhausted. The beat chaser below re-queues one embed task per branch.
STRANDED_EMBEDDING_MINUTES = 15


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.search.requeue_stranded_search_embeddings",
)
def requeue_stranded_search_embeddings() -> dict[str, int]:
    """Periodic safety net for the event-driven embedding pipeline.

    Embeddings normally refresh via the task queued after every reindex (API
    CRUD, worker post-scan/metrics, post-merge). That message can be lost —
    broker down at enqueue time, worker killed mid-batch, batch retries
    exhausted — leaving documents ``pending`` with nothing chasing them. This
    beat task re-queues one embed task per (project, branch) that still has
    pending documents older than the stranded horizon; fresh pending docs are
    skipped so in-flight batches are not double-processed.
    """
    session = _get_sync_session()
    try:
        ai_config = app_settings_service.get_embedding_config_sync(session)
        if not ai_config.search_embeddings_enabled:
            return {"branches_requeued": 0}
        cutoff = datetime.now(UTC) - timedelta(minutes=STRANDED_EMBEDDING_MINUTES)
        pairs = session.execute(
            select(SearchDocument.project_id, SearchDocument.branch_id)
            .where(
                SearchDocument.embedding_status == "pending",
                SearchDocument.updated_at < cutoff,
            )
            .distinct()
        ).all()
        for project_id, branch_id in pairs:
            embed_search_documents.delay(str(project_id), str(branch_id))
        return {"branches_requeued": len(pairs)}
    finally:
        session.close()
