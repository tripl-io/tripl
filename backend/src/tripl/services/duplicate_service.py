"""Duplicate detection and naming-convention lint (GH #265, F12).

No LLM (owner decision). Similarity is lexical (``core.analyzers.name_similarity``)
blended with an embedding cosine when the search index already holds vectors
for the events — the same ``search_documents.embedding`` rows the semantic
search leg reads — and the candidate can be embedded by the configured
provider. Neither being available simply means lexical only.

Three entry points:

* :func:`check_candidates` — would-be events (create form, bulk preview,
  shadow inbox accept) against the branch's catalog: top duplicates, lint
  against the inferred convention, and one suggested name.
* :func:`list_clusters` — existing live / implemented / ready events that look
  like one event spelled several ways, grouped by union-find, paginated.
* :func:`dismiss_pair` — "these two are not duplicates", remembered per project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import Text, cast, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers.duplicate_matching import (
    DUPLICATE_THRESHOLD,
    MIN_LEXICAL_FOR_EMBEDDING,
    CatalogName,
    Cluster,
    Match,
    NameIndex,
    cluster_pairs,
    ordered_pair,
    scored_pairs,
)
from tripl.core.analyzers.name_similarity import (
    Convention,
    combined_score,
    infer_convention,
    lint_name,
    normalised_text,
    suggest,
    with_prefix,
)
from tripl.models.domain_enums import enum_text
from tripl.models.duplicate_dismissal import DuplicateDismissal
from tripl.models.event import Event, EventStatus
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.search_document import SearchDocument
from tripl.schemas.duplicates import (
    DuplicateCandidateIn,
    DuplicateCheckResponse,
    DuplicateCheckResult,
    DuplicateCluster,
    DuplicateClusterEvent,
    DuplicateClusterPage,
    DuplicateDismissResponse,
    DuplicateHit,
    NameLintIssue,
    NamingConventionOut,
)
from tripl.services import app_settings_service
from tripl.services._id_chunks import chunked
from tripl.services.lifecycle_rules import lookback_start, window_volume

logger = logging.getLogger(__name__)

#: The most catalog rows one request reads. Past this the answer is partial
#: (``truncated``) rather than slow.
MAX_CATALOG_EVENTS = 5000
#: Candidates beyond this are checked lexically only: one provider round trip
#: for a 500-row bulk paste is not worth its latency.
MAX_EMBEDDED_CANDIDATES = 50
EMBED_TIMEOUT_SECONDS = 3.0
#: A cosine at or above this is named as "semantic match" in the reasons.
SEMANTIC_REASON_COSINE = 0.85
CLUSTER_PAGE_SIZE = 25
#: The largest cluster listed in full; a bigger one is a naming rule problem.
MAX_CLUSTER_MEMBERS = 20
VOLUME_WINDOW = timedelta(days=7)

#: Statuses the duplicates view clusters (the catalog people actually use).
CLUSTER_STATUSES = (
    EventStatus.live.value,
    EventStatus.implemented.value,
    EventStatus.ready_for_dev.value,
)
#: Statuses the naming convention is learned from.
CONVENTION_STATUSES = frozenset({EventStatus.live.value, EventStatus.implemented.value})


async def _dismissal_anchors(
    session: AsyncSession,
    project_id: uuid.UUID,
    origins: Mapping[uuid.UUID, uuid.UUID | None],
) -> dict[uuid.UUID, uuid.UUID]:
    """Each event's dismissal key — THE one definition, for reads and writes.

    The key is the MAIN row a branch copy was made from (``origin_id``), so a
    pair dismissed on main stays dismissed on every branch; but only while that
    row still exists in this project. An origin that is gone (or foreign) falls
    back to the event itself, exactly as :func:`dismiss_pair` stores it.
    """
    wanted = sorted({origin for origin in origins.values() if origin is not None}, key=str)
    existing: set[uuid.UUID] = set()
    for chunk in chunked(wanted):
        existing.update(
            (
                await session.execute(
                    select(Event.id).where(Event.project_id == project_id, Event.id.in_(chunk))
                )
            ).scalars()
        )
    return {
        event_id: origin if origin is not None and origin in existing else event_id
        for event_id, origin in origins.items()
    }


async def _catalog(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    statuses: Sequence[str] | None = None,
) -> tuple[list[CatalogName], dict[uuid.UUID, uuid.UUID], bool]:
    """Non-archived events of one branch, plus each row's dismissal key
    (:func:`_dismissal_anchors`)."""
    query = (
        select(Event.id, Event.name, Event.event_type_id, Event.status, Event.origin_id)
        .where(Event.project_id == project_id, Event.branch_id == branch_id)
        .order_by(Event.order, Event.name, Event.id)
        .limit(MAX_CATALOG_EVENTS + 1)
    )
    if statuses is not None:
        query = query.where(Event.status.in_(list(statuses)))
    else:
        query = query.where(Event.status != EventStatus.archived)
    rows = (await session.execute(query)).all()
    truncated = len(rows) > MAX_CATALOG_EVENTS
    entries: list[CatalogName] = []
    origins: dict[uuid.UUID, uuid.UUID | None] = {}
    for event_id, name, event_type_id, status, origin_id in rows[:MAX_CATALOG_EVENTS]:
        entries.append(CatalogName(event_id, name or "", event_type_id, enum_text(status)))
        origins[event_id] = origin_id
    keys = await _dismissal_anchors(session, project_id, origins)
    return entries, keys, truncated


async def _name_formats(
    session: AsyncSession, project_id: uuid.UUID, event_type_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    from tripl.services.scan_config_lookup import (
        governing_name_format,
        load_governing_scan_configs_by_type,
    )

    if not event_type_ids:
        return {}
    by_type = await load_governing_scan_configs_by_type(
        session, project_id=project_id, event_type_ids=list(event_type_ids)
    )
    formats: dict[uuid.UUID, str] = {}
    for type_id, configs in by_type.items():
        name_format = governing_name_format(configs)
        if name_format and name_format.strip():
            formats[type_id] = name_format
    return formats


def _conventions(
    entries: Sequence[CatalogName], ruled_types: set[uuid.UUID]
) -> tuple[Convention, dict[uuid.UUID, str | None]]:
    """The project convention plus a prefix per event type.

    Learned from live / implemented events of UNRULED types only: a naming
    rule spells names from warehouse values, and those say nothing about how
    people name events. Falls back to every listed event when too few are live.
    """
    authored = [e for e in entries if e.event_type_id not in ruled_types]
    learned = [e for e in authored if e.status in CONVENTION_STATUSES]
    if len(learned) < 5:
        learned = authored
    project = infer_convention([e.name for e in learned])
    by_type: dict[uuid.UUID, list[str]] = {}
    for entry in learned:
        by_type.setdefault(entry.event_type_id, []).append(entry.name)
    prefixes = {type_id: infer_convention(names).prefix for type_id, names in by_type.items()}
    return project, prefixes


def _convention_out(convention: Convention) -> NamingConventionOut:
    return NamingConventionOut.model_validate(convention.to_dict())


# ---------------------------------------------------------------------------
# Embeddings (best-effort)
# ---------------------------------------------------------------------------


def _cosine(a: Sequence[float], b: Sequence[float]) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else None


def _parse_vector(raw: object) -> list[float] | None:
    if raw is None:
        return None
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(values, list) or not values:
            return None
        vector = [float(v) for v in values]
    except TypeError, ValueError:
        return None
    return vector if all(math.isfinite(v) for v in vector) else None


async def _event_vectors(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_ids: Sequence[uuid.UUID],
    model: str,
) -> dict[uuid.UUID, list[float]]:
    """Stored event-document vectors of ``model`` for ``event_ids``.

    Read as TEXT: the Postgres column is ``vector(1536)`` (whose text form is
    a JSON array) and the SQLite test column is JSON, and one cast reads both.
    """
    vectors: dict[uuid.UUID, list[float]] = {}
    ids = list(dict.fromkeys(event_ids))
    for chunk in chunked(ids):
        rows = await session.execute(
            select(SearchDocument.entity_id, cast(SearchDocument.embedding, Text)).where(
                SearchDocument.project_id == project_id,
                SearchDocument.branch_id == branch_id,
                SearchDocument.entity_type == "event",
                SearchDocument.entity_id.in_(chunk),
                SearchDocument.embedding.is_not(None),
                SearchDocument.embedding_status == "ready",
                SearchDocument.embedding_model == model,
            )
        )
        for entity_id, raw in rows.all():
            vector = _parse_vector(raw)
            if vector is not None:
                vectors[entity_id] = vector
    return vectors


async def _stored_vector_model(session: AsyncSession, project_id: uuid.UUID) -> str | None:
    """Which embedding space the event vectors to compare live in, if any."""
    from tripl.models.project import Project
    from tripl.services.demo.search_embeddings import load_demo_embedding_fixture
    from tripl.services.embedding_service import embedding_provenance

    ai_config = await app_settings_service.get_embedding_config_for_project(session, project_id)
    if ai_config.search_embeddings_enabled:
        return embedding_provenance(ai_config)
    if await session.scalar(select(Project.is_demo).where(Project.id == project_id)):
        fixture = load_demo_embedding_fixture()
        return fixture.model if fixture is not None else None
    return None


async def _embed_candidates(
    session: AsyncSession, project_id: uuid.UUID, texts: list[str]
) -> tuple[list[list[float]], str | None]:
    """Live vectors for the candidates, or ``[]`` when that is not possible.

    In the project's organization's vector space (F20 PR10), so they compare
    with that organization's stored event vectors and nobody else's.
    """
    from tripl.services.embedding_service import embed_texts, embedding_provenance

    ai_config = await app_settings_service.get_embedding_config_for_project(session, project_id)
    if not ai_config.search_embeddings_enabled or not texts:
        return [], None
    try:
        vectors = await asyncio.wait_for(
            asyncio.to_thread(embed_texts, texts, config=ai_config, timeout=EMBED_TIMEOUT_SECONDS),
            timeout=EMBED_TIMEOUT_SECONDS + 1,
        )
    except Exception:
        logger.warning("Duplicate check: candidate embedding failed", exc_info=True)
        return [], None
    if len(vectors) != len(texts):
        return [], None
    return vectors, embedding_provenance(ai_config)


# ---------------------------------------------------------------------------
# Candidate check
# ---------------------------------------------------------------------------


async def _render_ruled_names(
    session: AsyncSession,
    candidates: Sequence[DuplicateCandidateIn],
    formats: Mapping[uuid.UUID, str],
) -> list[str]:
    """Each candidate's name; a blank name under a rule is rendered from its values."""
    from tripl.services.event_service import apply_scan_name_format

    names = [c.name.strip() for c in candidates]
    need = {
        c.event_type_id
        for c, name in zip(candidates, names, strict=True)
        if not name and c.event_type_id in formats and c.field_values
    }
    if not need:
        return names
    field_names: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
    rows = await session.execute(
        select(FieldDefinition.event_type_id, FieldDefinition.id, FieldDefinition.name).where(
            FieldDefinition.event_type_id.in_(list(need))
        )
    )
    for type_id, field_id, field_name in rows.all():
        field_names.setdefault(type_id, {})[field_id] = field_name
    for position, candidate in enumerate(candidates):
        if names[position] or candidate.event_type_id not in need:
            continue
        try:
            names[position] = apply_scan_name_format(
                name_format=formats[candidate.event_type_id],
                field_names=field_names.get(candidate.event_type_id, {}),
                field_values=candidate.field_values,
            )
        except HTTPException:
            names[position] = ""  # the rule still misses a value; nothing to compare yet
    return names


async def _field_overlap(
    session: AsyncSession,
    candidates: Sequence[DuplicateCandidateIn],
    matches: Sequence[Sequence[Match]],
) -> list[dict[uuid.UUID, int]]:
    """Per candidate, how many (field, value) pairs each match shares with it."""
    wanted = {
        m.event_id
        for candidate, found in zip(candidates, matches, strict=True)
        if candidate.field_values
        for m in found
    }
    if not wanted:
        return [{} for _ in candidates]
    values: dict[uuid.UUID, set[tuple[uuid.UUID, str]]] = {}
    rows = await session.execute(
        select(
            EventFieldValue.event_id, EventFieldValue.field_definition_id, EventFieldValue.value
        ).where(EventFieldValue.event_id.in_(list(wanted)))
    )
    for event_id, field_id, value in rows.all():
        if value:
            values.setdefault(event_id, set()).add((field_id, value))
    overlap: list[dict[uuid.UUID, int]] = []
    for candidate, found in zip(candidates, matches, strict=True):
        own = {(fv.field_definition_id, fv.value) for fv in candidate.field_values if fv.value}
        overlap.append(
            {m.event_id: len(own & values.get(m.event_id, set())) for m in found} if own else {}
        )
    return overlap


def _reasons(
    name: str, match: Match, cosine: float | None, shared_fields: int, threshold: float
) -> list[str]:
    reasons: list[str] = []
    if normalised_text(name) == normalised_text(match.name):
        reasons.append("same name")
    elif match.lexical >= threshold:
        reasons.append("similar name")
    if cosine is not None and cosine >= SEMANTIC_REASON_COSINE:
        reasons.append("semantic match")
    reasons.append("same event type" if match.same_type else "different event type")
    if shared_fields:
        noun = "value" if shared_fields == 1 else "values"
        reasons.append(f"{shared_fields} shared field {noun}")
    return reasons


async def check_candidates(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    candidates: Sequence[DuplicateCandidateIn],
    *,
    threshold: float = DUPLICATE_THRESHOLD,
) -> DuplicateCheckResponse:
    """Duplicates and lint for each would-be event, in request order. Writes nothing."""
    entries, _keys, _truncated = await _catalog(session, project_id, branch_id)
    # Only this project's types: a foreign id in the body is simply "no type".
    type_names: dict[uuid.UUID, str] = {
        type_id: display
        for type_id, display in (
            await session.execute(
                select(EventType.id, EventType.display_name).where(
                    EventType.project_id == project_id,
                    EventType.id.in_(
                        list(
                            {e.event_type_id for e in entries}
                            | {c.event_type_id for c in candidates}
                        )
                    ),
                )
            )
        ).all()
    }
    formats = await _name_formats(session, project_id, sorted(type_names, key=str))
    index = NameIndex(entries, formats_by_type=formats)
    convention, prefixes = _conventions(entries, set(formats))
    names = await _render_ruled_names(session, candidates, formats)

    # Pass 1, lexical: which catalog events could reach the threshold at all.
    lexical_hits: list[list[Match]] = [
        index.find(
            name,
            c.event_type_id,
            threshold=MIN_LEXICAL_FOR_EMBEDDING,
            slot_threshold=threshold,
            exclude=[c.event_id] if c.event_id else (),
            limit=MAX_CATALOG_EVENTS,
        )
        if name
        else []
        for c, name in zip(candidates, names, strict=True)
    ]

    # Pass 2, semantic (best-effort): only for those events, only when both
    # sides can be embedded in one space.
    candidate_vectors: list[list[float]] = []
    stored: dict[uuid.UUID, list[float]] = {}
    needs_semantic = any(hits for hits in lexical_hits)
    if needs_semantic and len(candidates) <= MAX_EMBEDDED_CANDIDATES:
        from tripl.services._search_documents import embed_text_for

        texts = [
            embed_text_for(
                title=name,
                subtitle=type_names.get(c.event_type_id, ""),
                keywords="",
                body=c.description or "",
            )
            for c, name in zip(candidates, names, strict=True)
        ]
        candidate_vectors, model = await _embed_candidates(session, project_id, texts)
        if candidate_vectors and model:
            stored = await _event_vectors(
                session,
                project_id=project_id,
                branch_id=branch_id,
                event_ids=[m.event_id for hits in lexical_hits for m in hits],
                model=model,
            )
    semantic_used = bool(stored)

    matches: list[list[Match]] = []
    cosines: list[dict[uuid.UUID, float | None]] = []
    for position, hits in enumerate(lexical_hits):
        vector = candidate_vectors[position] if semantic_used else []
        by_event = {m.event_id: _cosine(vector, stored.get(m.event_id, [])) for m in hits}
        cosines.append(by_event)
        rescored = [
            Match(
                event_id=m.event_id,
                name=m.name,
                event_type_id=m.event_type_id,
                status=m.status,
                lexical=m.lexical,
                score=combined_score(m.lexical, by_event.get(m.event_id)),
                same_type=m.same_type,
            )
            for m in hits
        ]
        kept = [m for m in rescored if m.score >= threshold]
        kept.sort(key=lambda m: (not m.same_type, -m.score, m.name, str(m.event_id)))
        matches.append(kept[:3])

    overlaps = await _field_overlap(session, candidates, matches)

    items: list[DuplicateCheckResult] = []
    for position, candidate in enumerate(candidates):
        name = names[position]
        ruled = candidate.event_type_id in formats
        type_convention = with_prefix(convention, prefixes.get(candidate.event_type_id))
        exact = any(normalised_text(m.name) == normalised_text(name) for m in matches[position])
        lint = [] if ruled or exact or not name else lint_name(name, type_convention)
        suggestion = suggest(name, type_convention) if lint else None
        items.append(
            DuplicateCheckResult(
                name=name,
                duplicates=[
                    DuplicateHit(
                        event_id=m.event_id,
                        name=m.name,
                        event_type_id=m.event_type_id,
                        status=m.status,
                        score=m.score,
                        reasons=_reasons(
                            name,
                            m,
                            cosines[position].get(m.event_id),
                            overlaps[position].get(m.event_id, 0),
                            threshold,
                        ),
                    )
                    for m in matches[position]
                ],
                lint=[NameLintIssue.model_validate(issue) for issue in lint],
                suggestion=suggestion if suggestion and suggestion != name else None,
                lint_applicable=not ruled,
                convention=None if ruled else _convention_out(type_convention),
            )
        )
    return DuplicateCheckResponse(items=items, threshold=threshold, semantic_used=semantic_used)


# ---------------------------------------------------------------------------
# Clusters and dismissals
# ---------------------------------------------------------------------------


async def _dismissed_pairs(
    session: AsyncSession, project_id: uuid.UUID
) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
    rows = await session.execute(
        select(DuplicateDismissal.event_a_id, DuplicateDismissal.event_b_id).where(
            DuplicateDismissal.project_id == project_id
        )
    )
    return frozenset(ordered_pair(a, b) for a, b in rows.all())


async def _volumes_7d(
    session: AsyncSession, metric_ids: Sequence[uuid.UUID], *, now: datetime | None = None
) -> dict[uuid.UUID, int]:
    if not metric_ids:
        return {}
    moment = now or datetime.now(UTC)
    rows = await session.execute(
        select(EventMetric.event_id, EventMetric.bucket, EventMetric.count, ScanConfig.interval)
        .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
        .where(
            EventMetric.event_id.in_(list(metric_ids)),
            EventMetric.bucket >= lookback_start(moment, VOLUME_WINDOW),
            EventMetric.count > 0,
        )
    )
    return window_volume(
        (
            (event_id, bucket, int(count), None if interval is None else enum_text(interval))
            for event_id, bucket, count, interval in rows.all()
            if event_id is not None
        ),
        now=moment,
        window=VOLUME_WINDOW,
    )


def _lexical_pairs(
    entries: list[CatalogName], formats: Mapping[uuid.UUID, str], threshold: float
) -> tuple[list[tuple[tuple[uuid.UUID, uuid.UUID], float]], bool]:
    """Index and score the catalog (sync; run in a worker thread)."""
    return scored_pairs(NameIndex(entries, formats_by_type=formats), threshold=threshold)


def _clusters(
    links: list[tuple[tuple[uuid.UUID, uuid.UUID], float]],
    threshold: float,
    order: Mapping[uuid.UUID, int],
) -> list[Cluster]:
    return cluster_pairs(links, threshold=threshold, order=order)


#: A ``next_cursor``: one to nine ASCII digits. ``str.isdigit`` alone also passed
#: "²" and digit strings past ``int``'s 4300-digit limit, and ``int`` then raised.
_CURSOR = re.compile(r"[0-9]{1,9}")


def _parse_cursor(cursor: str | None) -> int:
    """The cluster offset a cursor carries; 422 for anything that is not one."""
    if not cursor:
        return 0
    if _CURSOR.fullmatch(cursor) is None:
        raise HTTPException(status_code=422, detail="Invalid cursor")
    return int(cursor)


async def list_clusters(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = CLUSTER_PAGE_SIZE,
    threshold: float = DUPLICATE_THRESHOLD,
) -> DuplicateClusterPage:
    """Clusters of likely duplicates among live / implemented / ready events.

    Computed on read — there is no job and nothing to go stale. Same-type pairs
    only; a dismissed pair never links (its events may still meet through a
    third). Cursor is an opaque offset into the score-ordered cluster list.
    """
    offset = _parse_cursor(cursor)
    entries, keys, truncated = await _catalog(
        session, project_id, branch_id, statuses=CLUSTER_STATUSES
    )
    formats = await _name_formats(
        session, project_id, sorted({e.event_type_id for e in entries}, key=str)
    )
    dismissed = await _dismissed_pairs(session, project_id)
    # Normalising, blocking and scoring thousands of names is pure CPU: off the
    # event loop, so one big catalog does not stall every other request.
    scored, over_budget = await asyncio.to_thread(_lexical_pairs, entries, formats, threshold)
    truncated = truncated or over_budget
    candidates = [
        (pair, lexical)
        for pair, lexical in scored
        if ordered_pair(keys[pair[0]], keys[pair[1]]) not in dismissed
    ]

    stored: dict[uuid.UUID, list[float]] = {}
    if candidates:
        model = await _stored_vector_model(session, project_id)
        if model:
            stored = await _event_vectors(
                session,
                project_id=project_id,
                branch_id=branch_id,
                event_ids=[event_id for pair, _ in candidates for event_id in pair],
                model=model,
            )
    links = [
        (
            pair,
            combined_score(
                lexical,
                _cosine(stored[pair[0]], stored[pair[1]])
                if pair[0] in stored and pair[1] in stored
                else None,
            ),
        )
        for pair, lexical in candidates
    ]
    order = {entry.event_id: position for position, entry in enumerate(entries)}
    clusters = await asyncio.to_thread(_clusters, links, threshold, order)
    page = clusters[offset : offset + limit]

    by_id = {entry.event_id: entry for entry in entries}
    member_ids = [event_id for cluster in page for event_id in cluster.event_ids]
    volumes = await _volumes_7d(session, [keys[event_id] for event_id in member_ids])
    items = [
        DuplicateCluster(
            score=cluster.score,
            events=[
                DuplicateClusterEvent(
                    id=event_id,
                    name=by_id[event_id].name,
                    status=by_id[event_id].status,
                    event_type_id=by_id[event_id].event_type_id,
                    volume_7d=volumes.get(keys[event_id], 0),
                )
                for event_id in cluster.event_ids[:MAX_CLUSTER_MEMBERS]
            ],
        )
        for cluster in page
    ]
    next_offset = offset + limit
    return DuplicateClusterPage(
        items=items,
        next_cursor=str(next_offset) if next_offset < len(clusters) else None,
        total=len(clusters),
        threshold=threshold,
        truncated=truncated,
    )


async def dismiss_pair(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_a_id: uuid.UUID,
    event_b_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
) -> DuplicateDismissResponse:
    """Remember that two events of this project are not duplicates (idempotent).

    Stored against the MAIN rows (a branch copy's ``origin_id``) so the verdict
    holds on every branch. 404 when either event is not in the project.
    """
    rows = (
        await session.execute(
            select(Event.id, Event.origin_id).where(
                Event.project_id == project_id, Event.id.in_([event_a_id, event_b_id])
            )
        )
    ).all()
    origins = {event_id: origin_id for event_id, origin_id in rows}
    if event_a_id not in origins or event_b_id not in origins:
        raise HTTPException(status_code=404, detail="Event not found")
    anchors = await _dismissal_anchors(session, project_id, origins)
    first, second = ordered_pair(anchors[event_a_id], anchors[event_b_id])
    if first == second:
        raise HTTPException(status_code=422, detail="Both ids name the same event")
    values = {
        "id": uuid.uuid4(),
        "project_id": project_id,
        "event_a_id": first,
        "event_b_id": second,
        "dismissed_by": user_id,
    }
    keys = ["project_id", "event_a_id", "event_b_id"]
    # ON CONFLICT DO NOTHING, not select-then-insert: two clicks (or two tabs)
    # racing on one pair must both succeed, and a savepoint around a plain
    # INSERT misbehaves in the SQLite test database.
    if session.get_bind().dialect.name == "sqlite":
        result = await session.execute(
            sqlite_insert(DuplicateDismissal)
            .values(**values)
            .on_conflict_do_nothing(index_elements=keys)
        )
    else:
        result = await session.execute(
            pg_insert(DuplicateDismissal)
            .values(**values)
            .on_conflict_do_nothing(index_elements=keys)
        )
    created = bool(getattr(result, "rowcount", 0))
    return DuplicateDismissResponse(event_a_id=first, event_b_id=second, created=created)
