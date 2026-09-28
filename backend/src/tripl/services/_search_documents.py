"""Document building helpers for the search index.

Converts every project entity kind in ``SearchEntityType``
(``schemas/search.py``) into ``BuiltDocument`` instances ready to be stored as
``SearchDocument`` rows — one ``_<kind>_document`` builder per kind.

Stated as a reference rather than a list on purpose: the previous wording named
eight of the eleven kinds, having gone stale twice as kinds were added.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.fact_table import FactTable
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.schemas.search import SearchEntityType
from tripl.services.project_links import legacy_project_path


def _clean(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _join(parts: Sequence[object | None]) -> str:
    values = [_clean(part) for part in parts]
    return " ".join(value for value in values if value)


def _is_sensitive(sensitivity: str | None) -> bool:
    return (sensitivity or "none") != "none"


#: What glues an identifier together in this product: ``screen_spot``,
#: ``page_data.extra.spot_id``, ``properties.session_key``.
_IDENTIFIER_SEPARATORS = re.compile(r"[._]+")


def _spaced_identifiers(values: Sequence[object | None]) -> str:
    """Space-separated aliases for the snake_case / dotted names in ``values``.

    INDEX-TIME HALF OF tripl-h9x2
    -----------------------------
    Entities are named ``screen_spot`` and people type ``screen spot``. The
    tsvector already survives that (the text-search parser splits on ``_``), but
    the ranking's boost ladder does not: its tiers are ``LIKE``/regex
    comparisons against the STORED text, so ``lower(title) = 'screen spot'``,
    ``LIKE 'screen spot%'`` and ``LIKE '%screen spot%'`` all miss a title that is
    spelled ``screen_spot``. Measured: ``q='screen spot'`` ranked the
    ``screen_spot`` event 5th at 4.545.

    So each identifier is ALSO indexed in its spaced form. Together with the
    query-time half (``_search_query.token_boundary_regex`` folds a spaced query
    into the underscored form) the two spellings reach each other from either
    side. Three properties are load-bearing:

    * only the ALIAS is added, once per distinct identifier and only when it
      actually differs — this is not a second copy of the document text, which
      would feed the very term-frequency inflation tripl-gbxj is about;
    * it is applied to ``keywords`` and never to a tag document, whose
      ``keywords`` is exactly ``tag.name`` and is compared for EQUALITY by the
      4.0 tier — appending anything there would silently delete that tier;
    * every call site passes an entity's NAME — never a value. Check that when
      adding one. What ``keywords`` means for the 3.5/3.25 tiers is an entity's
      IDENTITY, and this function's whole job is to make the segments of an
      identity reachable; a value spelled into the same column would take the
      same tiers without being anything anyone named (tripl-gbxj, tripl-0qld).

    "IDENTITY" AND NOT "CURATED", DELIBERATELY (tripl-0qld)
    -------------------------------------------------------
    A variable that the scanner auto-detected is still passed through here by
    ``_variable_document`` — ``variable.name`` is derived from the source column,
    so ``${property.spot_id}`` gains the alias ``property spot id`` and
    ``q='spot'`` can reach it at the 3.5 tier on a name nobody typed. That is
    intended, and calling it "curated" would be a lie this docstring is not going
    to tell: the entity exists, that IS what it is called, and the ranking's
    complaint in tripl-gbxj was never about names — it was about one document
    repeating a harvested VALUE more often than the entity named after it. The
    event named ``spot`` still wins ``q='spot'`` on the 5.0 exact-title tier,
    which is above anything an alias can buy.

    Dots are folded in the same pass (``page_data.extra.spot_id`` ->
    ``page data extra spot id``) because the text-search parser classifies a
    dotted name as a single host-like token and indexes it WHOLE — unlike the
    underscore, which it splits — so without the alias a search for ``spot_id``
    cannot reach the variable literally called ``page_data.extra.spot_id``.
    """
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean(value)
        if not cleaned:
            continue
        spaced = _IDENTIFIER_SEPARATORS.sub(" ", cleaned).strip()
        if not spaced or spaced == cleaned or spaced in seen:
            continue
        seen.add(spaced)
        aliases.append(spaced)
    return " ".join(aliases)


# Cap on the text embedded per document. The embedding model accepts 8191
# tokens; 6000 characters stays safely under that even for Cyrillic-heavy
# content (low chars-per-token). The 16k per-text cap in embedding_service
# remains as a backstop.
EMBED_TEXT_MAX_CHARS = 6000

# SearchDocument.title and .subtitle are VARCHAR(500) in PostgreSQL. Keep
# generated labels within that limit before hashing or embedding them.
SEARCH_LABEL_MAX_CHARS = 500


def embed_text_for(*, title: str, subtitle: str, keywords: str, body: str) -> str:
    """Build the text that gets embedded for one search document.

    Single source of truth for the embed-text recipe — the worker task and the
    demo embedding fixture pipeline both call this, so the two can never drift.
    Keywords come BEFORE the potentially huge body so that truncation drops
    the low-signal text first, and the joined text is capped at
    ``EMBED_TEXT_MAX_CHARS`` characters.
    """
    joined = "\n".join([title, subtitle, keywords, body]).strip()
    return joined[:EMBED_TEXT_MAX_CHARS]


@dataclass(frozen=True)
class BuiltDocument:
    entity_type: SearchEntityType
    entity_id: uuid.UUID
    parent_event_id: uuid.UUID | None
    title: str
    subtitle: str
    body: str
    keywords: str
    # ORG-LESS on purpose: ``/p/{slug}/...`` (critique #21, F20 PR8). It is part
    # of ``content_hash``, so writing the organization into it would change every
    # stored hash and re-embed the whole corpus. ``search_service`` completes it
    # to ``/o/{org}/p/{slug}/...`` when it serves a result.
    route_path: str
    description: str = ""
    archived: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", self.title[:SEARCH_LABEL_MAX_CHARS])
        object.__setattr__(self, "subtitle", self.subtitle[:SEARCH_LABEL_MAX_CHARS])

    @property
    def content_hash(self) -> str:
        content = "\n".join(
            [
                self.entity_type,
                str(self.entity_id),
                self.title,
                self.subtitle,
                self.description,
                self.body,
                self.keywords,
                self.route_path,
                str(self.archived),
            ]
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


#: Which generation of the document builders produced a stored row (tripl-uji9).
#:
#: BUMP THIS whenever a change to this module alters the TEXT a document is built
#: from — a new field folded into ``body``, a keyword rule, a title format. Do not
#: bump it for a change that cannot move any document's text (a refactor, a type
#: annotation, a docstring).
#:
#: WHY ``content_hash`` CANNOT ANSWER THIS
#: ---------------------------------------
#: ``content_hash`` is a sha256 over the document's own fields, so it detects that
#: a rebuilt document DIFFERS from the stored one — but only once the rebuild has
#: happened. It says nothing about whether a branch has been through the current
#: builders at all, and that is the question the sweep asks.
#:
#: Main branches self-heal: the worker reindexes main after every scan and every
#: metrics collection, so they pick up a builder change within the hour. Working
#: branches have no such path — they are rebuilt only when somebody edits them.
#: Measured on production on 2026-08-16, eight days after the keywords fix
#: shipped: all three main branches were correct, and eight acme-ios working
#: branches still held 7117 documents built by the previous generation.
#:
#: HISTORY
#: 1 — first stamped generation. Everything written before this column existed is
#:     0, which is what makes those eight branches visible to the sweep.
#: 2 — scan configurations and alert rules became searchable (tripl-dfct). Every
#:     branch gains documents it did not have, which no content_hash comparison
#:     could have discovered, so this is exactly the case the stamp exists for.
#: 3 — generated titles and subtitles are capped to their storage column width.
#: 4 — docs catalog notes (project + organization) became searchable (F22).
DOCUMENT_BUILDER_VERSION = 4


# Every caller reindexes inside its OWN transaction, right after mutating the
# rows it is about to index, so ``build_documents`` shares the writer's identity
# map. Without ``populate_existing`` the ORM hands back the objects already in
# that map and a ``selectinload`` does NOT overwrite a collection that is
# already loaded — so an event whose tags/field values/meta values were replaced
# by a bulk ``delete()`` plus FK-only ``add_all`` (``event_service.update_event``)
# still carries the OLD children in memory, and a field created or deleted
# through ``field_service`` is missing from or still present in
# ``EventType.field_definitions``. The rebuilt text then matches the stored
# ``content_hash``, the row is KEPT, and the index sits exactly one write behind
# until some later reindex runs from a fresh session (tripl-0zpq.183). Reading
# with ``populate_existing`` costs the same query and returns what the
# transaction actually holds.
#
# Where it is and is not, and why — a loaded COLLECTION is what a ``selectinload``
# will not overwrite, so the option goes on exactly the four reads that load one:
# ``EventType``, ``Event``, ``VariableValue`` and ``EventTypeRelation``. The
# plain-column reads below (``MetaFieldDefinition``, ``MetricDefinition``,
# ``FactTable``, ``ScanConfig``, ``AlertRule``) need nothing: a scalar edit is
# already on the identity-map object the query hands back, so they are current
# without it. The one read where it would be actively WRONG is ``Variable``,
# which carries ``lazyload(Variable.value_contexts)``: refreshing there would
# unload a collection the caller may still touch, and a lazy load on this async
# session raises.
async def build_documents(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    slug: str,
) -> list[BuiltDocument]:
    event_types = list(
        await session.scalars(
            select(EventType)
            .where(EventType.project_id == project_id, EventType.branch_id == branch_id)
            .options(selectinload(EventType.field_definitions))
            .execution_options(populate_existing=True)
        )
    )
    event_types_by_id = {event_type.id: event_type for event_type in event_types}

    meta_fields = list(
        await session.scalars(
            select(MetaFieldDefinition).where(
                MetaFieldDefinition.project_id == project_id,
                MetaFieldDefinition.branch_id == branch_id,
            )
        )
    )

    events = list(
        await session.scalars(
            select(Event)
            .where(Event.project_id == project_id, Event.branch_id == branch_id)
            .options(
                selectinload(Event.event_type),
                selectinload(Event.field_values).selectinload(EventFieldValue.field_definition),
                selectinload(Event.meta_values).selectinload(EventMetaValue.meta_field_definition),
                selectinload(Event.tags),
            )
            .execution_options(populate_existing=True)
        )
    )

    variables = list(
        await session.scalars(
            select(Variable)
            .where(
                Variable.project_id == project_id,
                Variable.branch_id == branch_id,
            )
            # ``Variable.value_contexts`` is ``lazy="selectin"`` and each context
            # then selectin-loads its FieldDefinition, so without this the reindex
            # pulls the project's whole context table plus every referenced field
            # definition — to build text it assembles from its OWN
            # ``select(VariableValue)`` below. Every variable create, every
            # variable update and every event write reaches this through
            # ``reindex_project_branch``, which makes it the hottest of the
            # tripl-xkbb sites.
            .options(lazyload(Variable.value_contexts))
        )
    )
    # Sorted deterministically: the demo embedding fixture keys documents by
    # sha256(embed_text), so text joined from these rows must not depend on
    # dialect-specific row order (SQLite fixture generation vs Postgres
    # runtime) — the query itself has no ORDER BY.
    variable_values = sorted(
        await session.scalars(
            select(VariableValue)
            .where(
                VariableValue.project_id == project_id,
                VariableValue.branch_id == branch_id,
            )
            .options(
                selectinload(VariableValue.variable),
                selectinload(VariableValue.event),
                selectinload(VariableValue.field_definition),
            )
            .execution_options(populate_existing=True)
        ),
        key=_variable_value_sort_key,
    )
    contexts_by_event_field: dict[tuple[uuid.UUID, uuid.UUID], list[VariableValue]] = {}
    contexts_by_variable: dict[uuid.UUID, list[VariableValue]] = {}
    for context in variable_values:
        contexts_by_event_field.setdefault(
            (context.event_id, context.field_definition_id),
            [],
        ).append(context)
        contexts_by_variable.setdefault(context.variable_id, []).append(context)
    relations = list(
        await session.scalars(
            select(EventTypeRelation)
            .where(
                EventTypeRelation.project_id == project_id,
                EventTypeRelation.branch_id == branch_id,
            )
            .options(
                selectinload(EventTypeRelation.source_event_type),
                selectinload(EventTypeRelation.target_event_type),
                selectinload(EventTypeRelation.source_field),
                selectinload(EventTypeRelation.target_field),
            )
            .execution_options(populate_existing=True)
        )
    )

    # Metrics, fact tables, scans and alert rules are GLOBAL, project-scoped
    # entities (not plan-branched), so they are queried by project_id only; the
    # same documents are duplicated into each branch's index — consistent with
    # the per-branch index design (tripl-dfct). Ordered by name for
    # deterministic output.
    metrics = list(
        await session.scalars(
            select(MetricDefinition)
            .where(MetricDefinition.project_id == project_id)
            .order_by(MetricDefinition.name)
        )
    )
    fact_tables = list(
        await session.scalars(
            select(FactTable).where(FactTable.project_id == project_id).order_by(FactTable.name)
        )
    )
    scan_configs = list(
        await session.scalars(
            select(ScanConfig).where(ScanConfig.project_id == project_id).order_by(ScanConfig.name)
        )
    )
    # An AlertRule has no project_id of its own — it reaches its project through
    # its destination, so this joins rather than filtering directly.
    alert_rules = list(
        await session.scalars(
            select(AlertRule)
            .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
            .where(AlertDestination.project_id == project_id)
            .order_by(AlertRule.name)
        )
    )

    documents: list[BuiltDocument] = []
    for event_type in event_types:
        documents.append(_event_type_document(event_type, slug))
        for field in sorted(event_type.field_definitions, key=lambda item: item.order):
            documents.append(_field_document(field, event_type, slug))

    for meta_field in meta_fields:
        documents.append(_meta_field_document(meta_field, slug))

    for event in events:
        event_type = event_types_by_id.get(event.event_type_id, event.event_type)
        documents.append(_event_document(event, event_type, slug, contexts_by_event_field))
        for tag in event.tags:
            documents.append(_tag_document(tag, event, event_type, slug))

    for variable in variables:
        documents.append(
            _variable_document(variable, slug, contexts_by_variable.get(variable.id, []))
        )

    for relation in relations:
        documents.append(_relation_document(relation, slug))

    for metric in metrics:
        documents.append(_metric_document(metric, slug))

    for fact_table in fact_tables:
        documents.append(_fact_table_document(fact_table, slug))

    scan_config_names = {config.id: config.name for config in scan_configs}
    for scan_config in scan_configs:
        documents.append(_scan_config_document(scan_config, slug))

    for alert_rule in alert_rules:
        documents.append(_alert_rule_document(alert_rule, slug, scan_config_names))

    # Docs catalog notes: global too, the project's own and its organization's
    # (F22). Imported here because that module builds on this one's helpers.
    from tripl.services._search_doc_documents import build_doc_documents

    documents.extend(await build_doc_documents(session, project_id, slug))

    return documents


def _event_type_document(event_type: EventType, slug: str) -> BuiltDocument:
    fields = sorted(event_type.field_definitions, key=lambda field: field.order)
    field_text = _join(
        [
            _join(
                [
                    field.name,
                    field.display_name,
                    field.field_type,
                    field.description,
                    " ".join(field.enum_options or []),
                ]
            )
            for field in fields
        ]
    )
    return BuiltDocument(
        entity_type="event_type",
        entity_id=event_type.id,
        parent_event_id=None,
        title=event_type.display_name,
        subtitle=event_type.name,
        description=_clean(event_type.description),
        body=_join([event_type.description, field_text]),
        keywords=_join(
            [
                event_type.name,
                event_type.display_name,
                _spaced_identifiers([event_type.name]),
            ]
        ),
        route_path=legacy_project_path(slug, f"/events/{event_type.name}"),
    )


def _field_document(field: FieldDefinition, event_type: EventType, slug: str) -> BuiltDocument:
    return BuiltDocument(
        entity_type="field",
        entity_id=field.id,
        parent_event_id=None,
        title=field.display_name or field.name,
        subtitle=f"{event_type.display_name} field",
        description=_clean(field.description),
        body=_join(
            [
                field.name,
                field.description,
                field.field_type,
                "required" if field.is_required else "",
                " ".join(field.enum_options or []),
            ]
        ),
        keywords=_join(
            [
                field.name,
                field.display_name,
                event_type.name,
                event_type.display_name,
                _spaced_identifiers([field.name, event_type.name]),
            ]
        ),
        route_path=legacy_project_path(slug, "/event-types"),
    )


def _meta_field_document(meta_field: MetaFieldDefinition, slug: str) -> BuiltDocument:
    return BuiltDocument(
        entity_type="meta_field",
        entity_id=meta_field.id,
        parent_event_id=None,
        title=meta_field.display_name or meta_field.name,
        subtitle="Meta field",
        body=_join(
            [
                meta_field.name,
                meta_field.field_type,
                "required" if meta_field.is_required else "",
                " ".join(meta_field.enum_options or []),
                meta_field.default_value if not _is_sensitive(meta_field.sensitivity) else "",
            ]
        ),
        keywords=_join(
            [
                meta_field.name,
                meta_field.display_name,
                _spaced_identifiers([meta_field.name]),
            ]
        ),
        route_path=legacy_project_path(slug, "/meta-fields"),
    )


def _variable_value_sort_key(context: VariableValue) -> tuple[str, str, str, str, str]:
    return (
        _clean(context.variable.name),
        _clean(context.event.name),
        _clean(context.field_definition.name),
        _clean(context.source_column),
        _clean(context.value_kind),
    )


def _event_document(
    event: Event,
    event_type: EventType | None,
    slug: str,
    contexts_by_event_field: Mapping[tuple[uuid.UUID, uuid.UUID], list[VariableValue]],
) -> BuiltDocument:
    """Build the search document for one event.

    OBSERVED VALUES ARE BODY TEXT HERE TOO (tripl-0qld)
    ---------------------------------------------------
    ``_variable_document`` stopped joining harvested values into ``keywords``
    for tripl-gbxj; this builder was missed, and it feeds the SAME column that
    two ranking tiers read — the 3.5 literal keyword-token tier and the 3.25
    stemmed-identity tier. Both rest on ``keywords`` carrying an entity's
    IDENTITY and no observed value, the premise stated on the 3.25 tier in
    ``_search_query.postgres_lexical_search``. It was not true for events:
    every ``VariableValue.values`` entry a bound variable had harvested reached
    an EVENT's keywords through
    ``_variable_context_text(..., include_values=True)``.

    MEASURED ON THE RELEVANCE CORPUS, WHICH IS WHAT MAKES THIS A DEFECT AND NOT A
    PREFERENCE: ``${property.screen_name}`` harvests the plurals ``purchases``,
    ``spots`` and ``уловы`` on the ``view_id`` field of ``app_open``,
    ``screen_home`` and ``screen_settings``. So ``q='purchases'`` paid those
    three unrelated pageview events ``keywords ~* '\\mpurchases\\M'`` = 3.5 while
    ``purchase_completed`` — the entity the query is about — reached only the
    3.25 stemmed tier. tripl-nh5s built that ladder to say "a stemmed match on
    the entity's own name outranks the literal token appearing somewhere in its
    body"; for those three the order was inverted, and ``purchase-plural`` was
    green only because the trigram leg covered the 0.25 the ladder gave away.

    So ``keywords`` gets ``authored_values`` and the VALUES-FREE context text,
    while ``body`` keeps ``safe_values`` and the full context text unchanged —
    harvested values are real evidence about the event and stay searchable, they
    are simply not part of its identity.

    WHY ``is_authored`` AND NOT "no values at all"
    ----------------------------------------------
    An ``EventFieldValue`` is not a ``VariableValue``: when a person types a
    field's example value into the spec, ``event_service`` records
    ``is_authored=True`` (:540/:635/:954), and a scan writes ``is_authored=False``
    (``core/analyzers/event_generator.py``:395). That flag IS the curation line,
    and dropping every value instead would demote a hand-typed value from the
    3.5 keyword-token tier to the 3.0 body tier — a user-visible routing change
    pinned by ``tests/test_search.py::test_global_search_matches_multilingual_plan_content``,
    which asserts ``q='завершение покупки'`` is served at ``_SQLITE_KEYWORD_TOKEN``.

    KNOWN HAZARD, WRITTEN DOWN RATHER THAN DISCOVERED LATER: the flag is not
    immutable. ``core/analyzers/_event_generator_merge.py``:304 recomputes it as
    ``fv.is_authored and value == fv.value``, and
    ``plan_branch_revert_service.py``:400 restores it from a snapshot, so a scan
    that observes a different value for a hand-typed field silently demotes that
    value out of ``keywords`` on the next reindex. That is a demotion of a
    curated string to body text, never a promotion of harvested text into
    keywords, so it cannot re-break the premise above — but it does mean a
    ranking can move without anyone editing search code.

    The sensitivity guard is unchanged and still the outer condition: an authored
    value on a ``sensitivity != 'none'`` field must not reach the index at all,
    and nesting the new append inside it is what keeps that true.
    """
    field_names: list[str] = []
    safe_values: list[str] = []
    authored_values: list[str] = []
    variable_context_text: list[str] = []
    variable_context_keywords: list[str] = []
    # field_values / meta_values / tags are selectin relationships with no
    # order_by; iterate them sorted so the embed text (and therefore the demo
    # fixture's sha256 key) is identical across dialects.
    for field_value in sorted(
        event.field_values,
        key=lambda item: (item.field_definition.order, item.field_definition.name),
    ):
        field = field_value.field_definition
        field_names.extend([field.name, field.display_name, field.description])
        if not _is_sensitive(field.sensitivity):
            safe_values.append(field_value.value)
            # Nested inside the sensitivity guard on purpose: it is the outer
            # precondition for ANY value of this field reaching the index, and a
            # sibling `if field_value.is_authored` at loop level would leak a
            # hand-typed secret into keywords and into `embed_text_for`.
            if field_value.is_authored:
                authored_values.append(field_value.value)
            contexts = contexts_by_event_field.get((event.id, field_value.field_definition_id), [])
            variable_context_text.append(
                _variable_context_text(contexts, include_event_names=False)
            )
            variable_context_keywords.append(
                _variable_context_text(
                    contexts,
                    include_event_names=False,
                    include_values=False,
                )
            )

    meta_names: list[str] = []
    safe_meta_values: list[str] = []
    # Names once per FIELD, values once per row. A multi-valued field
    # (tripl-h2sx.31) has several rows, and extending the names inside the loop
    # would repeat "jira" once per key and weight the field by how many tickets
    # happen to be attached to the event.
    seen_meta_fields: set[uuid.UUID] = set()
    for meta_value in sorted(event.meta_values, key=lambda item: item.meta_field_definition.name):
        meta_field = meta_value.meta_field_definition
        if meta_field.id not in seen_meta_fields:
            seen_meta_fields.add(meta_field.id)
            meta_names.extend([meta_field.name, meta_field.display_name])
        if not _is_sensitive(meta_field.sensitivity):
            safe_meta_values.append(meta_value.value)

    tag_names = sorted(tag.name for tag in event.tags)
    event_type_names = []
    if event_type is not None:
        event_type_names = [event_type.name, event_type.display_name, event_type.description]

    return BuiltDocument(
        entity_type="event",
        entity_id=event.id,
        parent_event_id=event.id,
        title=event.name,
        subtitle=event_type.display_name if event_type is not None else "",
        description=_clean(event.description),
        body=_join(
            [
                event.title,
                event.description,
                " ".join(event_type_names),
                " ".join(field_names),
                " ".join(safe_values),
                " ".join(variable_context_text),
                " ".join(meta_names),
                " ".join(safe_meta_values),
                " ".join(tag_names),
            ]
        ),
        # IDENTITY text only (tripl-0qld) — deliberately not "curated", see
        # `_spaced_identifiers` and the 3.25-tier bullet in
        # `_search_query.postgres_lexical_search` for why that word is wrong
        # here. The event's name, its type, its tags, its breakdown columns, the
        # values a person AUTHORED, and the bindings — but not a single observed
        # value. `safe_values` and the values-carrying `variable_context_text`
        # above stay in `body`, which is what the 3.0 body-token tier reads.
        # Read the docstring before putting either of them back: two tiers rest
        # on this column being identity rather than harvested values.
        keywords=_join(
            [
                event.name,
                event.title,
                event.source_name,
                " ".join(event_type_names),
                " ".join(tag_names),
                # ``or []`` matches the two readers in ``event_service.update_event``.
                # The column is NOT NULL and the PATCH schema now turns an explicit
                # null into ``[]`` (tripl-0zpq.190), so nothing can write None here
                # — but a legacy row that did would take out the reindex of the
                # WHOLE branch with a TypeError, where this makes it a blank keyword.
                " ".join(event.metric_breakdown_columns or []),
                " ".join(authored_values),
                " ".join(variable_context_keywords),
                _spaced_identifiers([event.name, event.source_name]),
            ]
        ),
        route_path=legacy_project_path(slug, f"/monitoring/event/{event.id}"),
        archived=(event.status == "archived"),
    )


def _tag_document(
    tag: EventTag,
    event: Event,
    event_type: EventType | None,
    slug: str,
) -> BuiltDocument:
    return BuiltDocument(
        entity_type="tag",
        entity_id=tag.id,
        parent_event_id=event.id,
        title=f"#{tag.name}",
        subtitle=event.name,
        description=_clean(event.description),
        body=_join(
            [
                tag.name,
                event.name,
                event.description,
                event_type.display_name if event_type is not None else "",
            ]
        ),
        # Deliberately NOT run through ``_spaced_identifiers`` (tripl-h9x2): a
        # tag document's keywords are exactly the tag name, which is what the
        # 4.0 "keywords ARE the query" tier compares for equality. Appending an
        # alias here would delete that tier for every tag.
        keywords=tag.name,
        route_path=legacy_project_path(slug, f"/monitoring/event/{event.id}"),
        archived=(event.status == "archived"),
    )


def _variable_document(
    variable: Variable,
    slug: str,
    contexts: list[VariableValue],
) -> BuiltDocument:
    """Build the search document for one variable.

    HARVESTED VALUES ARE BODY TEXT, NOT KEYWORDS (tripl-gbxj)
    ---------------------------------------------------------
    A variable is bound to many (event, field) contexts, and every context
    repeats the variable name, the event name, the source column AND all of its
    observed values. This document used to put that text into ``body`` and into
    ``keywords``, and then append a deduplicated copy of the values on top — so
    a value appeared roughly ``2 * contexts + 1`` times in the indexed text of a
    single document, while the entity actually named after it appeared once.

    That is what an unbounded ``ts_rank_cd`` was multiplying (see
    ``_search_query.postgres_lexical_search``). Measured: one production project
    holds 1526 auto-detected "variables" that are really harvested field values,
    and ``q='spot'`` returned ``${property.spot_id}`` at 73.69 and
    ``${property.cube}`` at 55.68 — the latter only because one harvested value
    is ``spot:reload:bento`` — while the events named ``spot`` and
    ``screen_spot`` did not appear at all.

    Keywords are the high-weight field, and a value someone's app happened to
    emit is not a keyword of the variable: the variable's own name, the columns
    it comes from and the events it is bound to are. So the values stay in
    ``body`` — searching a concrete observed value still finds both the variable
    and its owning event, which is behaviour the product documents — and they
    are simply no longer joined into ``keywords``. Both halves of tripl-gbxj had
    to land together: the measured 73.69-vs-4.55 gap was the duplication and the
    missing normalization compounding.

    THIS RULE IS NOW SYMMETRIC (tripl-0qld)
    ---------------------------------------
    tripl-gbxj drew the line here and nowhere else, so ``_event_document`` went
    on joining the SAME harvested values — the ones it picks up through the
    contexts bound to its own fields — into an EVENT's ``keywords``. The two
    builders now draw the identical line, which is what lets the 3.5 and 3.25
    tiers say anything about the column at all. If you add a third builder that
    reads ``_variable_context_text``, decide which column its values belong in
    before writing the join, not after the harness disagrees with you.
    """
    context_text = _variable_context_text(contexts, include_event_names=True)
    context_keywords = _variable_context_text(
        contexts,
        include_event_names=True,
        include_values=False,
    )
    return BuiltDocument(
        entity_type="variable",
        entity_id=variable.id,
        parent_event_id=None,
        title=f"${{{variable.name}}}",
        subtitle=variable.variable_type,
        description=_clean(variable.description),
        body=_join([variable.name, variable.source_name, variable.description, context_text]),
        keywords=_join(
            [
                variable.name,
                variable.source_name,
                context_keywords,
                _spaced_identifiers([variable.name, variable.source_name]),
            ]
        ),
        route_path=legacy_project_path(slug, "/variables"),
    )


def _variable_context_text(
    contexts: list[VariableValue],
    *,
    include_event_names: bool,
    include_values: bool = True,
) -> str:
    """Flatten a variable's bindings into indexable text.

    ``include_values=False`` yields the same text without the observed values,
    which is what a variable's ``keywords`` is built from (tripl-gbxj) and, since
    tripl-0qld, an EVENT's keywords too: the binding is a keyword of both, the
    harvested value is a keyword of neither. Sensitive fields never contribute
    values under either setting.

    WHAT ``include_values=False`` STILL LETS THROUGH, SAID PLAINLY
    --------------------------------------------------------------
    ``context.value_kind`` and ``str(context.observed_count)`` below are written
    by the scanner, not by a person, and they reach ``keywords`` under BOTH
    settings. So "keywords holds only text a human wrote" is false even after
    tripl-0qld, and ``postgres_lexical_search``'s docstring says the narrower
    true thing instead of the tidy one. Removing them is a separate change with
    its own blast radius — it moves every variable document's keywords, which
    the relevance harness ranks — and it was not folded in here so that this
    commit's effect on the harness stays attributable.
    """
    parts: list[str] = []
    for context in contexts:
        field = context.field_definition
        include_field_values = include_values and not _is_sensitive(field.sensitivity)
        safe_values = " ".join(context.values or []) if include_field_values else ""
        parts.append(
            _join(
                [
                    context.variable.name,
                    context.variable.source_name,
                    context.source_column,
                    context.value_kind,
                    str(context.observed_count),
                    context.event.name if include_event_names else "",
                    field.name,
                    field.display_name,
                    safe_values,
                ]
            )
        )
    return _join(parts)


def _relation_document(relation: EventTypeRelation, slug: str) -> BuiltDocument:
    source_type = relation.source_event_type
    target_type = relation.target_event_type
    source_field = relation.source_field
    target_field = relation.target_field
    return BuiltDocument(
        entity_type="relation",
        entity_id=relation.id,
        parent_event_id=None,
        title=f"{source_type.display_name} -> {target_type.display_name}",
        subtitle=relation.relation_type,
        description=_clean(relation.description),
        body=_join(
            [
                relation.description,
                source_type.name,
                source_type.display_name,
                target_type.name,
                target_type.display_name,
                source_field.name,
                source_field.display_name,
                target_field.name,
                target_field.display_name,
            ]
        ),
        keywords=_join(
            [
                relation.relation_type,
                source_field.name,
                target_field.name,
                _spaced_identifiers([source_field.name, target_field.name]),
            ]
        ),
        route_path=legacy_project_path(slug, "/relations"),
    )


def _scan_config_document(scan_config: ScanConfig, slug: str) -> BuiltDocument:
    """A scan configuration, findable by name and by the columns it is wired to.

    Route is the scan's own page rather than the list, so a hit lands where the
    configuration can be read (App.tsx registers /p/:slug/scans/:scanId).
    """
    return BuiltDocument(
        entity_type="scan_config",
        entity_id=scan_config.id,
        parent_event_id=None,
        title=scan_config.name,
        subtitle=_clean(scan_config.interval),
        description="",
        body=_join(
            [
                scan_config.event_type_column,
                scan_config.event_name_format,
                scan_config.time_column,
                # The warehouse SQL, so "which scan reads table X" is answerable.
                scan_config.base_query,
                scan_config.platform_column,
                scan_config.app_version_column,
            ]
        ),
        keywords=_join([scan_config.name, _spaced_identifiers([scan_config.name])]),
        route_path=legacy_project_path(slug, f"/scans/{scan_config.id}"),
        archived=False,
    )


def _alert_rule_document(
    alert_rule: AlertRule,
    slug: str,
    scan_config_names: dict[uuid.UUID, str],
) -> BuiltDocument:
    """An alert rule, findable by name and by the wording of its templates.

    The templates are the part worth indexing beyond the name: they are text a
    human wrote, so "which rule says 'investigate immediately'" is answerable.
    The include_* flags are deliberately NOT folded in — they are booleans whose
    field names would match every rule equally and add no discrimination.

    A disabled rule is still indexed and NOT marked archived: "why am I not
    getting alerts about X" is exactly when someone searches for it, and marking
    it archived would hide it from the default search that person runs.
    """
    return BuiltDocument(
        entity_type="alert_rule",
        entity_id=alert_rule.id,
        parent_event_id=None,
        title=alert_rule.name,
        subtitle=_clean(
            scan_config_names.get(alert_rule.scan_config_id, "")
            if alert_rule.scan_config_id is not None
            else ""
        ),
        description="",
        body=_join([alert_rule.message_template, alert_rule.items_template]),
        keywords=_join([alert_rule.name, _spaced_identifiers([alert_rule.name])]),
        route_path=legacy_project_path(slug, "/alerting"),
        archived=False,
    )


def _metric_document(metric: MetricDefinition, slug: str) -> BuiltDocument:
    return BuiltDocument(
        entity_type="metric",
        entity_id=metric.id,
        parent_event_id=None,
        title=metric.display_name,
        subtitle=metric.name,
        description=_clean(metric.description),
        body=_join(
            [
                metric.description,
                metric.kind,
                metric.aggregation,
                metric.composition,
                metric.unit,
                " ".join(metric.breakdown_columns or []),
                metric.app_version_column,
                metric.platform_column,
            ]
        ),
        keywords=_join(
            [
                metric.name,
                metric.display_name,
                metric.unit,
                metric.kind,
                _spaced_identifiers([metric.name]),
            ]
        ),
        route_path=legacy_project_path(slug, f"/monitoring/metric/{metric.id}"),
        archived=(metric.status == "archived"),
    )


def _fact_table_document(fact_table: FactTable, slug: str) -> BuiltDocument:
    column_names = [
        column["name"]
        for column in (fact_table.columns or [])
        if isinstance(column, dict) and "name" in column
    ]
    filter_names = [
        row_filter["name"]
        for row_filter in (fact_table.row_filters or [])
        if isinstance(row_filter, dict) and "name" in row_filter
    ]
    return BuiltDocument(
        entity_type="fact_table",
        entity_id=fact_table.id,
        parent_event_id=None,
        title=fact_table.display_name,
        subtitle=fact_table.name,
        description=_clean(fact_table.description),
        body=_join(
            [
                fact_table.description,
                fact_table.timestamp_column,
                " ".join(column_names),
                " ".join(fact_table.identifier_columns or []),
                " ".join(filter_names),
            ]
        ),
        keywords=_join(
            [
                fact_table.name,
                fact_table.display_name,
                " ".join(column_names),
                _spaced_identifiers([fact_table.name, *column_names]),
            ]
        ),
        route_path=legacy_project_path(slug, f"/metrics/fact-tables/{fact_table.id}/edit"),
    )
