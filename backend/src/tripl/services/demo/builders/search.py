"""Search builder: index the seeded plan for this branch.

Runs last so every seeded entity is indexable. No commit — the caller owns the
phase-2 transaction boundary, so ``commit=False`` is load-bearing: the default
reindex commits twice, which used to persist the whole seed before
``create_demo_project`` checked for a cancel. Embeddings are not
scheduled (no worker in the demo path).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services.demo.scenario import DemoContext
from tripl.services.search_service import reindex_project_branch


async def build_search(session: AsyncSession, ctx: DemoContext) -> None:
    await reindex_project_branch(
        session,
        project_id=ctx.project_id,
        branch_id=ctx.branch_id,
        slug=ctx.slug,
        schedule_embeddings=False,
        commit=False,
    )
