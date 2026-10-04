"""Keyless semantic search for the demo project.

The demo's searchable text is fully deterministic, so its embeddings can be
precomputed once (by a maintainer running
``scripts/generate_demo_search_embeddings.py`` with a real OpenAI key) and
shipped as a compressed ``.npz`` fixture. At demo (re)index time the fixture
vectors are stamped onto matching search documents; at query time a small set
of canned query vectors stands in for the live embedding call. No API key is
ever needed at runtime.

The fixture keys documents by ``sha256(embed_text)`` — NOT by ``content_hash``,
which mixes in the per-install random demo slug and entity ids and is therefore
not install-stable.

Identity fallback
-----------------------------------
Keying by embed text alone is fragile: the demo runs the REAL scan/collection
pipeline, and that pipeline rewrites the very text the key is derived from
(observed field values replace ``${product_id}`` templates, catalog sync appends
auto-created field descriptors, variable contexts are re-recorded). One scan —
step 1 of the coached "Run the live loop" chapter — therefore missed every key
and left the whole index ``embedding_status='disabled'`` with no provider to
re-embed it, so semantic search died permanently on the demo's own showcase
query.

So each vector also carries a CONTENT-INDEPENDENT identity key,
``entity_type \x1f title \x1f subtitle``, in the JSON sidecar next to the
archive. A document whose descriptive text a scan has changed still resolves to
the vector generated for that same entity. The sidecar is derived, not embedded:
regenerate it by rebuilding a pristine demo, computing
``embed_text_key(embed_text_for(...))`` per search document and mapping the
identity key onto it — no embedding API call is involved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import settings
from tripl.models.search_document import SearchDocument
from tripl.services._search_documents import embed_text_for
from tripl.services.demo.scenario import DEMO_RECIPE_VERSION

logger = logging.getLogger(__name__)

DEFAULT_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "search_embeddings_v4.npz"

# Module attribute (not inlined) so tests can monkeypatch the location.
_FIXTURE_PATH = DEFAULT_FIXTURE_PATH

# Sidecar mapping ``identity key -> embed-text key`` for the same fixture. Kept
# beside the archive (and derived from it) so the content-independent fallback
# needs no re-embedding and stays reviewable as plain text.
IDENTITY_SIDECAR_SUFFIX = ".identity.json"

# Field separator for the identity key. A unit separator can never occur in a
# title/subtitle, so the joined key is unambiguous.
_IDENTITY_SEP = "\x1f"


def normalize_demo_query(query: str) -> str:
    """Canonical form for canned-query lookup: casefold + collapsed whitespace."""
    return re.sub(r"\s+", " ", query.casefold()).strip()


def embed_text_key(embed_text: str) -> str:
    """Install-stable fixture key for one document's embed text."""
    return hashlib.sha256(embed_text.encode("utf-8")).hexdigest()


def identity_sidecar_path(fixture_path: Path) -> Path:
    """Location of the identity sidecar for a given fixture archive."""
    return fixture_path.with_suffix(fixture_path.suffix + IDENTITY_SIDECAR_SUFFIX)


def identity_key(entity_type: str, title: str, subtitle: str) -> str:
    """Content-INDEPENDENT fixture key for one search document.

    Built from what the document *is* rather than what it currently says, so a
    scan that rewrites a document's body/keywords cannot orphan its vector.
    """
    return _IDENTITY_SEP.join((entity_type, title, subtitle or ""))


@dataclass(frozen=True)
class DemoEmbeddingFixture:
    """Parsed fixture: vectors keyed by embed-text sha256 / normalized query.

    ``identity_vectors`` holds the same vectors under their content-independent
    identity key (see :func:`identity_key`); it is empty when the sidecar is
    missing, which simply disables the fallback.
    """

    model: str
    dimensions: int
    doc_vectors: dict[str, list[float]]
    query_vectors: dict[str, list[float]]
    identity_vectors: dict[str, list[float]] = field(default_factory=dict)

    def vector_for(self, *, embed_key: str, identity: str) -> list[float] | None:
        """The document's vector: exact embed text first, identity as fallback."""
        vector = self.doc_vectors.get(embed_key)
        if vector is not None:
            return vector
        return self.identity_vectors.get(identity)


def load_demo_embedding_fixture() -> DemoEmbeddingFixture | None:
    """Parse-once fixture reader; ``None`` when absent or unusable.

    A missing or stale fixture is an expected state (fresh checkout, recipe
    bump, changed dimensions), so every rejection is a single INFO log — never
    an error — and the demo simply stays lexical-only.
    """
    return _parse_fixture(_FIXTURE_PATH)


@lru_cache(maxsize=8)
def _parse_fixture(path: Path) -> DemoEmbeddingFixture | None:
    if not path.is_file():
        logger.info(
            "Demo search-embedding fixture missing at %s; demo semantic search stays lexical",
            path,
        )
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            manifest_raw = data["manifest"].item()
            manifest = json.loads(str(manifest_raw))
            doc_vectors = np.asarray(data["doc_vectors"], dtype=np.float32)
            query_vectors = np.asarray(data["query_vectors"], dtype=np.float32)
        recipe_version = str(manifest.get("recipe_version", ""))
        model = str(manifest.get("model", ""))
        dimensions = int(manifest.get("dimensions", 0))
        doc_keys = manifest.get("doc_keys")
        query_keys = manifest.get("query_keys")
    except (
        OSError,
        EOFError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        # np.load raises these (direct Exception subclasses, so they need
        # listing) on archives that carry the zip magic but are truncated or
        # corrupted — e.g. an interrupted generator run or a partial deploy.
        zipfile.BadZipFile,
        zlib.error,
    ) as exc:
        logger.info("Demo search-embedding fixture at %s is unreadable (%s); ignoring", path, exc)
        return None
    if recipe_version != DEMO_RECIPE_VERSION:
        logger.info(
            "Demo search-embedding fixture at %s targets recipe %r (current %r); ignoring",
            path,
            recipe_version,
            DEMO_RECIPE_VERSION,
        )
        return None
    if dimensions != settings.search_embedding_dimensions:
        logger.info(
            "Demo search-embedding fixture at %s has %d dimensions (configured %d); ignoring",
            path,
            dimensions,
            settings.search_embedding_dimensions,
        )
        return None
    if (
        not isinstance(doc_keys, list)
        or not isinstance(query_keys, list)
        or doc_vectors.shape != (len(doc_keys), dimensions)
        or query_vectors.shape != (len(query_keys), dimensions)
    ):
        logger.info(
            "Demo search-embedding fixture at %s has inconsistent keys/vector shapes; ignoring",
            path,
        )
        return None

    resolved_docs = {
        str(key): [float(value) for value in row]
        for key, row in zip(doc_keys, doc_vectors, strict=True)
    }
    return DemoEmbeddingFixture(
        model=model,
        dimensions=dimensions,
        doc_vectors=resolved_docs,
        query_vectors={
            str(key): [float(value) for value in row]
            for key, row in zip(query_keys, query_vectors, strict=True)
        },
        identity_vectors=_load_identity_vectors(path, resolved_docs),
    )


def _load_identity_vectors(
    path: Path, doc_vectors: dict[str, list[float]]
) -> dict[str, list[float]]:
    """Resolve the ``identity key -> embed-text key`` sidecar into vectors.

    Absent, unreadable or stale entries are skipped silently (one INFO log at
    most): the fallback is an enhancement, and losing it only takes the demo back
    to matching on exact embed text.
    """
    sidecar = identity_sidecar_path(path)
    if not sidecar.is_file():
        return {}
    try:
        mapping = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.info("Demo embedding identity sidecar at %s is unreadable (%s); ignoring", path, exc)
        return {}
    if not isinstance(mapping, dict):
        logger.info("Demo embedding identity sidecar at %s is not an object; ignoring", sidecar)
        return {}
    resolved: dict[str, list[float]] = {}
    for key, embed_key in mapping.items():
        vector = doc_vectors.get(str(embed_key))
        if vector is not None:
            resolved[str(key)] = vector
    return resolved


def demo_query_embedding(query: str) -> list[float] | None:
    """Canned vector for a demo search query, or ``None`` on a fixture miss."""
    fixture = load_demo_embedding_fixture()
    if fixture is None:
        return None
    vector = fixture.query_vectors.get(normalize_demo_query(query))
    return list(vector) if vector is not None else None


async def apply_demo_embedding_fixture(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
) -> int:
    """Stamp fixture vectors onto this branch's search documents; return count.

    Postgres-only (the caller guards the dialect and ``project.is_demo``);
    reuses the worker task's ``CAST(:embedding AS vector)`` UPDATE idiom.
    Idempotent: rows already ``ready`` are left alone, unmatched rows are left
    untouched, and a demo reset simply re-runs the whole thing on the freshly
    re-seeded rows. Does not commit — the caller owns the transaction.
    """
    fixture = load_demo_embedding_fixture()
    if fixture is None:
        return 0

    rows = (
        await session.execute(
            select(
                SearchDocument.id,
                SearchDocument.entity_type,
                SearchDocument.title,
                SearchDocument.subtitle,
                SearchDocument.keywords,
                SearchDocument.body,
                SearchDocument.embedding_status,
            ).where(
                SearchDocument.project_id == project_id,
                SearchDocument.branch_id == branch_id,
            )
        )
    ).all()

    applied = 0
    for row in rows:
        if row.embedding_status == "ready":
            continue
        embedding = fixture.vector_for(
            embed_key=embed_text_key(
                embed_text_for(
                    title=row.title,
                    subtitle=row.subtitle,
                    keywords=row.keywords,
                    body=row.body,
                )
            ),
            identity=identity_key(row.entity_type, row.title, row.subtitle),
        )
        if embedding is None:
            continue
        vector = "[" + ",".join(f"{value:.8f}" for value in embedding) + "]"
        await session.execute(
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
                "embedding_model": fixture.model,
                "document_id": row.id,
            },
        )
        applied += 1
    return applied
