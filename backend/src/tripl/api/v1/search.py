from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from tripl.api.deps import BranchIdDep, CurrentUserDep, EditorUserDep, SessionDep
from tripl.schemas.search import SearchEntityType, SearchReindexResponse, SearchResponse
from tripl.services import search_service

router = APIRouter(prefix="/projects/{slug}/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search_project(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    current_user: CurrentUserDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    types: Annotated[list[SearchEntityType] | None, Query()] = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    semantic: Annotated[
        bool,
        Query(
            description=(
                "Run the embedding leg as well as the lexical one. Pass false for "
                "a keyword-only answer that skips the provider round trip; "
                "``semantic_used`` in the response is then always false."
            )
        ),
    ] = True,
    group_variants: Annotated[
        bool,
        Query(
            description=(
                "Fold events of one event type whose names differ only in one "
                "naming-rule placeholder into their best-ranked member, which "
                "then carries ``variant_group``. ``limit`` and ``total`` count "
                "the folded rows."
            )
        ),
    ] = False,
) -> SearchResponse:
    return await search_service.search_project(
        session,
        slug,
        q,
        branch_id=branch_id,
        entity_types=types,
        include_archived=include_archived,
        limit=limit,
        semantic=semantic,
        group_variants=group_variants,
        # Docs catalog notes hidden from the caller never reach the answer (F24).
        viewer=current_user,
    )


@router.post("/reindex", response_model=SearchReindexResponse)
async def reindex_project_search(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    _current_user: EditorUserDep,
) -> SearchReindexResponse:
    outcome = await search_service.reindex_branch(session, slug, branch_id)
    return SearchReindexResponse(
        documents_indexed=outcome.documents_indexed,
        embeddings_scheduled=outcome.embeddings_scheduled,
    )
