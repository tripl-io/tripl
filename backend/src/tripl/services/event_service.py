import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, noload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from tripl import cache
from tripl.alerting_matching import rule_covers_event
from tripl.core.name_template import (
    VARIABLE_TOKEN_PATTERN,
    apply_name_format,
    resolve_dotted_keys,
)
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.event import Event, EventStatus
from tripl.models.event_change import (
    EVENT_CHANGE_SOURCE_SCAN,
    SCAN_AUTHOR_LABEL,
    EventChange,
    create_event_change,
)
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_metric import EventMetric
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.schemas.event import (
    META_VALUE_MAX_BYTES,
    EventBulkDelete,
    EventBulkUpdate,
    EventCreate,
    EventFieldValueIn,
    EventIdentityHolder,
    EventIdentityHoldersResponse,
    EventMetaValueIn,
    EventMove,
    EventReorder,
    EventUpdate,
)
from tripl.services import notification_announce, subscription_service
from tripl.services._branch_counterparts import (
    attach_main_last_seen,
    main_counterparts,
    metrics_row_for,
)
from tripl.services._branch_event_threads import rescue_branch_event_threads
from tripl.services._event_health_sort import order_page_by_health
from tripl.services._event_reference_cleanup import drop_dangling_event_references
from tripl.services.event_comment_service import (
    events_with_open_questions,
    open_question_counts,
)
from tripl.services.lifecycle_service import attach_event_findings, attach_list_warnings
from tripl.services.plan_branch_service import ensure_main_branch_id, resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.scan_config_lookup import (
    governing_name_format,
    load_governing_scan_configs,
)
from tripl.services.schema_drift_service import get_drift_counts_by_event_type
from tripl.services.search_service import (
    _queue_embedding_refresh,
    _reindex_branch_documents,
)
from tripl.services.variable_value_service import attach_event_field_variable_values

HEALTH_SORT_MAIN_ONLY = "Health sort is available on the main plan"

_TRACKED_FIELDS = (
    "status",
    "name",
    "title",
    "description",
    "required_presence_threshold",
    "sunset_at",
    "superseded_by_event_id",
)
# One ``${token}`` grammar for the codebase; this module's spelling is the one
# it standardised on (``core.name_template``).
_TEMPLATE_TOKEN_PATTERN = VARIABLE_TOKEN_PATTERN
# The token body is EVERYTHING A SCAN CAN WRITE, not an identifier. A JSON-path
# variable is only rewritten to its sanitised display name when it has one:
# ``derive_display_name`` falls back to the raw path once every candidate is
# taken (a second whitespace-only or all-non-Latin key under the same parent
# reduces to the same candidate), an excluded path is skipped by normalisation
# entirely, and a legacy variable's name IS a raw path. So a scan-written value
# legitimately holds ``${property.Москва}``, ``${property.  }`` or
# ``${property.Albany, OR}``. The event form re-sends every field value on save,
# so an identifier grammar here 422'd every later edit of such an event —
# including one that only touched the description — until somebody hand-edited
# the JSON (tripl-0zpq.125).
#
# What stays out, and why: ``}`` ends the token; ``"``, ``\`` and any C0 control
# character (U+0000–U+001F, so a raw tab, newline or carriage return) are all
# illegal inside a JSON string, and a token is spliced back into the dumped text
# VERBATIM rather than re-escaped — while the stash happens before
# ``json.loads`` ever sees the value, so this function would keep accepting the
# result on every later save while ``json.loads`` and the browser's
# ``JSON.parse`` both rejected it. An empty ``${}`` names no variable at all.
_JSON_TEMPLATE_TOKEN_NAME_PATTERN = re.compile(r'^[^"\\}\x00-\x1f]+$')
_JSON_TEMPLATE_VALUE_PATTERN = re.compile(r'"\$\{[^"\\}\x00-\x1f]+\}"|\$\{[^"\\}\x00-\x1f]+\}')


def _normalize_json_template_value(field: FieldDefinition, value: str) -> str:
    """Validate and canonically dump JSON while preserving ``${variable}`` values.

    Template values may occupy a complete JSON value either with quotes or
    without them.  Temporarily replacing them with distinct JSON strings lets
    ``json.loads`` validate the surrounding JSON; restoring the original token
    after ``json.dumps`` keeps the authored template intact.
    """
    tokens = [match.group(1) for match in _TEMPLATE_TOKEN_PATTERN.finditer(value)]
    if any(not _JSON_TEMPLATE_TOKEN_NAME_PATTERN.fullmatch(token) for token in tokens):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Field '{field.display_name}' has an invalid variable token; "
                "a ${...} token must name a variable and cannot contain a "
                "quote, a backslash or a control character"
            ),
        )

    placeholders: dict[str, str] = {}

    def _stash(match: re.Match[str]) -> str:
        sentinel = f"__TRIPL_JSON_TEMPLATE_{uuid.uuid4().hex}__"
        while sentinel in value or sentinel in placeholders:
            sentinel = f"__TRIPL_JSON_TEMPLATE_{uuid.uuid4().hex}__"
        placeholders[sentinel] = match.group(0)
        return f'"{sentinel}"'

    def _reject_nonstandard_constant(constant: str) -> None:
        raise ValueError(f"{constant} is not valid JSON")

    def _has_template_key(node: object) -> bool:
        if isinstance(node, list):
            return any(_has_template_key(item) for item in node)
        if isinstance(node, dict):
            return any(key in placeholders or _has_template_key(item) for key, item in node.items())
        return False

    try:
        safe_value = _JSON_TEMPLATE_VALUE_PATTERN.sub(_stash, value)
        if len(placeholders) != len(tokens):
            raise ValueError("Variable templates must occupy a complete JSON value")
        parsed = json.loads(safe_value, parse_constant=_reject_nonstandard_constant)
        if _has_template_key(parsed):
            raise ValueError("Variable templates cannot be JSON object keys")
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Field '{field.display_name}' must contain valid JSON: {exc}",
        ) from exc

    def _dump(node: object) -> str:
        if isinstance(node, str):
            return placeholders.get(node, json.dumps(node, ensure_ascii=False))
        if isinstance(node, list):
            return "[" + ", ".join(_dump(item) for item in node) + "]"
        if isinstance(node, dict):
            return (
                "{"
                + ", ".join(
                    f"{json.dumps(key, ensure_ascii=False)}: {_dump(item)}"
                    for key, item in node.items()
                )
                + "}"
            )
        return json.dumps(node, ensure_ascii=False, allow_nan=False)

    try:
        return _dump(parsed)
    except RecursionError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Field '{field.display_name}' must contain valid JSON: nesting is too deep",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Field '{field.display_name}' must contain valid JSON: {exc}",
        ) from exc


async def _attach_template_warnings(session: AsyncSession, event: Event) -> None:
    """Attach advisory warnings for complete template tokens unknown in the event branch.

    Three COLUMNS, not the mapped ``Variable``: ``Variable.value_contexts`` is
    ``lazy="selectin"`` and every ``VariableValue`` it brings back selectin-loads
    its own ``FieldDefinition``, so selecting the entity dragged the branch's
    entire variable graph — each context row with its JSON ``values`` list —
    into the session on the request path of EVERY event create and PATCH, to
    read a name, a source name and a bindings list (tripl-0zpq.129). A column
    select has no relationships to load, so the three round trips are one.
    """
    result = await session.execute(
        select(Variable.name, Variable.source_name, Variable.bindings).where(
            Variable.project_id == event.project_id,
            Variable.branch_id == event.branch_id,
        )
    )
    known_tokens = {
        token
        for name, source_name, bindings in result.all()
        for token in (name, source_name, *(bindings or []))
        if token
    }
    unknown_tokens = {
        match.group(1)
        for value in [
            *(field_value.value for field_value in event.field_values),
            *(meta_value.value for meta_value in event.meta_values),
        ]
        for match in _TEMPLATE_TOKEN_PATTERN.finditer(value)
        if match.group(1) not in known_tokens
    }
    event.warnings = [f"Unknown variable token: ${{{token}}}" for token in sorted(unknown_tokens)]  # type: ignore[attr-defined]


def _record_changes(
    session: AsyncSession,
    *,
    event: Event,
    old_values: dict[str, object],
    new_values: dict[str, object],
    user_id: uuid.UUID | None,
) -> None:
    for field, new_val in new_values.items():
        old_val = old_values.get(field)
        old_str = str(old_val) if old_val is not None else None
        new_str = str(new_val) if new_val is not None else None
        if old_str != new_str:
            session.add(
                create_event_change(
                    event_id=event.id,
                    user_id=user_id,
                    field=field,
                    old_value=old_str,
                    new_value=new_str,
                )
            )


def _record_list_change(
    session: AsyncSession,
    *,
    event: Event,
    field: str,
    old: Sequence[str],
    new: Sequence[str],
    user_id: uuid.UUID | None,
) -> None:
    """One history row for a list-valued attribute (tags), joined for reading."""
    if sorted(old) == sorted(new):
        return
    session.add(
        create_event_change(
            event_id=event.id,
            user_id=user_id,
            field=field,
            old_value=", ".join(sorted(old)) or None,
            new_value=", ".join(sorted(new)) or None,
        )
    )


def _record_keyed_changes(
    session: AsyncSession,
    *,
    event: Event,
    prefix: str,
    names: Mapping[uuid.UUID, str],
    old: Mapping[uuid.UUID, str],
    new: Mapping[uuid.UUID, str],
    user_id: uuid.UUID | None,
) -> None:
    """One history row per field or meta value whose text changed.

    Rows are keyed ``field:<name>`` / ``meta:<name>`` so the history reads as
    the same list the form shows. Until tripl-kjhi.9 only name, description,
    status and sunset date were recorded, and an event whose Jira key or
    screen value changed hands had an empty history.
    """
    for definition_id in sorted(set(old) | set(new), key=lambda key: names.get(key, "")):
        before = old.get(definition_id)
        after = new.get(definition_id)
        if before == after:
            continue
        session.add(
            create_event_change(
                event_id=event.id,
                user_id=user_id,
                field=f"{prefix}:{names.get(definition_id, str(definition_id))}",
                old_value=before,
                new_value=after,
            )
        )


async def _load_meta_field_definitions(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    meta_field_definition_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, tuple[str | None, bool, str]]:
    """Link template, allow-multiple and name per meta field, ON THIS BRANCH.

    Split out from :func:`_normalize_meta_values_against` so the bulk paste can
    load every definition it references in one query and then refuse a bad item
    without touching the database again — see ``bulk_create_events``.
    """
    wanted = set(meta_field_definition_ids)
    if not wanted:
        return {}
    rows = await session.execute(
        select(
            MetaFieldDefinition.id,
            MetaFieldDefinition.link_template,
            MetaFieldDefinition.allow_multiple,
            MetaFieldDefinition.name,
        ).where(
            MetaFieldDefinition.id.in_(wanted),
            MetaFieldDefinition.project_id == project_id,
            MetaFieldDefinition.branch_id == branch_id,
        )
    )
    return {
        definition_id: (template, allow_multiple, name)
        for definition_id, template, allow_multiple, name in rows.all()
    }


def _normalize_meta_values_against(
    definitions: Mapping[uuid.UUID, tuple[str | None, bool, str]],
    meta_values: Sequence[EventMetaValueIn],
) -> list[EventMetaValueIn]:
    """Store what the link template will be applied TO, never the whole link.

    A meta field with a ``link_template`` such as
    ``https://jira.example/browse/${value}`` renders its value into that
    template, so the value should be ``PROJ-4770``. People paste the full URL
    from the browser — on production every branch-authored event held the
    whole address, and the rendered link was the template applied to a URL
    (tripl-kjhi.5). When the pasted text is exactly the template around some
    value, keep only the value; anything else is stored as typed.

    ``definitions`` holds the branch's OWN meta fields, and an id absent from it
    is a 422: a branch deep-copies every definition under a NEW id, so main's id
    posted with ``?branch=`` used to fall through to a ``(None, False, "")``
    default — no link stripping, the "one value here" rule off, and a row on the
    branch pointing at another branch's definition (tripl-0zpq.123).

    The byte cap is applied to the STRIPPED value, which is the one that goes
    into the unique index the cap exists for — see ``META_VALUE_MAX_BYTES``.
    """
    if not meta_values:
        return list(meta_values)
    out_of_scope = sorted(
        {mv.meta_field_definition_id for mv in meta_values} - definitions.keys(), key=str
    )
    if out_of_scope:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Meta field {out_of_scope[0]} is not on this project's branch; "
                "read the meta fields with the same ?branch= this event is written to"
            ),
        )
    out: list[EventMetaValueIn] = []
    # A field opted in to several values may repeat; one that did not may not.
    # The row constraint stopped counting fields when it started counting values
    # (tripl-h2sx.31), so the "one value here" rule lives here now, where the
    # definition says whether it applies.
    seen: dict[uuid.UUID, set[str]] = {}
    for mv in meta_values:
        template, allow_multiple, name = definitions[mv.meta_field_definition_id]
        stripped = strip_link_template(template or "", mv.value)
        weight = len(stripped.encode("utf-8"))
        if weight > META_VALUE_MAX_BYTES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Meta field '{name}' would store {weight} bytes; the stored value is "
                    f"part of a unique index and cannot exceed {META_VALUE_MAX_BYTES} bytes"
                ),
            )
        already = seen.setdefault(mv.meta_field_definition_id, set())
        if already and not allow_multiple:
            raise HTTPException(
                status_code=422,
                detail=f"Meta field '{name}' holds one value; enable Allow multiple for more",
            )
        # An exact repeat is the same fact twice, not a second value — and it
        # would collide on uq_event_meta_value_event_meta_value.
        if stripped in already:
            continue
        already.add(stripped)
        out.append(mv if stripped == mv.value else mv.model_copy(update={"value": stripped}))
    return out


async def _normalize_meta_values(
    session: AsyncSession,
    meta_values: Sequence[EventMetaValueIn],
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
) -> list[EventMetaValueIn]:
    """The one-event shape of :func:`_normalize_meta_values_against`.

    Loads this event's branch's own definitions, then applies every rule that
    function documents: the link-template strip, the branch scoping that refuses
    another branch's definition, the "one value here" rule and the byte cap.
    The bulk paste loads once for the whole paste instead — see
    ``bulk_create_events``.
    """
    if not meta_values:
        return list(meta_values)
    definitions = await _load_meta_field_definitions(
        session,
        project_id=project_id,
        branch_id=branch_id,
        meta_field_definition_ids={mv.meta_field_definition_id for mv in meta_values},
    )
    return _normalize_meta_values_against(definitions, meta_values)


def _meta_by_definition(pairs: Iterable[tuple[uuid.UUID, str]]) -> dict[uuid.UUID, str]:
    """One activity-log line per meta field, its values joined.

    The log is keyed by field, so a multi-valued field would otherwise have its
    entries overwrite each other under one key and report the last one as the
    whole change. "PROJ-4770, PROJ-5012" is what actually happened.
    """
    grouped: dict[uuid.UUID, list[str]] = {}
    for definition_id, value in pairs:
        grouped.setdefault(definition_id, []).append(value)
    return {definition_id: ", ".join(values) for definition_id, values in grouped.items()}


def strip_link_template(template: str, value: str) -> str:
    """``value`` with the template's fixed text removed when it wraps it exactly."""
    if "${value}" not in template:
        return value
    prefix, suffix = template.split("${value}", 1)
    text = value.strip()
    if (
        prefix
        and text.startswith(prefix)
        and text.endswith(suffix)
        and len(text) > len(prefix) + len(suffix)
    ):
        return text[len(prefix) : len(text) - len(suffix)] if suffix else text[len(prefix) :]
    return value


async def _definition_names(
    session: AsyncSession,
    model: type[FieldDefinition] | type[MetaFieldDefinition],
    ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (await session.execute(select(model.id, model.name).where(model.id.in_(ids)))).all()
    return {row_id: name for row_id, name in rows}


def _authored_after_edit(before: tuple[str, bool] | None, value: str) -> bool:
    """Authored when typed or changed; otherwise whatever the row already said."""
    if before is None:
        return True
    previous_value, was_authored = before
    return True if previous_value != value else was_authored


async def _validate_field_values(
    session: AsyncSession,
    event_type_id: uuid.UUID,
    field_values: list[EventFieldValueIn],
    *,
    enforce_required: bool = True,
) -> list[EventFieldValueIn]:
    result = await session.execute(
        select(FieldDefinition).where(FieldDefinition.event_type_id == event_type_id)
    )
    field_defs = {fd.id: fd for fd in result.scalars().all()}

    return _check_and_normalize_field_values(
        field_defs, field_values, enforce_required=enforce_required
    )


def _check_and_normalize_field_values(
    field_defs: dict[uuid.UUID, FieldDefinition],
    field_values: list[EventFieldValueIn],
    *,
    enforce_required: bool = True,
) -> list[EventFieldValueIn]:
    """Required/known checks plus JSON template normalisation, given loaded defs.

    Split out of ``_validate_field_values`` so ``bulk_create_events`` can run the
    same rule off its one batched SELECT instead of restating it. Its own copy
    had drifted: it repeated the two checks and silently skipped the JSON
    normalisation, so the same payload stored a different value depending on
    which door it came through (tripl-u2h9.11).

    ``enforce_required=False`` is the ``scan_identity`` create: the caller
    already knows the identity the warehouse observed and carries no field
    values at all, so a required field would refuse an event the scan has
    ALREADY seen, on a form that has nowhere to type one. Name generation is
    skipped there for exactly that reason; the required check had been left
    behind, which turned every Reconciliation → Accept on a type with one
    required field into a 422 (tripl-0zpq.130). Everything else still applies:
    an unknown field definition is refused and JSON values are normalised.
    """
    provided_ids = {fv.field_definition_id for fv in field_values}
    for fd_id, fd in field_defs.items():
        if enforce_required and fd.is_required and fd_id not in provided_ids:
            raise HTTPException(status_code=422, detail=f"Required field '{fd.name}' is missing")
    normalized_values: list[EventFieldValueIn] = []
    for fv in field_values:
        if fv.field_definition_id not in field_defs:
            raise HTTPException(
                status_code=422, detail=f"Field definition {fv.field_definition_id} not found"
            )
        field = field_defs[fv.field_definition_id]
        normalized_values.append(
            fv.model_copy(
                update={
                    "value": _normalize_json_template_value(field, fv.value)
                    if field.field_type == "json"
                    else fv.value
                }
            )
        )
    return normalized_values


async def _event_type_ids_in_branch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_ids: Iterable[uuid.UUID],
) -> set[uuid.UUID]:
    """Which of these event type ids actually live on this project AND branch.

    ``uq_event_scan_identity`` is ``(event_type_id, source_name)`` and nothing
    else, because an event type lives on exactly one branch of one project — so
    the type id alone was MEANT to scope a scan identity. Nothing enforced it:
    ``events.event_type_id`` is a plain FK with no composite key behind it, and
    the router hands the request's id straight to the service.

    An agent that listed event types with no ``branch_id`` got MAIN's ids, and
    posting one of them with ``?branch=B`` wrote a row on B holding main's
    identity. The identity probe is branch-scoped, so it saw nothing and
    allowed it; the scan's own INSERT for that identity then hit the unique key,
    and ``insert_event_claiming_identity`` — which selects the holder with no
    branch term — wrote main's field values, contexts and metrics onto the
    BRANCH row, leaving main without the event entirely. Accepting the shadow
    candidate for it answered 500, and merging B raised ``KeyError`` on a type
    id main's side of the merge had never heard of (tripl-0zpq.123).
    """
    wanted = set(event_type_ids)
    if not wanted:
        return set()
    rows = await session.execute(
        select(EventType.id).where(
            EventType.id.in_(wanted),
            EventType.project_id == project_id,
            EventType.branch_id == branch_id,
        )
    )
    return set(rows.scalars().all())


def _event_type_out_of_branch_detail(event_type_id: uuid.UUID) -> str:
    """The 422 both create doors answer with, naming the fix as well as the fault."""
    return (
        f"Event type {event_type_id} is not on this project's branch; "
        "read the event types with the same ?branch= this event is written to"
    )


async def _guard_event_type_in_branch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_id: uuid.UUID,
) -> None:
    """The single-event shape of :func:`_event_type_ids_in_branch`."""
    in_branch = await _event_type_ids_in_branch(
        session, project_id=project_id, branch_id=branch_id, event_type_ids=[event_type_id]
    )
    if event_type_id not in in_branch:
        raise HTTPException(status_code=422, detail=_event_type_out_of_branch_detail(event_type_id))


def _twin_reads_for_branch_rows(
    *, project_id: uuid.UUID, main_branch_id: uuid.UUID
) -> tuple[ColumnElement[datetime | None], ColumnElement[uuid.UUID]]:
    """What a branch row has to read on its MAIN twin: last seen, and the metric id.

    Everything a warehouse produces lands on main — ``event_metrics`` rows and
    ``last_seen_at`` bumps alike — because a scan only ever sees main. A branch
    copy is stamped with the twin's ``last_seen_at`` once, at deep-copy time,
    and never bumped again; it receives no metric row at all. So a filter or a
    sort reading the branch row's OWN columns answers about the copy rather than
    about the event: "Silent > N days" matched every copy as soon as the branch
    itself was N days old — beside a Last seen column showing the twin's fresh
    timestamp through ``attach_main_last_seen`` — and "Busiest first" gave every
    branch row volume 0 and fell back to id order (tripl-0zpq.124).

    The pairing is the one the merge, the discussion and ``_branch_counterparts``
    use: the event type's NAME (a branch type carries an id of its own) and the
    scan identity — ``source_name`` where the row has one, ``name`` where it does
    not, on both sides. ``nullif(source_name, '')`` because ``_identity`` over
    there is ``source_name or name``, so an empty ``source_name`` — which a scan
    whose ``event_name_format`` renders to nothing writes — falls through to the
    name in Python, where a bare ``coalesce`` would have kept the empty string.

    ONE twin, the SAME one for both expressions: the lowest id among the main
    rows that answer the key. Nothing stops main from holding two rows under one
    (event type, identity), and nothing refuses that state — a branch COPY
    now reads the main row it came from (``origin_id``, tripl-0zpq.292), but a
    row created on the branch still pairs by key — and while the last-seen
    read took
    ``max()`` over the pair and the metric id took the lowest id, the "Silent >
    N days" filter and the "Busiest first" sort could answer about two different
    main rows on the same branch row. ``_branch_counterparts.main_counterparts``,
    which fills the Last seen the list RENDERS, now reads the same lowest-id twin
    — its ``select(Event)`` is ordered by id and the key keeps the first row — so
    all three agree. If that rule ever moves, it has to move in both places at
    once or the filter and the column go back to disagreeing.

    Correlated scalar subqueries, not joins, and each is one for its own reason:
    ``twin_last_seen`` goes into the COUNT as well as the page, where a join
    would multiply any row whose identity two main rows both answer to and
    inflate ``total`` past what the list can ever show; ``metric_event_id`` never
    reaches the COUNT at all — it is a per-row expression and so cannot be a
    join key (see the volume branch in ``list_events``). The price, measured by
    reading the indexes rather than by running it: the identity is an expression
    on BOTH sides, so no index on ``events`` can serve the equality, and
    ``ix_events_project_branch_status`` narrows each evaluation to main's rows of
    this project rather than to one of them. That is one such scan per candidate
    row, twice over for ``silent_since_days`` because the clause is applied to
    the COUNT too. Two ways out if a large project ever feels it, both outside
    this module: materialise the pairing once per request the way
    ``attach_main_last_seen`` already does and feed id sets into the WHERE (the
    ``has_open_questions`` arm in ``list_events`` takes exactly that shape), or
    add an expression index on
    ``(project_id, branch_id, coalesce(nullif(source_name, ''), name))``.
    """
    twin = aliased(Event)
    twin_type = aliased(EventType)
    branch_type_name = (
        select(EventType.name)
        .where(EventType.id == Event.event_type_id)
        .correlate(Event)
        .scalar_subquery()
    )
    # A branch copy's twin is the main row it was copied from; only a row
    # without a live origin — authored on the branch, or copied from a row main
    # has since deleted — pairs by type name and identity. The same rule, in
    # the same order, as ``main_counterparts`` (tripl-0zpq.292).
    origin_row = aliased(Event)
    origin_is_live = (
        select(origin_row.id)
        .where(origin_row.id == Event.origin_id, origin_row.branch_id == main_branch_id)
        .correlate(Event)
        .exists()
    )
    paired = (
        twin.project_id == project_id,
        twin.branch_id == main_branch_id,
        or_(
            twin.id == Event.origin_id,
            and_(
                ~origin_is_live,
                twin_type.name == branch_type_name,
                func.coalesce(func.nullif(twin.source_name, ""), twin.name)
                == func.coalesce(func.nullif(Event.source_name, ""), Event.name),
            ),
        ),
    )
    twin_last_seen = (
        select(twin.last_seen_at)
        .select_from(twin)
        .join(twin_type, twin_type.id == twin.event_type_id)
        .where(*paired)
        .order_by(twin.id.asc())
        .limit(1)
        .correlate(Event)
        .scalar_subquery()
    )
    # The same subquery shape as above, so the two can never name different main
    # rows. Falls back to the row's own id so the expression is also correct for
    # a row that exists only on this branch — it has no twin, and no metrics.
    metric_event_id = func.coalesce(
        select(twin.id)
        .select_from(twin)
        .join(twin_type, twin_type.id == twin.event_type_id)
        .where(*paired)
        .order_by(twin.id.asc())
        .limit(1)
        .correlate(Event)
        .scalar_subquery(),
        Event.id,
    )
    return twin_last_seen, metric_event_id


async def list_events(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID | None = None,
    search: str | None = None,
    status: list[str] | None = None,
    tag: str | None = None,
    offset: int = 0,
    limit: int = 200,
    silent_since_days: int | None = None,
    field_value: str | None = None,
    meta_value: str | None = None,
    reviewed: bool | None = None,
    has_open_questions: bool | None = None,
    branch_id: uuid.UUID | None = None,
    order_by: str = "catalog",
) -> tuple[list[Event], int]:
    project_id = await resolve_project_id(session, slug)
    requested_branch_id = branch_id
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    # Three of the controls below answer about the EVENT rather than about the
    # row: on a branch its discussion, its last seen and its volume all live on
    # the main twin, so each of them needs main's branch id before the query is
    # built. A read that named no branch already holds it — ``resolve_branch_id``
    # answers a ``None`` override with ``ensure_main_branch_id``, and
    # ``BranchIdDep`` yields ``None`` for main whether or not the client spelled
    # it out — so the ordinary catalog read pays nothing for this line. A read
    # that named a branch pays one more indexed ``plan_branches`` SELECT:
    # ``ensure_main_branch_id`` memoizes nothing and ``attach_main_last_seen``
    # at the end of this function resolves its own copy.
    main_branch_id = (
        branch_id
        if requested_branch_id is None
        else await ensure_main_branch_id(session, project_id)
    )
    if order_by == "health" and branch_id != main_branch_id:
        # Health is scored on the main plan only (F15, #268): scans write the
        # facts it reads for main rows, so a branch copy has no score to sort by.
        raise HTTPException(status_code=400, detail=HEALTH_SORT_MAIN_ONLY)
    twin_last_seen: ColumnElement[datetime | None] | None = None
    # ``Event.id`` is an InstrumentedAttribute, the subquery below a plain
    # ColumnElement; both belong in this slot, so the annotation is the wider one.
    metric_event_id: ColumnElement[uuid.UUID] = Event.id.expression
    if branch_id != main_branch_id and (silent_since_days is not None or order_by == "volume"):
        twin_last_seen, metric_event_id = _twin_reads_for_branch_rows(
            project_id=project_id, main_branch_id=main_branch_id
        )
    # Skip the selectin load for Event.event_type — the list response schema
    # ships only event_type_id, and the client already has EventTypes cached.
    query = (
        select(Event)
        .where(Event.project_id == project_id, Event.branch_id == branch_id)
        .options(noload(Event.event_type))
    )
    count_query = select(func.count(Event.id)).where(
        Event.project_id == project_id, Event.branch_id == branch_id
    )

    if event_type_id:
        query = query.where(Event.event_type_id == event_type_id)
        count_query = count_query.where(Event.event_type_id == event_type_id)
    if search:
        # Local list search is a plain substring filter across the visible
        # text columns (name, description, and the legacy "old" source_name).
        # The semantic/hybrid search lives in the global command palette only;
        # wiring it in here broke exact-match filtering whenever the search
        # index had not been (re)built for the project/branch yet.
        search_clause = or_(
            Event.name.ilike(f"%{search}%"),
            Event.description.ilike(f"%{search}%"),
            Event.source_name.ilike(f"%{search}%"),
        )
        query = query.where(search_clause)
        count_query = count_query.where(search_clause)
    if status:
        query = query.where(Event.status.in_(status))
        count_query = count_query.where(Event.status.in_(status))
    else:
        # Archiving an event is the user asking for it to be out of the way, so
        # an unfiltered listing must not carry it. Only the web app used to
        # honour that, and by accident: it sends an explicit six-status filter,
        # which happens to omit `archived`. Every other consumer of this
        # endpoint — the CLI, the MCP server's `list_events` tool, any direct
        # API call — still got archived events back, so "archived" meant
        # "hidden in one client" rather than a property of the plan (tripl-mhhi).
        # Asking for them explicitly (`?status=archived`) still works.
        query = query.where(Event.status != EventStatus.archived)
        count_query = count_query.where(Event.status != EventStatus.archived)
    if tag:
        # Case-folded on BOTH sides, because the write side was only normalised
        # from this batch forward: ``EventCreate``/``EventUpdate`` lower-case
        # what they store now, but every ``event_tags.name`` an MCP tool or a
        # raw API client wrote before that keeps the spelling it was handed. A
        # raw equality therefore left ``Checkout`` — a row this very list
        # displays — unreachable from the ``?tag=checkout`` the tag facet now
        # offers (tripl-0zpq.126). ``func.lower`` rather than ``ilike``: this is
        # an equality and a tag is free text, so ``_`` and ``%`` must stay
        # literal. It gives up ``ix_event_tag_name``; that is the price of
        # reaching the old rows without rewriting them in a migration.
        tag_filter = (
            select(EventTag.event_id)
            .where(func.lower(EventTag.name) == tag.strip().lower())
            .correlate(None)
        )
        query = query.where(Event.id.in_(tag_filter))
        count_query = count_query.where(Event.id.in_(tag_filter))
    if field_value:
        fv_filter = (
            select(EventFieldValue.event_id)
            .where(EventFieldValue.value.ilike(f"%{field_value}%"))
            .correlate(None)
        )
        query = query.where(Event.id.in_(fv_filter))
        count_query = count_query.where(Event.id.in_(fv_filter))
    if meta_value:
        mv_filter = (
            select(EventMetaValue.event_id)
            .where(EventMetaValue.value.ilike(f"%{meta_value}%"))
            .correlate(None)
        )
        query = query.where(Event.id.in_(mv_filter))
        count_query = count_query.where(Event.id.in_(mv_filter))
    if reviewed is not None:
        # Independent of `status`: this narrows by the review FLAG, so
        # ?status=in_review&reviewed=false answers "what is still unreviewed in
        # the queue" — the question the review tab could not ask (tripl-invv).
        query = query.where(Event.reviewed.is_(reviewed))
        count_query = count_query.where(Event.reviewed.is_(reviewed))
    if has_open_questions is not None:
        # Resolved to a set of ids rather than a join: the discussion hangs on
        # the event's MAIN twin, and pairing a branch row with it is the natural
        # key (event type name + scan identity), not something the events table
        # can express in a WHERE clause. The set is bounded by how many
        # questions are actually unanswered, not by the catalog.
        open_ids = await events_with_open_questions(
            session,
            project_id=project_id,
            branch_id=branch_id,
            main_branch_id=main_branch_id,
        )
        questions_clause = (
            Event.id.in_(open_ids) if has_open_questions else Event.id.not_in(open_ids)
        )
        query = query.where(questions_clause)
        count_query = count_query.where(questions_clause)
    if silent_since_days is not None and silent_since_days >= 0:
        cutoff = datetime.now(UTC) - timedelta(days=silent_since_days)
        silent_clause = or_(Event.last_seen_at.is_(None), Event.last_seen_at < cutoff)
        if twin_last_seen is not None:
            # Silent on the row AND on its twin — which is "max(own, twin) is
            # older than the cutoff", spelled as a conjunction because SQLite
            # has no ``GREATEST``. The copy's own stamp still counts: it is the
            # twin's value frozen at deep-copy time, so it can only be older,
            # never newer, and dropping it would change nothing but say less.
            silent_clause = and_(
                silent_clause, or_(twin_last_seen.is_(None), twin_last_seen < cutoff)
            )
        query = query.where(silent_clause)
        count_query = count_query.where(silent_clause)

    total = (await session.execute(count_query)).scalar() or 0
    if order_by == "volume":
        # Busiest-first: rank the review queue by each event's total ingested
        # volume over the last 24h (summed EventMetric.count) so high-traffic
        # events surface before quiet ones. Events with no metrics in the window
        # sort last (COALESCE→0 under DESC == NULLS LAST for non-negative counts),
        # with id.asc() as a stable tiebreak.
        #
        # Summed over ``metric_event_id``, which is the row's own id on main and
        # its main twin's on a branch: scans write metrics only for main ids, so
        # keying this on the branch id gave every copy volume 0 and quietly
        # degraded the sort to id order (tripl-0zpq.124). Correlated per row
        # rather than a grouped subquery joined on the id, because that id is
        # itself a per-row expression and cannot be a join key — which also
        # confines the aggregate to this project's events, where the grouped
        # form summed every project's metrics in the window and threw the rest
        # away on the join.
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        volume = (
            select(func.coalesce(func.sum(EventMetric.count), 0))
            .where(EventMetric.event_id == metric_event_id, EventMetric.bucket >= cutoff)
            .correlate(Event)
            .scalar_subquery()
        )
        ordered_query = query.order_by(volume.desc(), Event.id.asc())
    else:
        ordered_query = query.order_by(
            Event.order.asc(),
            Event.created_at.desc(),
            Event.id.asc(),
        )
    if order_by == "health":
        # Least healthy first: scored over the whole filtered set, then paged.
        events = await order_page_by_health(session, project_id, query, offset, limit)
    else:
        result = await session.execute(ordered_query.offset(offset).limit(limit))
        events = list(result.scalars().all())

    # Project SchemaDrift counts (per event_type) onto each event so the API
    # ships drift signal alongside the catalog row without an extra round-trip.
    event_type_ids = list({event.event_type_id for event in events})
    drift_counts = await get_drift_counts_by_event_type(session, project_id, event_type_ids)
    for event in events:
        event.drift_count = drift_counts.get(event.event_type_id, 0)  # type: ignore[attr-defined]

    # Project alert-rule coverage onto each row so the catalog's Monitor column
    # reflects whether an event is watched (has a rule) — the same identity-based
    # coverage the live pipeline uses — not merely whether it is firing.
    coverage_rules = await _load_event_coverage_rules(session, project_id)
    for event in events:
        event.monitored = any(  # type: ignore[attr-defined]
            rule_covers_event(
                rule,
                event_id=event.id,
                event_type_id=event.event_type_id,
            )
            for rule in coverage_rules
        )

    # Unanswered questions per row, read through to the main twin the way every
    # other borrowed signal on a branch copy is. Shipped with the catalog row so
    # the filter has something visible to agree with — a filter whose result the
    # list cannot explain reads as a bug.
    question_counts = await open_question_counts(session, project_id=project_id, events=events)
    for event in events:
        event.open_question_count = question_counts.get(event.id, 0)  # type: ignore[attr-defined]

    await attach_event_field_variable_values(session, events)
    await attach_main_last_seen(session, project_id=project_id, events=events)
    await attach_list_warnings(session, project_id=project_id, events=events)
    return events, total


async def _load_event_coverage_rules(
    session: AsyncSession, project_id: uuid.UUID
) -> list[AlertRule]:
    """Enabled, event-scoped alert rules under enabled destinations.

    Only these rules can *cover* an event, so the catalog computes the
    ``monitored`` flag by testing each event against just this set (their
    identity filters) rather than the full rule roster. Filters load eagerly via
    ``AlertRule.filters`` (``lazy="selectin"``), so this is a bounded, two-query
    load regardless of page size.
    """
    result = await session.execute(
        select(AlertRule)
        .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
        .where(
            AlertDestination.project_id == project_id,
            AlertDestination.enabled.is_(True),
            AlertRule.enabled.is_(True),
            AlertRule.include_events.is_(True),
        )
    )
    return list(result.scalars().unique().all())


async def _get_next_event_order(
    session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID
) -> int:
    max_order = await session.scalar(
        select(func.max(Event.order)).where(
            Event.project_id == project_id, Event.branch_id == branch_id
        )
    )
    return int(max_order or 0) + 1 if max_order is not None else 0


async def list_tags(
    session: AsyncSession, slug: str, branch_id: uuid.UUID | None = None
) -> list[str]:
    """The tag facet: one entry per distinct label on this branch.

    Lower-cased for the same reason ``list_events``' ``?tag=`` filter case-folds
    (tripl-0zpq.126): a row written before tag normalisation keeps the spelling
    it was handed, so ``Checkout`` and ``checkout`` stood here as two entries
    for one label and picking either found only half the events. What this
    returns is exactly what the filter takes.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    lowered = func.lower(EventTag.name)
    result = await session.execute(
        select(lowered)
        .join(Event, EventTag.event_id == Event.id)
        .where(Event.project_id == project_id, Event.branch_id == branch_id)
        .distinct()
        .order_by(lowered)
    )
    return list(result.scalars().all())


async def get_event(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
    *,
    strict_branch: bool = True,
) -> Event:
    """One event by id.

    ``strict_branch`` is what every WRITE path needs: an id from another branch
    is a 404, so a PATCH aimed at main can never land on a branch copy or the
    other way round. The two read routes pass ``False``: an event id is a link
    people paste, and a link into a branch event without its ``?branch=`` (or
    into main's row with someone else's branch selected) dead-ended on a 404
    that told the reader nothing. The row is returned with its own
    ``branch_id`` so the client can say where it lives and switch
    (tripl-kjhi.7).
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(Event).where(
            Event.id == event_id,
            Event.project_id == project_id,
            Event.branch_id == branch_id,
        )
    )
    event = result.scalar_one_or_none()
    if event is None and not strict_branch:
        event = (
            await session.execute(
                select(Event).where(Event.id == event_id, Event.project_id == project_id)
            )
        ).scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await attach_event_field_variable_values(session, [event])
    await attach_main_last_seen(session, project_id=project_id, events=[event])
    await _attach_first_seen(session, project_id=project_id, event=event)
    await attach_event_findings(session, project_id=project_id, event=event)
    return event


async def _attach_first_seen(session: AsyncSession, *, project_id: uuid.UUID, event: Event) -> None:
    """``first_seen_at``: the oldest bucket that counted this event (tripl-kjhi.10).

    Metrics are keyed on the main row, so a branch copy reads its twin's — the
    same twin ``last_seen_at`` comes from. The worker stamps the column since
    #258 (and the migration backfilled it); the aggregate over the event's own
    metric rows remains the fallback for a row it has not stamped yet.

    Set as a COMMITTED value, like ``attach_main_last_seen``: ``first_seen_at``
    is a real column now, and a plain assignment would ride the next flush of a
    write path that loads its row through here into the database.
    """
    row = await metrics_row_for(session, project_id=project_id, event=event)
    first_seen = row.first_seen_at
    if first_seen is None:
        first_seen = await session.scalar(
            select(func.min(EventMetric.bucket)).where(
                EventMetric.event_id == row.id, EventMetric.count > 0
            )
        )
    if first_seen != event.first_seen_at:
        set_committed_value(event, "first_seen_at", first_seen)
    # The same twin, named: a branch page's "View main plan" opens this event on
    # main instead of main's event list (EVT-42). None on main and for an event
    # created on the branch that main has no counterpart of.
    event.main_event_id = row.id if row.id != event.id else None  # type: ignore[attr-defined]


async def _resolve_event_name_format(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
) -> str | None:
    """The scan rule governing names for this event type, if any.

    Which configs are in scope is ``scan_config_lookup.load_governing_scan_configs``
    — shared with the schema-drift guard so the two cannot disagree about
    reachability (tripl-3mmh) — and the tie-break is ``governing_name_format``
    beside it, shared with the event-type listing so the form and the create
    path cannot disagree either (tripl-kjhi.1).
    """
    rows = await load_governing_scan_configs(
        session, project_id=project_id, event_type_id=event_type_id
    )
    return governing_name_format(rows)


async def _generate_scan_template_name(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
    field_values: list[EventFieldValueIn],
) -> str | None:
    """Name generated from the governing scan rule, or None when unruled.

    Manual events must carry the same identity the scan will derive from row
    values, or they never merge with their scan-generated counterparts —
    that's why the template, not the user, decides the name. 422 when the
    template references fields the payload doesn't fill.
    """
    name_format = await _resolve_event_name_format(session, project_id, event_type_id)
    if not name_format:
        return None
    field_names = {
        fd.id: fd.name
        for fd in (
            await session.execute(
                select(FieldDefinition).where(FieldDefinition.event_type_id == event_type_id)
            )
        ).scalars()
    }
    return apply_scan_name_format(
        name_format=name_format, field_names=field_names, field_values=field_values
    )


def apply_scan_name_format(
    *,
    name_format: str,
    field_names: Mapping[uuid.UUID, str],
    field_values: Sequence[EventFieldValueIn],
) -> str:
    """The name a scan rule gives these field values, or 422 naming what is missing.

    Separated from the lookups above so a batch can resolve the format and the
    field names ONCE PER EVENT TYPE and then name every item without touching
    the database. Naming fifty events item by item cost two queries each before
    this — the governing-config select and the field-definition select — on top
    of one identity probe per item.
    """
    values_by_field = {
        field_names[fv.field_definition_id]: fv.value
        for fv in field_values
        if fv.field_definition_id in field_names and fv.value
    }
    resolved = resolve_dotted_keys(name_format, values_by_field)
    generated, missing = apply_name_format(name_format, resolved)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Event name is generated by the scan rule '{name_format}'; "
                f"fill field values for: {', '.join(sorted(set(missing)))}"
            ),
        )
    return generated


async def _identities_already_held(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    wanted: Sequence[tuple[uuid.UUID, str]],
) -> dict[tuple[uuid.UUID, str], Event]:
    """Which of these (event type, identity) pairs an event already answers to.

    THE definition of "this identity is taken", used by the single create, the
    bulk create and the shadow accept alike. Two arms, because
    ``generate_events`` resolves identity in two steps
    (``core/analyzers/event_generator.py:230-243``): it matches on
    ``source_name``; and for a row that has none — authored through the API
    before any scan rule governed its type — it ADOPTS the current ``name`` as
    the identity on the next run. So a NULL-``source_name`` row whose name
    equals an incoming one is not free; it is a row that will claim that
    identity the first time a scan looks.

    Only the first arm is backed by the schema: ``uq_event_scan_identity`` on
    ``(event_type_id, source_name)`` refuses a second row for a held identity,
    and it is what closes the window between this SELECT and the INSERT that a
    concurrent request can slip through (tripl-8tdl). The second arm no unique
    key can express — a NULL ``source_name`` is distinct from every other in a
    unique key — so a rule-less row's future claim is honoured here and nowhere
    else.

    Scoped to one branch. ``ScanConfig`` has no branch column and scans write the
    main branch, but a working branch is a copy of the plan, so the same identity
    living once per branch is correct and only a collision WITHIN a branch is one.

    Filtering on the identity strings keeps the scan narrow; the pairing is
    finished in Python because the event type has to match too, and a row is
    only a collision for its own type. Every relationship is noload'ed: this is a
    probe, and only ``name`` and ``id`` reach the 409 body.
    """
    if not wanted:
        return {}
    identities = {identity for _, identity in wanted}
    event_type_ids = {event_type_id for event_type_id, _ in wanted}
    rows = (
        (
            await session.execute(
                select(Event)
                .where(
                    Event.project_id == project_id,
                    Event.branch_id == branch_id,
                    Event.event_type_id.in_(event_type_ids),
                    or_(
                        Event.source_name.in_(identities),
                        and_(Event.source_name.is_(None), Event.name.in_(identities)),
                    ),
                )
                .options(
                    noload(Event.event_type),
                    noload(Event.field_values),
                    noload(Event.meta_values),
                    noload(Event.tags),
                )
            )
        )
        .scalars()
        .all()
    )
    held: dict[tuple[uuid.UUID, str], Event] = {}
    for row in rows:
        held.setdefault((row.event_type_id, row.source_name or row.name), row)
    return held


async def _event_holding_scan_identity(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_id: uuid.UUID,
    identity: str,
) -> Event | None:
    """The event a scan would already match for ``identity``, if one exists.

    One item routed through the batch helper rather than a second copy of the
    predicate: the rule below has two arms and a branch scope, and two writings
    of it are exactly what ``scan_config_lookup`` exists to prevent for the
    reachability rule next door.
    """
    held = await _identities_already_held(
        session,
        project_id=project_id,
        branch_id=branch_id,
        wanted=[(event_type_id, identity)],
    )
    return held.get((event_type_id, identity))


async def identity_holders(
    session: AsyncSession,
    slug: str,
    *,
    event_type_id: uuid.UUID,
    names: Sequence[str],
    branch_id: uuid.UUID | None,
) -> EventIdentityHoldersResponse:
    """Which of ``names`` an event of this type already holds, exactly (EVT-37).

    The same predicate create uses (``_identities_already_held``), so an
    authoring form can say "already in the catalog" for exactly the names an
    event holds — in one query, where the bulk form used to send one substring
    search per pasted name. Create refuses such a name (409) only on an event
    type governed by a scan naming rule; elsewhere it is a warning.
    """
    project_id = await resolve_project_id(session, slug)
    resolved_branch_id = await resolve_branch_id(session, project_id, branch_id)
    wanted = list(dict.fromkeys(name for name in names if name))
    held = await _identities_already_held(
        session,
        project_id=project_id,
        branch_id=resolved_branch_id,
        wanted=[(event_type_id, name) for name in wanted],
    )
    items = [
        EventIdentityHolder(
            identity=name,
            event_id=event.id,
            name=event.name,
            source_name=event.source_name,
        )
        for name in wanted
        if (event := held.get((event_type_id, name))) is not None
    ]
    return EventIdentityHoldersResponse(items=items)


def _scan_identity_conflict_detail(*, identity: str, existing: Event) -> str:
    """The 409 body for an authored event that would collide with an existing one.

    Names the identity, says which event holds it, and gives the two ways out.
    Deliberately explains the CONSEQUENCE rather than the constraint: the reason
    a second row is refused is that only one of the two could ever receive scan
    updates, and "duplicate key" says nothing about that. ``uq_event_scan_identity``
    now refuses the row too (tripl-8tdl), but the probe before the INSERT keeps
    two jobs the constraint cannot do: it lets this body NAME the holder, and it
    honours the NULL-``source_name`` arm of ``_identities_already_held``, which
    no unique key can express. The constraint's job is the concurrent-request
    race the probe cannot see; a violation is re-probed and answered with this
    same body, so the two doors read alike.
    """
    return (
        f"An event already answers to the scan identity '{identity}' in this event type: "
        f"'{existing.name}' ({existing.id}). A scan matches one event per identity, so a "
        "second one would never receive volumes, last-seen times or observed values. "
        "Open the existing event instead, or change the field values the name is built from."
    )


async def _guard_scan_identity(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_id: uuid.UUID,
    identity: str,
) -> None:
    """Refuse an identity an existing event would compete with. See tripl-u2h9.1."""
    existing = await _event_holding_scan_identity(
        session,
        project_id=project_id,
        branch_id=branch_id,
        event_type_id=event_type_id,
        identity=identity,
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=_scan_identity_conflict_detail(identity=identity, existing=existing),
        )


async def _guard_batch_scan_identities(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    claimed_in_batch: Mapping[tuple[uuid.UUID, str], int],
    total: int,
) -> None:
    """The batch shape of ``_guard_scan_identity``: one probe, and the 409 names the item.

    ``claimed_in_batch`` maps each identity to the index of the item that first
    claimed it, in claim order, so the first held identity found is the lowest
    numbered item that collides — the same item a reader fixing the paste from
    the top would reach first.
    """
    held = await _identities_already_held(
        session, project_id=project_id, branch_id=branch_id, wanted=list(claimed_in_batch)
    )
    for key, index in claimed_in_batch.items():
        existing = held.get(key)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Event {index + 1} of {total}: "
                    + _scan_identity_conflict_detail(identity=key[1], existing=existing)
                ),
            )


async def _resolved_event_identity(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_id: uuid.UUID,
    field_values: list[EventFieldValueIn],
) -> str | None:
    """Generate this event's scan identity and refuse it if it is already taken.

    The one-at-a-time path. The bulk create does the same two steps in a batched
    shape — it resolves the format once per event type and probes every identity
    in one query — but through the SAME two helpers, ``apply_scan_name_format``
    and ``_identities_already_held``, so the doors cannot disagree about what an
    authored event is named or when its name is free. They did until
    tripl-u2h9.11, with bulk writing ``data.name`` verbatim and no identity at all.
    """
    generated_name = await _generate_scan_template_name(
        session,
        project_id=project_id,
        event_type_id=event_type_id,
        field_values=field_values,
    )
    if generated_name is None:
        return None
    await _guard_scan_identity(
        session,
        project_id=project_id,
        branch_id=branch_id,
        event_type_id=event_type_id,
        identity=generated_name,
    )
    return generated_name


async def _reserved_breakdown_columns_by_type(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_type_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, set[str]]:
    """Columns an event may not list as a breakdown, per event type.

    Today that is exactly the ``app_version_column`` of every scan that collects
    the type: app versions are collected on their own path, into the same table
    and the same row shape, so listing the column here made the collector write
    one breakdown key twice per bucket (tripl-0zpq.15). The scan-level list is
    already guarded the same way by ``check_scalar_columns_unreserved``; this is
    the event-level door.

    One query for any number of types — the bulk create asks about a whole paste
    at once. A scan with no bound event type is a grouped scan: it discovers its
    types from the data, so it collects every type in the project and its version
    column is reserved for all of them.

    Deliberately NOT reserved: ``platform_column``. The collector adds it
    scan-wide and dedupes it, so an event listing it produces one key, and the
    demo project ships an event that lists it.
    """
    wanted = list(dict.fromkeys(event_type_ids))
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(ScanConfig.event_type_id, ScanConfig.app_version_column).where(
                ScanConfig.project_id == project_id,
                ScanConfig.app_version_column.is_not(None),
                or_(
                    ScanConfig.event_type_id.in_(wanted),
                    ScanConfig.event_type_id.is_(None),
                ),
            )
        )
    ).all()
    reserved: dict[uuid.UUID, set[str]] = {event_type_id: set() for event_type_id in wanted}
    for bound_event_type_id, column in rows:
        name = (column or "").strip()
        if not name:
            continue
        if bound_event_type_id is None:
            for columns in reserved.values():
                columns.add(name)
        elif bound_event_type_id in reserved:
            reserved[bound_event_type_id].add(name)
    return reserved


def _breakdown_column_conflict_detail(clashing: Sequence[str]) -> str:
    """The 422 body for an event breakdown column a scan has already claimed.

    Says "scan", not "scan config", and spells the plurals out rather than
    writing "(s)" — the same rule ``name_format_conflict_detail`` holds this
    side of the wire to, for the same reason: the web UI renders this string
    verbatim.
    """
    named = ", ".join(f"'{column}'" for column in clashing)
    one = len(clashing) == 1
    noun = "Metric breakdown column" if one else "Metric breakdown columns"
    subject = "is the app version column" if one else "are the app version columns"
    pronoun = "it" if one else "them"
    return (
        f"{noun} {named} {subject} of a scan that collects this event type. App "
        "versions are collected as their own series already, so listing the "
        f"column here would collect it twice. Remove {pronoun} — the version "
        "breakdown stays available without it."
    )


def _clashing_breakdown_columns(columns: Sequence[str] | None, reserved: set[str]) -> list[str]:
    """The listed columns that are reserved, in the order the caller listed them."""
    if not columns or not reserved:
        return []
    return [column for column in dict.fromkeys(columns) if column in reserved]


async def _guard_event_breakdown_columns(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
    columns: Sequence[str] | None,
) -> None:
    """Refuse a breakdown column the collector would have to skip. See tripl-0zpq.15."""
    if not columns:
        return
    reserved = await _reserved_breakdown_columns_by_type(
        session, project_id=project_id, event_type_ids=[event_type_id]
    )
    clashing = _clashing_breakdown_columns(columns, reserved.get(event_type_id, set()))
    if clashing:
        raise HTTPException(
            status_code=422,
            detail=_breakdown_column_conflict_detail(clashing),
        )


async def create_event(
    session: AsyncSession,
    slug: str,
    data: EventCreate,
    branch_id: uuid.UUID | None = None,
    *,
    scan_identity: str | None = None,
    user_id: uuid.UUID | None = None,
) -> Event:
    """Author one event.

    ``scan_identity`` is for the caller that ALREADY knows the identity a scan
    derived — today only ``reconciliation_service.accept_shadow_event``, where the
    warehouse observed the name directly. It has THREE effects, and the third is
    a validation gate rather than a naming convenience, so a second caller has
    to want all three:

    * it skips name generation, which for that caller could only ever fail: a
      shadow candidate carries no field values, so a governing
      ``event_name_format`` reports every placeholder missing and the accept
      422s (tripl-u2h9.12);
    * it leaves ``data.name`` as the display name, which is what lets an
      operator rename an event while accepting it;
    * it turns OFF the required-field check, for the same reason as the first:
      the caller carries no field values at all and the accept form offers
      nowhere to type one, so a required field refused an event the warehouse
      had already seen (tripl-0zpq.130; the long version is on
      ``_check_and_normalize_field_values``). An unknown field definition is
      still refused and JSON values are still normalised.
    """
    is_main = branch_id is None
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    # First, before anything reads the type: a type from another branch has
    # field definitions of its own, so every later check would pass on it.
    await _guard_event_type_in_branch(
        session, project_id=project_id, branch_id=branch_id, event_type_id=data.event_type_id
    )
    field_values = await _validate_field_values(
        session,
        data.event_type_id,
        data.field_values,
        enforce_required=scan_identity is None,
    )
    await _guard_event_breakdown_columns(
        session,
        project_id=project_id,
        event_type_id=data.event_type_id,
        columns=data.metric_breakdown_columns,
    )
    if scan_identity is None:
        generated_name = await _resolved_event_identity(
            session,
            project_id=project_id,
            branch_id=branch_id,
            event_type_id=data.event_type_id,
            field_values=field_values,
        )
        display_name = generated_name or data.name
    else:
        await _guard_scan_identity(
            session,
            project_id=project_id,
            branch_id=branch_id,
            event_type_id=data.event_type_id,
            identity=scan_identity,
        )
        generated_name = scan_identity
        display_name = data.name

    event = Event(
        project_id=project_id,
        branch_id=branch_id,
        event_type_id=data.event_type_id,
        name=display_name,
        title=data.title.strip(),
        required_presence_threshold=data.required_presence_threshold,
        # Scan dedup keys on source_name: stamping the generated identity here
        # is what makes the manual event merge with its scanned counterpart.
        source_name=generated_name,
        description=data.description,
        order=await _get_next_event_order(session, project_id, branch_id),
        status=data.status,
        sunset_at=data.sunset_at,
        owner_id=data.owner_id,
        reviewed=data.reviewed,
        metric_breakdown_columns=data.metric_breakdown_columns,
    )
    # The probe above is a SELECT with no lock, so a second request can commit
    # this identity between it and the INSERT; ``uq_event_scan_identity`` is
    # what refuses the loser (tripl-8tdl). That refusal used to surface as a
    # bare 500. Here it is answered with the pre-check's own 409, naming the
    # winner: roll back, probe again, raise. The ROOT rollback is deliberate,
    # not the SAVEPOINT ``_ensure_variable`` uses — that shape exists so a scan
    # can carry on after a lost race, and this request cannot: nothing written
    # before the INSERT is worth keeping when the answer is 409, and
    # ``get_session`` would roll it all back on the way out anyway. (A savepoint
    # would also misbehave in the SQLite test database, where pysqlite's legacy
    # mode lets a SAVEPOINT opened before any DML commit on RELEASE — see
    # ``sqlite_transactions`` in the dialect docs — and this INSERT is normally
    # the request's first DML.)
    #
    # What the except arm may touch after a root rollback is exactly what
    # ``_commit_merged_plan`` spells out: the rollback expires every state in
    # the identity map, so reading an attribute of an ORM row loaded before
    # the try would be implicit IO — ``MissingGreenlet`` — and a 500 again. It
    # reads plain locals and the request schema only, and the re-probe is a
    # fresh query, whose rows arrive loaded.
    try:
        session.add(event)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        if generated_name is not None:
            await _guard_scan_identity(
                session,
                project_id=project_id,
                branch_id=branch_id,
                event_type_id=data.event_type_id,
                identity=generated_name,
            )
        # No identity, or nobody holds it: some other constraint refused the row,
        # and that is not a collision this function knows how to name.
        raise

    for fv in field_values:
        session.add(
            EventFieldValue(
                event_id=event.id,
                field_definition_id=fv.field_definition_id,
                value=fv.value,
                is_authored=True,
            )
        )
    for mv in await _normalize_meta_values(
        session, data.meta_values, project_id=project_id, branch_id=branch_id
    ):
        session.add(
            EventMetaValue(
                event_id=event.id,
                meta_field_definition_id=mv.meta_field_definition_id,
                value=mv.value,
            )
        )
    for tag_name in data.tags:
        session.add(EventTag(event_id=event.id, name=tag_name))
    # The history's first row, so "who created this and when" is answered by
    # the same list as every later edit (tripl-kjhi.9).
    session.add(
        create_event_change(
            event_id=event.id,
            user_id=user_id,
            field="created",
            old_value=None,
            new_value=event.name,
        )
    )
    await _subscribe_authors_and_owners(
        session, event.project_id, [(event, data.owner_id)], author_id=user_id
    )

    # Rebuild the search index inside the SAME transaction as the write, then
    # commit once. A single commit keeps primary data and the search index
    # consistent: if the reindex raises, the whole request rolls back together.
    await session.flush()
    _, ai_config = await _reindex_branch_documents(
        session, project_id=project_id, branch_id=branch_id, slug=slug
    )
    await session.commit()
    # Fire the async embedding refresh only after the commit succeeds, so a
    # rolled-back transaction never enqueues a stale refresh.
    await _queue_embedding_refresh(project_id, branch_id, ai_config=ai_config)
    await session.refresh(event)
    await attach_event_field_variable_values(session, [event])
    await _attach_template_warnings(session, event)
    # Only when the RULE overrode the caller. On the ``scan_identity`` path the
    # provided name is kept as the display name, so saying it was ignored would
    # be false — the two names differing there is the feature, not a collision.
    if scan_identity is None and generated_name and data.name and data.name != generated_name:
        event.warnings = [  # type: ignore[attr-defined]
            *event.warnings,  # type: ignore[attr-defined]
            f"Event name was generated from the scan rule: '{generated_name}'"
            f" (provided name '{data.name}' was ignored)",
        ]
    if is_main:
        await cache.delete_prefix(cache.prefix_projects())
    return event


async def _subscribe_authors_and_owners(
    session: AsyncSession,
    project_id: uuid.UUID,
    items: Sequence[tuple[Event, uuid.UUID | None]],
    *,
    author_id: uuid.UUID | None,
) -> None:
    """Auto-subscribe whoever authored each event, and its owner, to it (#259).

    ``items`` pairs each created event with its ``owner_id``. Kept under the
    discussion's home id (the main twin of a branch copy, one batched lookup
    for all of them), the id the comment notifications are about. Best-effort
    in a savepoint of the event's own transaction: a failure here is logged
    and never fails the create.
    """
    if not items or (author_id is None and all(owner is None for _event, owner in items)):
        return

    async def subscribe_all() -> None:
        await session.flush()
        twins = await main_counterparts(
            session, project_id=project_id, events=[event for event, _owner in items]
        )
        for event, owner_id in items:
            home_id = twins.get(event.id, event).id
            for user_id, reason in ((author_id, "author"), (owner_id, "owner")):
                await subscription_service.subscribe(
                    session,
                    user_id=user_id,
                    project_id=project_id,
                    entity_type=subscription_service.EVENT,
                    entity_id=home_id,
                    reason=reason,
                )

    await notification_announce.best_effort(session, "subscribe event author", subscribe_all)


async def update_event(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    data: EventUpdate,
    branch_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> Event:
    is_main = branch_id is None
    event = await get_event(session, slug, event_id, branch_id)
    update_data = data.model_dump(exclude_unset=True)

    # Snapshot tracked fields before mutation for change history
    old_values = {f: getattr(event, f) for f in _TRACKED_FIELDS if f in update_data}

    if "metric_breakdown_columns" in update_data:
        # Checked before anything is mutated, and only for columns being ADDED.
        # The event form re-sends the whole list on every save and it used to
        # OFFER the version column, so checking the whole list would block an
        # unrelated edit until the user removed a column the product itself put
        # there. A stored one is inert either way — the collector skips it
        # (``_is_supported_configured_breakdown_column``) — so grandfathering
        # costs nothing and closing the door on new ones is the point
        # (tripl-0zpq.15).
        held_columns = set(event.metric_breakdown_columns or [])
        await _guard_event_breakdown_columns(
            session,
            project_id=event.project_id,
            event_type_id=event.event_type_id,
            columns=[
                column
                for column in update_data["metric_breakdown_columns"] or []
                if column not in held_columns
            ],
        )

    if "name" in update_data:
        event.name = update_data["name"]
    if "title" in update_data:
        event.title = (update_data["title"] or "").strip()
    if "required_presence_threshold" in update_data:
        event.required_presence_threshold = update_data["required_presence_threshold"]
    if "description" in update_data:
        event.description = update_data["description"]
    if "status" in update_data:
        event.status = update_data["status"]
    if "sunset_at" in update_data:
        event.sunset_at = update_data["sunset_at"]
    if "superseded_by_event_id" in update_data:
        event.superseded_by_event_id = await _resolve_successor(
            session, event, update_data["superseded_by_event_id"]
        )
    if "metric_breakdown_columns" in update_data:
        event.metric_breakdown_columns = update_data["metric_breakdown_columns"]
    if "owner_id" in update_data:
        event.owner_id = update_data["owner_id"]
    if "reviewed" in update_data:
        event.reviewed = update_data["reviewed"]

    tracked_new = {f: update_data[f] for f in _TRACKED_FIELDS if f in update_data}
    _record_changes(
        session, event=event, old_values=old_values, new_values=tracked_new, user_id=user_id
    )

    # Replace child rows via a single DELETE+INSERT-batch per relation, instead
    # of `await session.delete(row)` per existing child (was ~N round-trips).
    if data.tags is not None:
        _record_list_change(
            session,
            event=event,
            field="tags",
            old=[tag.name for tag in event.tags],
            new=list(data.tags),
            user_id=user_id,
        )
        await session.execute(delete(EventTag).where(EventTag.event_id == event.id))
        await session.flush()
        if data.tags:
            session.add_all([EventTag(event_id=event.id, name=name) for name in data.tags])

    if data.field_values is not None:
        field_values = await _validate_field_values(session, event.event_type_id, data.field_values)
        # The form always sends every field back, edited or not. A value whose
        # text did not change keeps the ``is_authored`` it had: re-inserting it
        # as authored turned a scan-observed value into a "curated" one on every
        # unrelated save, which the search ranking reads as identity and the
        # branch diff showed as a change no one made (tripl-kjhi.4).
        authored_before = {
            row.field_definition_id: (row.value, row.is_authored)
            for row in (
                await session.execute(
                    select(EventFieldValue).where(EventFieldValue.event_id == event.id)
                )
            ).scalars()
        }
        _record_keyed_changes(
            session,
            event=event,
            prefix="field",
            names=await _definition_names(
                session,
                FieldDefinition,
                set(authored_before) | {fv.field_definition_id for fv in field_values},
            ),
            old={key: value for key, (value, _authored) in authored_before.items()},
            new={fv.field_definition_id: fv.value for fv in field_values},
            user_id=user_id,
        )
        # VariableValue rows are scan-observed contexts keyed by field_definition_id,
        # not by EventFieldValue rows — manual edits must not wipe them.
        await session.execute(delete(EventFieldValue).where(EventFieldValue.event_id == event.id))
        await session.flush()
        if field_values:
            session.add_all(
                [
                    EventFieldValue(
                        event_id=event.id,
                        field_definition_id=field_value.field_definition_id,
                        value=field_value.value,
                        is_authored=_authored_after_edit(
                            authored_before.get(field_value.field_definition_id),
                            field_value.value,
                        ),
                    )
                    for field_value in field_values
                ]
            )

    if data.meta_values is not None:
        meta_values = await _normalize_meta_values(
            session,
            data.meta_values,
            project_id=event.project_id,
            branch_id=event.branch_id,
        )
        meta_before = _meta_by_definition(
            (mv.meta_field_definition_id, mv.value) for mv in event.meta_values
        )
        meta_after = _meta_by_definition(
            (mv.meta_field_definition_id, mv.value) for mv in meta_values
        )
        _record_keyed_changes(
            session,
            event=event,
            prefix="meta",
            names=await _definition_names(
                session,
                MetaFieldDefinition,
                set(meta_before) | set(meta_after),
            ),
            old=meta_before,
            new=meta_after,
            user_id=user_id,
        )
        await session.execute(delete(EventMetaValue).where(EventMetaValue.event_id == event.id))
        await session.flush()
        if meta_values:
            session.add_all(
                [
                    EventMetaValue(
                        event_id=event.id,
                        meta_field_definition_id=meta_value.meta_field_definition_id,
                        value=meta_value.value,
                    )
                    for meta_value in meta_values
                ]
            )

    event_project_id = event.project_id
    event_branch_id = event.branch_id
    await session.flush()
    _, ai_config = await _reindex_branch_documents(
        session,
        project_id=event_project_id,
        branch_id=event_branch_id,
        slug=slug,
    )
    await session.commit()
    await _queue_embedding_refresh(event_project_id, event_branch_id, ai_config=ai_config)
    await session.refresh(event)
    await attach_event_field_variable_values(session, [event])
    await _attach_template_warnings(session, event)
    if is_main:
        await cache.delete_prefix(cache.prefix_projects())
    return event


async def delete_event(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> str:
    """Delete one event and hand its name back for the audit record.

    Returning the name follows the precedent in ``variable_service`` and matters
    more here: ``event_changes.event_id`` is ``ondelete="CASCADE"``, so the
    event's own history dies with the row and the audit entry is the only trace
    the delete ever happened (tripl-wkwv.10).
    """
    is_main = branch_id is None
    event = await get_event(session, slug, event_id, branch_id)
    project_id = event.project_id
    resolved_branch_id = event.branch_id
    # Snapshotted for the same reason as the two ids above: the flush below
    # evicts this instance from the identity map and marks it deleted, so it no
    # longer stands for a live row and must not be read afterwards. Not because
    # the read would raise — both session factories are ``expire_on_commit=False``
    # (database.py, tests/conftest.py), so the attribute would in fact still
    # answer; the rule is about not reading a deleted instance.
    event_name = event.name
    # Before the delete: several rows point at this event by string or JSON id,
    # or through a SET NULL foreign key, and would otherwise outlive it. The
    # anomalies are the sharp one — a NULL event_id satisfies every event filter,
    # so deleting an event used to UN-suppress its alerts (tripl-xjuv).
    await drop_dangling_event_references(session, project_id=project_id, event_ids=[event.id])
    # A branch row's own discussion is shown through its main twin too; hand it
    # over rather than let the cascade take it (tripl-0zpq.289).
    if not is_main:
        await rescue_branch_event_threads(session, project_id=project_id, event_ids=[event.id])
    await session.delete(event)
    await session.flush()
    _, ai_config = await _reindex_branch_documents(
        session,
        project_id=project_id,
        branch_id=resolved_branch_id,
        slug=slug,
    )
    await session.commit()
    await _queue_embedding_refresh(project_id, resolved_branch_id, ai_config=ai_config)
    if is_main:
        await cache.delete_prefix(cache.prefix_projects())
    return event_name


async def bulk_delete_events(
    session: AsyncSession,
    slug: str,
    data: EventBulkDelete,
    branch_id: uuid.UUID | None = None,
) -> list[tuple[uuid.UUID, str]]:
    """Delete the listed events and hand back ``(id, name)`` for each.

    The names are what the audit row keeps: once this returns the ids resolve to
    nothing, so an id-only record of a bulk delete names nothing a reader can
    recognise (tripl-wkwv.10).
    """
    is_main = branch_id is None
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    # Validate all ids exist + belong to this project+branch in a single query,
    # which also collects the names the caller audits — same one round trip the
    # COUNT this replaced cost. The comparison stays against
    # ``len(data.event_ids)`` and NOT the deduplicated set: a request repeating
    # an id matches fewer rows than it listed and 404s, exactly as before.
    rows = (
        await session.execute(
            select(Event.id, Event.name).where(
                Event.project_id == project_id,
                Event.branch_id == branch_id,
                Event.id.in_(data.event_ids),
            )
        )
    ).all()
    if len(rows) != len(data.event_ids):
        raise HTTPException(status_code=404, detail="One or more events were not found")
    deleted = [(event_id, name) for event_id, name in rows]

    # Same cleanup as the single delete, and it matters MORE here: this is a
    # Core DELETE, so no ORM cascade runs at all and everything not covered by a
    # database-level FK survives untouched.
    await drop_dangling_event_references(
        session, project_id=project_id, event_ids=list(data.event_ids)
    )
    if not is_main:
        await rescue_branch_event_threads(
            session, project_id=project_id, event_ids=list(data.event_ids)
        )
    # Single DELETE with IN-list; child rows go via FK ondelete=CASCADE in the DB.
    await session.execute(
        delete(Event).where(
            Event.project_id == project_id,
            Event.branch_id == branch_id,
            Event.id.in_(data.event_ids),
        )
    )
    await session.flush()
    _, ai_config = await _reindex_branch_documents(
        session, project_id=project_id, branch_id=branch_id, slug=slug
    )
    await session.commit()
    await _queue_embedding_refresh(project_id, branch_id, ai_config=ai_config)
    if is_main:
        await cache.delete_prefix(cache.prefix_projects())
    return deleted


async def bulk_update_events(
    session: AsyncSession,
    slug: str,
    data: EventBulkUpdate,
    branch_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    is_main = branch_id is None
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    event_ids = set(data.event_ids)

    present = await session.scalar(
        select(func.count(Event.id)).where(
            Event.project_id == project_id,
            Event.branch_id == branch_id,
            Event.id.in_(event_ids),
        )
    )
    if (present or 0) != len(event_ids):
        raise HTTPException(status_code=404, detail="One or more events were not found")

    # ``exclude_unset``, not ``exclude_none``: an explicit ``sunset_at: null`` or
    # ``owner_id: null`` is a request to CLEAR that field across the selection,
    # and dropping it answered 204 having changed nothing (tripl-0zpq.276).
    # Fields the client never sent stay out, which is what leaves them alone.
    # ``EventBulkUpdate`` refuses a null for the two NOT NULL columns up front,
    # so nothing reaching ``values()`` here can violate one.
    update_values = data.model_dump(exclude={"event_ids"}, exclude_unset=True)

    # Record changes for each event
    if update_values:
        events_result = await session.execute(
            select(Event).where(
                Event.project_id == project_id,
                Event.branch_id == branch_id,
                Event.id.in_(event_ids),
            )
        )
        for event in events_result.scalars().all():
            old_values = {f: getattr(event, f) for f in _TRACKED_FIELDS if f in update_values}
            _record_changes(
                session,
                event=event,
                old_values=old_values,
                new_values={f: update_values[f] for f in _TRACKED_FIELDS if f in update_values},
                user_id=user_id,
            )

    await session.execute(
        sql_update(Event)
        .where(
            Event.project_id == project_id,
            Event.branch_id == branch_id,
            Event.id.in_(event_ids),
        )
        .values(**update_values)
    )
    await session.flush()
    _, ai_config = await _reindex_branch_documents(
        session, project_id=project_id, branch_id=branch_id, slug=slug
    )
    await session.commit()
    await _queue_embedding_refresh(project_id, branch_id, ai_config=ai_config)
    if is_main:
        await cache.delete_prefix(cache.prefix_projects())


async def move_event(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    data: EventMove,
    branch_id: uuid.UUID | None = None,
) -> Event:
    event = await get_event(session, slug, event_id, branch_id)

    query = select(Event).where(
        Event.project_id == event.project_id, Event.branch_id == event.branch_id
    )
    if data.visible_event_ids:
        query = query.where(Event.id.in_(data.visible_event_ids))

    result = await session.execute(
        query.order_by(Event.order.asc(), Event.created_at.desc(), Event.id.asc())
    )
    ordered_events = list(result.scalars().all())
    ordered_ids = [item.id for item in ordered_events]
    if event.id not in ordered_ids:
        raise HTTPException(status_code=400, detail="Event is not present in the visible ordering")

    current_index = ordered_ids.index(event.id)
    target_index = current_index - 1 if data.direction == "up" else current_index + 1
    if target_index < 0 or target_index >= len(ordered_events):
        return event

    target = ordered_events[target_index]
    event.order, target.order = target.order, event.order
    await session.commit()
    await session.refresh(event)
    await attach_event_field_variable_values(session, [event])
    return event


async def reorder_events(
    session: AsyncSession,
    slug: str,
    data: EventReorder,
    branch_id: uuid.UUID | None = None,
) -> list[Event]:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)

    result = await session.execute(
        select(Event).where(
            Event.project_id == project_id,
            Event.branch_id == branch_id,
            Event.id.in_(data.event_ids),
        )
    )
    events = list(result.scalars().all())
    if len(set(data.event_ids)) != len(data.event_ids):
        # Checked before the ownership count, which a duplicated id passes (both
        # sides collapse to the same set) on the way to an IndexError 500 in the
        # slot loop below — the same defect, and the same 400, as
        # ``metric_definition_service.reorder_metric_definitions``
        # (tripl-0zpq.239).
        raise HTTPException(status_code=400, detail="Duplicate event ids in the requested order")
    if len(events) != len(set(data.event_ids)):
        raise HTTPException(status_code=400, detail="Some events do not belong to this project")

    events_by_id = {event.id: event for event in events}
    sorted_orders = sorted(event.order for event in events)
    for new_index, event_id in enumerate(data.event_ids):
        events_by_id[event_id].order = sorted_orders[new_index]

    await session.commit()
    # One round-trip with selectin relations (event_type/field_values/meta_values/tags)
    # instead of N×refresh after commit.
    refreshed = await session.execute(select(Event).where(Event.id.in_(data.event_ids)))
    by_id = {event.id: event for event in refreshed.scalars().all()}
    ordered_events = [by_id[event_id] for event_id in data.event_ids]
    await attach_event_field_variable_values(session, ordered_events)
    return ordered_events


async def bulk_create_events(
    session: AsyncSession,
    slug: str,
    events_data: list[EventCreate],
    branch_id: uuid.UUID | None = None,
    *,
    user_id: uuid.UUID | None = None,
) -> list[Event]:
    if not events_data:
        return []

    is_main = branch_id is None
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)

    # Batched per-event-type validation: ONE SELECT across all referenced event
    # types (IN-list), grouped by event_type_id in Python, then a per-event
    # check using the cached field definitions.
    unique_event_type_ids = {data.event_type_id for data in events_data}
    # One query for the paste, checked per item below so the refusal still names
    # which item is at fault. Same rule the single create applies first, for the
    # same reason: a type on another branch has field definitions of its own, so
    # every check after this one would pass on it (tripl-0zpq.123).
    event_type_ids_in_branch = await _event_type_ids_in_branch(
        session,
        project_id=project_id,
        branch_id=branch_id,
        event_type_ids=unique_event_type_ids,
    )
    # And one for the paste's meta fields, so the meta refusals reach the same
    # two places the event-type one does. Called per item from the children loop
    # instead, they fired outside the ``try`` that prefixes "Event N of M" AND
    # after ``session.add_all``/``flush`` had already put every row in the
    # database — so a client whose seventh of twenty items carried main's meta
    # field id got a bare "Meta field <uuid> is not on this project's branch",
    # naming no item, having paid for an insert of the whole batch to produce it
    # (tripl-0zpq.123). Loading here also collapses one SELECT per item into one
    # for the paste.
    meta_definitions = await _load_meta_field_definitions(
        session,
        project_id=project_id,
        branch_id=branch_id,
        meta_field_definition_ids={
            mv.meta_field_definition_id for data in events_data for mv in data.meta_values
        },
    )
    field_defs_by_type: dict[uuid.UUID, dict[uuid.UUID, FieldDefinition]] = {
        event_type_id: {} for event_type_id in unique_event_type_ids
    }
    result = await session.execute(
        select(FieldDefinition).where(FieldDefinition.event_type_id.in_(unique_event_type_ids))
    )
    for fd in result.scalars().all():
        field_defs_by_type.setdefault(fd.event_type_id, {})[fd.id] = fd

    # The same rules the single create applies, so a batch cannot author events
    # the one-at-a-time door would have refused: the field checks, the meta
    # value checks, the name the governing scan rule gives, and the identity
    # that name claims. Every failure names its item — a 422 reading "Required
    # field 'action' is missing" is unusable when thirty events were posted at
    # once — and every one of them lands before a single row is inserted.
    #
    # Shaped for a batch, not repeated per item: the format is resolved once per
    # event type (the lookup behind it is two queries), the naming itself is pure
    # over field definitions already loaded above, and the identities are probed
    # in a single query below. A paste of fifty would otherwise cost about 150
    # round trips before writing anything, and this endpoint takes an unbounded
    # list.
    name_formats: dict[uuid.UUID, str | None] = {
        event_type_id: await _resolve_event_name_format(session, project_id, event_type_id)
        for event_type_id in unique_event_type_ids
    }

    # One query for the whole paste, same shape as the identity probe below, and
    # only when the paste configures breakdowns at all.
    reserved_breakdown_columns = (
        await _reserved_breakdown_columns_by_type(
            session, project_id=project_id, event_type_ids=list(unique_event_type_ids)
        )
        if any(data.metric_breakdown_columns for data in events_data)
        else {}
    )

    normalized_values: list[list[EventFieldValueIn]] = []
    normalized_meta: list[list[EventMetaValueIn]] = []
    identities: list[str | None] = []
    claimed_in_batch: dict[tuple[uuid.UUID, str], int] = {}
    for index, data in enumerate(events_data):
        defs = field_defs_by_type[data.event_type_id]
        name_format = name_formats[data.event_type_id]
        try:
            # Raised inside the try on purpose: the arm below is what puts
            # "Event N of M" in front of every refusal this loop makes.
            if data.event_type_id not in event_type_ids_in_branch:
                raise HTTPException(
                    status_code=422,
                    detail=_event_type_out_of_branch_detail(data.event_type_id),
                )
            clashing_columns = _clashing_breakdown_columns(
                data.metric_breakdown_columns,
                reserved_breakdown_columns.get(data.event_type_id, set()),
            )
            if clashing_columns:
                raise HTTPException(
                    status_code=422,
                    detail=_breakdown_column_conflict_detail(clashing_columns),
                )
            values = _check_and_normalize_field_values(defs, data.field_values)
            meta_values = _normalize_meta_values_against(meta_definitions, data.meta_values)
            identity = (
                apply_scan_name_format(
                    name_format=name_format,
                    field_names={fd_id: fd.name for fd_id, fd in defs.items()},
                    field_values=values,
                )
                if name_format
                else None
            )
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=f"Event {index + 1} of {len(events_data)}: {exc.detail}",
            ) from exc
        if identity is not None:
            # The sibling is not in the database, so the batch query below cannot
            # see it; two items of one batch claiming one identity is exactly the
            # collision this endpoint would otherwise create in a single
            # transaction.
            key = (data.event_type_id, identity)
            first = claimed_in_batch.get(key)
            if first is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Events {first + 1} and {index + 1} of {len(events_data)} both "
                        f"generate the scan identity '{identity}' from the same event "
                        "type's naming rule, so only one of them could ever receive "
                        "scan updates."
                    ),
                )
            claimed_in_batch[key] = index
        normalized_values.append(values)
        normalized_meta.append(meta_values)
        identities.append(identity)

    # One probe for the whole batch, in place of one per item.
    await _guard_batch_scan_identities(
        session,
        project_id=project_id,
        branch_id=branch_id,
        claimed_in_batch=claimed_in_batch,
        total=len(events_data),
    )

    # One SELECT max(order) instead of N — we assign consecutive orders ourselves.
    base_order = await _get_next_event_order(session, project_id, branch_id)

    events: list[Event] = []
    for i, data in enumerate(events_data):
        events.append(
            Event(
                project_id=project_id,
                branch_id=branch_id,
                event_type_id=data.event_type_id,
                name=identities[i] or data.name,
                title=data.title.strip(),
                required_presence_threshold=data.required_presence_threshold,
                description=data.description,
                order=base_order + i,
                status=data.status,
                sunset_at=data.sunset_at,
                # Stamped for the same reason the single create stamps it: the
                # scan matches on source_name, so this is what makes an authored
                # event merge with its scanned counterpart instead of doubling it.
                source_name=identities[i],
                # Carried for the same reason every other column above is: the
                # batch door takes the SAME ``EventCreate`` the single door
                # takes, so a field it accepts and then drops makes one payload
                # mean two different things. These two were the drops — a paste
                # carrying an owner and ``reviewed: true`` was answered 201 with
                # the model defaults, unassigned and unreviewed (tripl-0zpq.127).
                owner_id=data.owner_id,
                reviewed=data.reviewed,
                metric_breakdown_columns=data.metric_breakdown_columns,
            )
        )
    # Same rollback and re-probe as ``create_event``, for the same race: the
    # probe above cannot see an identity another request commits after it, and
    # ``uq_event_scan_identity`` can. On a violation every identity is probed
    # again in one query and the first item now held is named, with the same
    # ``Event N of M`` prefix the rest of the batch's answers carry. The except
    # arm reads ``claimed_in_batch`` and the request list only — plain values —
    # for the reason given in ``create_event``.
    try:
        session.add_all(events)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await _guard_batch_scan_identities(
            session,
            project_id=project_id,
            branch_id=branch_id,
            claimed_in_batch=claimed_in_batch,
            total=len(events_data),
        )
        raise

    children: list[EventFieldValue | EventMetaValue | EventTag] = []
    for event, data, values, meta_values in zip(
        events, events_data, normalized_values, normalized_meta, strict=True
    ):
        for fv in values:
            children.append(
                EventFieldValue(
                    event_id=event.id,
                    field_definition_id=fv.field_definition_id,
                    value=fv.value,
                    is_authored=True,
                )
            )
        # Already normalised and already refused, up in the validation loop.
        for mv in meta_values:
            children.append(
                EventMetaValue(
                    event_id=event.id,
                    meta_field_definition_id=mv.meta_field_definition_id,
                    value=mv.value,
                )
            )
        for tag_name in data.tags:
            children.append(EventTag(event_id=event.id, name=tag_name))

    if children:
        session.add_all(children)

    # The same first history row ``create_event`` writes, one per event, now
    # that the ids exist (tripl-kjhi.9).
    session.add_all(
        [
            create_event_change(
                event_id=event.id,
                user_id=user_id,
                field="created",
                old_value=None,
                new_value=event.name,
            )
            for event in events
        ]
    )
    await _subscribe_authors_and_owners(
        session,
        project_id,
        [(event, data.owner_id) for event, data in zip(events, events_data, strict=True)],
        author_id=user_id,
    )
    await session.flush()
    _, ai_config = await _reindex_branch_documents(
        session, project_id=project_id, branch_id=branch_id, slug=slug
    )
    await session.commit()
    await _queue_embedding_refresh(project_id, branch_id, ai_config=ai_config)
    # One round-trip with selectin relations instead of N×refresh after commit.
    event_ids = [event.id for event in events]
    refreshed = await session.execute(select(Event).where(Event.id.in_(event_ids)))
    by_id = {event.id: event for event in refreshed.scalars().all()}
    ordered_events = [by_id[event_id] for event_id in event_ids]
    await attach_event_field_variable_values(session, ordered_events)
    if is_main:
        await cache.delete_prefix(cache.prefix_projects())
    return ordered_events


async def get_event_history(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> list[dict[str, object]]:
    # Validate event belongs to this project
    await get_event(session, slug, event_id, branch_id, strict_branch=False)

    result = await session.execute(
        select(EventChange, User.email)
        .outerjoin(User, EventChange.user_id == User.id)
        .where(EventChange.event_id == event_id)
        .order_by(EventChange.created_at.desc())
    )
    rows = result.all()
    history: list[dict[str, object]] = []
    for change, user_email in rows:
        history.append(
            {
                "id": change.id,
                "event_id": change.event_id,
                "user_id": change.user_id,
                "user_email": user_email,
                # Written by the scan (#258): read off ``source``, never off
                # ``user_id IS NULL`` — a status edit whose author was later
                # deleted (``user_id`` SET NULL) is still a person's.
                "author_label": (
                    SCAN_AUTHOR_LABEL if change.source == EVENT_CHANGE_SOURCE_SCAN else None
                ),
                "field": change.field,
                "old_value": change.old_value,
                "new_value": change.new_value,
                "created_at": change.created_at,
            }
        )
    return history


async def _resolve_successor(
    session: AsyncSession,
    event: Event,
    successor_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Validate the "replaced by" target before it is stored.

    A client sends a bare uuid, which is the one real surface here: unchecked,
    it would let an event point at another project's row and make that row's
    name readable through this project. So the successor has to live in the
    same project AND on the same branch — a pointer across branches would
    dangle the moment either side is deep-copied or merged — and an event
    cannot replace itself.
    """
    if successor_id is None:
        return None
    if successor_id == event.id:
        raise HTTPException(status_code=400, detail="An event cannot replace itself")
    successor = (
        await session.execute(
            select(Event).where(
                Event.id == successor_id,
                Event.project_id == event.project_id,
                Event.branch_id == event.branch_id,
            )
        )
    ).scalar_one_or_none()
    if successor is None:
        raise HTTPException(status_code=404, detail="Successor event not found")
    return successor.id
