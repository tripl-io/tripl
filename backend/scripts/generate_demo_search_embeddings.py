"""Generate the compressed demo search-embedding fixture.

Seeds the full demo scenario into an in-memory SQLite database, once in each
state of the calendar's property type change, builds the search documents
for BOTH demo branches (main + the deep-copied feature branch, whose single
edited event description must land in the fixture too), embeds the deduped
texts plus a curated query list via the OpenAI embedding API, and writes
``search_embeddings_v5.npz`` into
``src/tripl/services/demo/fixtures/`` together with its
``.identity.json`` sidecar (the content-independent fallback index — see
``services.demo.search_embeddings``; both files must ship together).

Usage (needs OPENAI_API_KEY or SEARCH_EMBEDDING_API_KEY in the env):

    cd backend && uv run python scripts/generate_demo_search_embeddings.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tripl.config import settings
from tripl.middleware.org_context import OrgRef, bound_org
from tripl.models import Base
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    default_organization_values,
)
from tripl.models.plan_branch import PlanBranch
from tripl.services._search_documents import build_documents, embed_text_for
from tripl.services.app_settings_service import env_ai_config
from tripl.services.demo.scenario import DEMO_RECIPE_VERSION
from tripl.services.demo.search_embeddings import (
    DEFAULT_FIXTURE_PATH,
    embed_text_key,
    identity_key,
    identity_sidecar_path,
    normalize_demo_query,
)
from tripl.services.embedding_service import embed_texts
from tripl.services.search_service import sanitize_embedding

# Curated demo queries whose vectors ship in the fixture. Keep in sync with
# anything the product surfaces as suggested demo searches: the coached chapter
# and the tour advertise "purchase funnel" and "money back"
# (frontend/src/demo/scenarioModel.ts, tourSteps.ts). The rest are what a
# visitor to a recipes app's plan tends to type.
DEMO_SEARCH_QUERIES: tuple[str, ...] = (
    "purchase funnel",
    "money back",
    "why did revenue drop",
    "subscription lifecycle",
    "user identity",
    "onboarding drop-off",
    "screen where users pay",
    "promo code redemption",
    "saving a recipe",
    "weekly meal plan",
    "push notification opt-in",
    "app crashed",
    "shopping list",
    "free trial ends",
)

DEFAULT_BATCH_SIZE = 64

# Injectable so tests can drive the script end-to-end with a fake embedder.
Embedder = Callable[[list[str]], list[list[float]]]


def _pinned_type_change(sends_string: bool) -> Callable[[str], bool]:
    """``calendar.property_type_broken`` for one build: every release, or none."""

    def _broken(_version: str) -> bool:
        return sends_string

    return _broken


@dataclass(frozen=True)
class GeneratedFixture:
    path: Path
    model: str
    dimensions: int
    doc_count: int
    query_count: int


def _openai_embedder() -> Embedder:
    """Real embedder: env config with embeddings force-enabled for this run."""
    config = replace(env_ai_config(), search_embeddings_enabled=True)
    if not config.search_embedding_api_key:
        msg = "No embedding API key configured: set OPENAI_API_KEY or SEARCH_EMBEDDING_API_KEY"
        raise SystemExit(msg)

    def _embed(texts: list[str]) -> list[list[float]]:
        return embed_texts(texts, config=config)

    return _embed


@dataclass(frozen=True)
class CollectedTexts:
    """Deduped embed texts plus the identity -> embed-key sidecar mapping."""

    texts: list[str]
    identity_keys: dict[str, str]


async def _collect_demo_embed_texts(session: AsyncSession) -> CollectedTexts:
    """Seed a demo project and return its deduped embed texts (both branches).

    Also builds the ``identity key -> embed-text key`` map that ships beside the
    archive, so a document whose text the real scan/collection
    pipeline later rewrites still resolves to its own vector. MAIN wins every
    identity: a working branch deep-copies the plan, so both branches produce the
    same identity, and main's text is the canonical one.

    The texts are read twice: as the demo opens (two days observed; the
    branches are snapshotted then), and once its history fill has replayed
    the rest. Identities map to the texts as it opens, the state every build
    shares, a short-history one included; the filled texts add vectors for
    what a visitor searches minutes later, and anything they rewrite after
    that falls back to the identity.

    And the demo is built twice, once in each state of the calendar's property
    type change: every release sending ``results_count`` as a string on iOS,
    then none. Which state a real build is in depends on its hour. Its first
    live collection types the variable from a sample of rows: ``number`` when
    no row of a release that sends the string is in it, and still ``string``
    when one is, a mixed sample having no type. Such releases alternate every
    fortnight, and the share of one drains for days after the next ships, so
    both states come round every fortnight. Built once, in a ``string`` hour
    as the shipped archive was, the fixture had no vector for the variable of
    a demo built in a ``number`` one.
    """
    from tripl.core.demo_warehouse import calendar, rows
    from tripl.services import demo_history_service, demo_service

    # A script has no request organization; the demo seed resolves its project
    # by slug, so bind the default organization for that call (F20 PR2).
    # The seed and the history fill normally run on the worker; here they run
    # inline on this session, on the in-memory warehouse engine, so the script
    # needs neither a broker nor the PostgreSQL warehouse and every run indexes
    # the same rows. The fill is held until the opening texts are read.
    async def _seed_inline(
        caller: AsyncSession, project_id: uuid.UUID, _request_id: object
    ) -> None:
        await demo_service.finish_demo_provision(caller, project_id)

    fills: list[uuid.UUID] = []

    async def _hold_fill(_caller: AsyncSession, project_id: uuid.UUID, _request_id: object) -> None:
        fills.append(project_id)

    texts: list[str] = []
    seen: set[str] = set()
    identity_keys: dict[str, str] = {}

    async def _read_documents(project_id: uuid.UUID, project_slug: str) -> None:
        branches = list(
            (
                await session.execute(
                    select(PlanBranch.id, PlanBranch.name)
                    .where(PlanBranch.project_id == project_id)
                    # "main" sorts first among the demo's branch names; the explicit
                    # key keeps that true if a branch is ever renamed.
                    .order_by(PlanBranch.name != "main", PlanBranch.name)
                )
            )
            .tuples()
            .all()
        )
        for branch_id, _branch_name in branches:
            documents = await build_documents(session, project_id, branch_id, project_slug)
            for doc in documents:
                embed_text = embed_text_for(
                    title=doc.title,
                    subtitle=doc.subtitle,
                    keywords=doc.keywords,
                    body=doc.body,
                )
                key = embed_text_key(embed_text)
                identity_keys.setdefault(
                    identity_key(doc.entity_type, doc.title, doc.subtitle),
                    key,
                )
                if key in seen:
                    continue
                seen.add(key)
                texts.append(embed_text)

    original_seed = demo_service.enqueue_demo_seed
    original_fill = demo_service.enqueue_demo_history_fill
    original_mode = settings.demo_warehouse_mode
    original_type_change = calendar.property_type_broken
    demo_service.enqueue_demo_seed = _seed_inline  # type: ignore[assignment]
    demo_service.enqueue_demo_history_fill = _hold_fill  # type: ignore[assignment]
    settings.demo_warehouse_mode = "memory"
    try:
        with bound_org(OrgRef(DEFAULT_ORG_ID, DEFAULT_ORG_SLUG)):
            for slug, sends_string in (("demo-fixture-gen", True), ("demo-fixture-gen-2", False)):
                calendar.property_type_broken = _pinned_type_change(sends_string)
                # The warehouse model caches the hours it generated.
                rows.clear_cache()
                fills.clear()
                project = await demo_service.create_demo_project(session, slug=slug)
                if project.generation_status != "ready":
                    raise SystemExit(f"the demo build ended {project.generation_status!r}")
                await _read_documents(project.id, project.slug)
                for project_id in fills:
                    outcome = await demo_history_service.finish_demo_history(session, project_id)
                    if outcome != "complete":
                        raise SystemExit(f"the demo's history fill ended {outcome!r}")
                await _read_documents(project.id, project.slug)
    finally:
        demo_service.enqueue_demo_seed = original_seed
        demo_service.enqueue_demo_history_fill = original_fill
        settings.demo_warehouse_mode = original_mode
        calendar.property_type_broken = original_type_change
        rows.clear_cache()
    return CollectedTexts(texts=texts, identity_keys=identity_keys)


def _embed_all(
    embedder: Embedder,
    texts: list[str],
    *,
    batch_size: int,
    label: str,
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        embeddings = embedder(batch)
        if len(embeddings) != len(batch):
            msg = f"Embedder returned {len(embeddings)} vectors for {len(batch)} {label} texts"
            raise SystemExit(msg)
        for offset, raw_embedding in enumerate(embeddings):
            sanitized = sanitize_embedding(raw_embedding)
            if not sanitized:
                msg = (
                    f"Invalid {label} embedding at index {start + offset} "
                    "(wrong dimensions or non-finite values)"
                )
                raise SystemExit(msg)
            vectors.append(sanitized)
    return vectors


async def generate_fixture(
    output_path: Path,
    *,
    embedder: Embedder | None = None,
    model: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> GeneratedFixture:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # The migration inserts the default organization; create_all does not
            # (the test suite's own engines get it from a listener).
            if (
                await conn.scalar(select(Organization.id).where(Organization.id == DEFAULT_ORG_ID))
                is None
            ):
                await conn.execute(insert(Organization).values(**default_organization_values()))
        async with session_local() as session:
            collected = await _collect_demo_embed_texts(session)
    finally:
        await engine.dispose()

    texts = collected.texts
    resolved_embedder = embedder if embedder is not None else _openai_embedder()
    resolved_model = model if model is not None else env_ai_config().search_embedding_model

    doc_keys = [embed_text_key(embed_text) for embed_text in texts]
    doc_vectors = _embed_all(resolved_embedder, texts, batch_size=batch_size, label="document")
    query_keys = [normalize_demo_query(query) for query in DEMO_SEARCH_QUERIES]
    query_vectors = _embed_all(resolved_embedder, query_keys, batch_size=batch_size, label="query")

    manifest = {
        "recipe_version": DEMO_RECIPE_VERSION,
        "model": resolved_model,
        "dimensions": settings.search_embedding_dimensions,
        "doc_keys": doc_keys,
        "query_keys": query_keys,
    }
    dimensions = settings.search_embedding_dimensions
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # float16 is deliberate: cosine ranking is quantization-tolerant and the
    # loader upcasts to float32 before anything touches pgvector.
    np.savez_compressed(
        output_path,
        manifest=np.array(json.dumps(manifest)),
        doc_vectors=np.asarray(doc_vectors, dtype=np.float16).reshape(len(doc_keys), dimensions),
        query_vectors=np.asarray(query_vectors, dtype=np.float16).reshape(
            len(query_keys), dimensions
        ),
    )
    # Content-independent fallback index, written beside the archive so the demo
    # keeps its vectors after a scan rewrites document text. Every identity maps
    # to a text this run embedded. Until the archive is next embedded, the
    # shipped sidecar also has one entry written by hand: that archive came from
    # one build, in which ``${results_count}`` stayed a ``string``, so the
    # variable's ``number`` identity maps to that text. The two differ in the
    # type word and in what the scan observed, which differs between any two
    # builds anyway, so the vector is still that variable's. A run of this
    # script maps the identity to its own text.
    sidecar = identity_sidecar_path(output_path)
    sidecar.write_text(
        json.dumps(dict(sorted(collected.identity_keys.items())), indent=1, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return GeneratedFixture(
        path=output_path,
        model=resolved_model,
        dimensions=dimensions,
        doc_count=len(doc_keys),
        query_count=len(query_keys),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_FIXTURE_PATH,
        help=f"Fixture path to write (default: {DEFAULT_FIXTURE_PATH})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Texts per embedding request",
    )
    args = parser.parse_args(argv)
    result = asyncio.run(generate_fixture(args.output, batch_size=args.batch_size))
    print(  # noqa: T201 - CLI output
        f"Wrote {result.path} ({result.doc_count} doc vectors, "
        f"{result.query_count} query vectors, model={result.model}, "
        f"dims={result.dimensions})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
