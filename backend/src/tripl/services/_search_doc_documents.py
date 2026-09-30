"""Search documents for docs catalog notes (F22, GH #299).

A project's index carries its own notes and its organization's. Notes are not
plan-branched, so, like metrics and fact tables, every branch's index holds a
copy; ``_search_documents.build_documents`` appends these at the end.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile, DocLink
from tripl.models.project import Project
from tripl.services._search_documents import BuiltDocument, _join, _spaced_identifiers
from tripl.services.docs_frontmatter import split_frontmatter
from tripl.services.docs_links import ID_KINDS, rewrite_links_as_text
from tripl.services.project_links import legacy_project_path

#: How much of a note's body is indexed. The embed text is capped separately
#: (``EMBED_TEXT_MAX_CHARS``); this keeps the stored tsvector bounded too.
DOC_BODY_MAX_CHARS = 20_000

_SCOPE_LABEL = {"project": "Project notes", "organization": "Organization notes"}


def _body(content: str) -> str:
    _yaml, body = split_frontmatter(content)
    return rewrite_links_as_text(body)[:DOC_BODY_MAX_CHARS]


async def build_doc_documents(
    session: AsyncSession, project_id: uuid.UUID, slug: str
) -> list[BuiltDocument]:
    organization_id = await session.scalar(
        select(Project.organization_id).where(Project.id == project_id)
    )
    scope_clause = (
        or_(DocFile.project_id == project_id, DocFile.organization_id == organization_id)
        if organization_id is not None
        else DocFile.project_id == project_id
    )
    docs = list(
        await session.scalars(
            select(DocFile)
            .where(scope_clause)
            .order_by(DocFile.organization_id.is_not(None), DocFile.path_key)
        )
    )
    if not docs:
        return []
    link_targets: dict[uuid.UUID, list[str]] = {}
    # Names only: a note, alert rule or person is linked by id, which is noise
    # to the index (and a note's id must not carry its title anywhere).
    rows = await session.execute(
        select(DocLink.doc_file_id, DocLink.target)
        .where(
            DocLink.doc_file_id.in_([doc.id for doc in docs]),
            DocLink.kind.not_in(sorted(ID_KINDS)),
        )
        .order_by(DocLink.doc_file_id, DocLink.kind, DocLink.target)
    )
    for doc_id, target in rows:
        link_targets.setdefault(doc_id, []).append(target)

    documents: list[BuiltDocument] = []
    for doc in docs:
        scope = "project" if doc.project_id is not None else "organization"
        segments = [
            segment[:-3] if segment.lower().endswith(".md") else segment
            for segment in doc.path.split("/")
        ]
        documents.append(
            BuiltDocument(
                entity_type="doc",
                entity_id=doc.id,
                parent_event_id=None,
                title=doc.title,
                subtitle=f"{_SCOPE_LABEL[scope]} · {doc.path}",
                description=doc.description,
                body=_body(doc.content),
                keywords=_join(
                    [
                        " ".join(doc.tags or []),
                        " ".join(segments),
                        _spaced_identifiers(segments),
                        " ".join(link_targets.get(doc.id, [])),
                        doc.audience,
                    ]
                ),
                route_path=legacy_project_path(slug, f"/docs/{scope}/{doc.path}"),
            )
        )
    return documents
