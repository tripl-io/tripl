"""Search over the docs catalog (F22, GH #299).

No ranking of its own: notes are ``doc`` documents in the project's hybrid
search index (``services/_search_doc_documents.py``), so this asks
``search_service.search_project`` for that one kind on the MAIN branch and maps
the hits back to notes. A hit whose note has gone since the index was built is
dropped rather than served.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.schemas.docs import DocSearchHit, DocSearchResponse
from tripl.services import _docs_store as store
from tripl.services import search_service
from tripl.services.docs_paths import DocScope
from tripl.services.docs_service import _resolve_project


async def search_docs(
    session: AsyncSession,
    slug: str,
    q: str,
    *,
    scope: DocScope | None = None,
    limit: int = 20,
) -> DocSearchResponse:
    project = await _resolve_project(session, slug)
    found = await search_service.search_project(
        session, project.slug, q, entity_types=["doc"], limit=limit * 2, project_id=project.id
    )
    ids = [item.entity_id for item in found.items]
    docs: dict[object, DocFile] = {}
    if ids:
        docs = {
            doc.id: doc
            for doc in await session.scalars(
                select(DocFile).where(DocFile.id.in_(ids), store.any_scope_filter(project))
            )
        }
    hits: list[DocSearchHit] = []
    for item in found.items:
        doc = docs.get(item.entity_id)
        if doc is None:
            continue
        doc_scope = store.scope_of(doc)
        if scope is not None and doc_scope != scope:
            continue
        hits.append(
            DocSearchHit(
                scope=doc_scope,
                path=doc.path,
                title=doc.title,
                description=doc.description,
                tags=list(doc.tags or []),
                audience=store.as_audience(doc.audience),
                snippet=item.snippet,
                score=item.score,
                confidence=item.confidence,
            )
        )
    page = hits[:limit]
    return DocSearchResponse(
        items=page,
        total=len(page),
        truncated=found.truncated or len(hits) > limit,
        semantic_used=found.semantic_used,
    )
