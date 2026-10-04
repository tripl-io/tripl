"""Generate the compressed demo search-embedding fixture.

Seeds the full demo scenario into an in-memory SQLite database, builds the
search documents for BOTH demo branches (main + the deep-copied feature
branch, whose single edited event description must land in the fixture too),
embeds the deduped texts plus a curated query list via the OpenAI embedding
API, and writes ``search_embeddings_v4.npz`` into
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tripl.config import settings
from tripl.middleware.org_context import OrgRef, bound_org
from tripl.models import Base
from tripl.models.organization import DEFAULT_ORG_ID, DEFAULT_ORG_SLUG
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
# anything the product surfaces as suggested demo searches.
DEMO_SEARCH_QUERIES: tuple[str, ...] = (
    "purchase funnel",
    "money back",
    "why did revenue drop",
    "subscription lifecycle",
    "user identity",
    "onboarding drop-off",
    "screen where users pay",
    "promo code redemption",
)

DEFAULT_BATCH_SIZE = 64

# Injectable so tests can drive the script end-to-end with a fake embedder.
Embedder = Callable[[list[str]], list[list[float]]]


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
    """
    from tripl.services import demo_service

    # A script has no request organization; the demo seed resolves its project
    # by slug, so bind the default organization for that call (F20 PR2).
    with bound_org(OrgRef(DEFAULT_ORG_ID, DEFAULT_ORG_SLUG)):
        project = await demo_service.create_demo_project(session, slug="demo-fixture-gen")
    branches = list(
        (
            await session.execute(
                select(PlanBranch.id, PlanBranch.name)
                .where(PlanBranch.project_id == project.id)
                # "main" sorts first among the demo's branch names; the explicit
                # key keeps that true if a branch is ever renamed.
                .order_by(PlanBranch.name != "main", PlanBranch.name)
            )
        )
        .tuples()
        .all()
    )
    texts: list[str] = []
    seen: set[str] = set()
    identity_keys: dict[str, str] = {}
    for branch_id, _branch_name in branches:
        documents = await build_documents(session, project.id, branch_id, project.slug)
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
    # keeps its vectors after a scan rewrites document text.
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
