"""Signal verdicts: set / clear, with the incident as source of truth (F01, #254).

A verdict says what a signal turned out to be — ``expected`` (with an optional
reason), ``tracking_bug``, ``false_positive`` or ``real_issue`` — plus a free-text
note, its author and when it was set. It extends the signal triage table
(``SignalTriage``) rather than adding a parallel one: a verdict is one more
per-bucket action, and a signal carries at most one of them.

* ``expected`` keeps what "mark as expected" always did: a chart annotation on
  the bucket and the one signal hidden. The reason only documents; it does not
  suppress later buckets.
* ``false_positive`` makes detection stricter on the signal's scope through the
  same ratchet an incident marked false positive uses
  (``alerting_service.tune_false_positive_scopes``).
* ``tracking_bug`` / ``real_issue`` record the call and change nothing else.

When the signal was routed to an incident, the incident is the source of truth:
the verdict writes through to it (false_positive -> false_positive, real_issue
and tracking_bug -> acknowledged, expected -> resolved, clearing -> reopened),
the false-positive tuning is the incident's (its delivered scopes), and the
signal's own row only refines how that status reads (``_signal_verdict_read``).
An incident moved in the inbox reflects onto its signals at read time.

Every change writes an ``EventChange`` (``field="signal_verdict"``) for an
event-scope signal, so it shows in the event's activity feed; the route writes
the audit row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import (
    MetricScopeType,
    SignalExpectedReason,
    SignalVerdict,
)
from tripl.models.event import Event
from tripl.models.event_change import create_event_change
from tripl.models.event_type import EventType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project
from tripl.models.signal_triage import SignalTriage
from tripl.schemas.event_metric import (
    EventMetricPoint,
    EventMetricsResponse,
    MetricSignalResponse,
    SignalIncidentBrief,
    SignalTriageState,
    SignalVerdictInfo,
    SignalVerdictRequest,
    SignalVerdictResponse,
)
from tripl.schemas.metric_series import MetricSeriesPoint, MetricSeriesResponse
from tripl.services import alerting_service, signal_triage_service
from tripl.services._signal_verdict_read import (
    INCIDENT_STATUS_VERDICT,
    VERDICT_ACTIONS,
    VERDICT_INCIDENT_ACTION,
    incident_brief,
    load_user_names,
    record_prevails,
    resolve_verdict,
    status_agrees,
)
from tripl.services._signal_verdict_rows import delete_verdict_rows
from tripl.services.project_lookup import get_project_by_slug
from tripl.services.signal_triage_service import (
    EXPECTED_ANNOTATION_COLOR,
    EXPECTED_ANNOTATION_LABEL,
    SignalKey,
    TriageIndex,
    TriageTarget,
)

# ``EventChange.field`` of a verdict entry in the event's activity feed. The
# values read ``<verdict>`` or ``expected:<reason>``, then `` — <note>`` when a
# note was given; a cleared verdict has a NULL ``new_value``.
EVENT_CHANGE_FIELD = "signal_verdict"
_EVENT_CHANGE_VALUE_MAX = 500


@dataclass(frozen=True)
class VerdictWrite:
    """What a verdict write did, for the route's audit row and response."""

    project: Project
    project_id: uuid.UUID
    key: SignalKey
    row_id: uuid.UUID | None
    # False for a repeat of the verdict already on record (nothing audited).
    changed: bool
    previous: str | None
    incident_id: uuid.UUID | None
    # The inbox action the verdict applied to ``incident_id`` (``acknowledge``,
    # ``resolve``, ``false_positive``), when it moved the incident; the route
    # files the matching ``alert_inbox.<action>`` audit row for it.
    incident_action: str | None = None


@dataclass(frozen=True)
class VerdictClear:
    project: Project
    project_id: uuid.UUID
    row_id: uuid.UUID | None
    previous: str | None
    # The incident a clear reopened, if the signal's verdict was its status.
    reopened_incident_id: uuid.UUID | None

    @property
    def changed(self) -> bool:
        return self.previous is not None


def _clean_note(note: str | None) -> str | None:
    return note.strip() if note and note.strip() else None


def _feed_value(
    verdict: str | None, reason: str | None = None, note: str | None = None
) -> str | None:
    if verdict is None:
        return None
    value = f"{verdict}:{reason}" if reason else verdict
    if note:
        value = f"{value} — {note}"
    if len(value) > _EVENT_CHANGE_VALUE_MAX:
        value = f"{value[: _EVENT_CHANGE_VALUE_MAX - 1]}…"
    return value


async def _verdict_rows(
    session: AsyncSession, project_id: uuid.UUID, key: SignalKey
) -> list[SignalTriage]:
    """Every verdict row on the signal, newest first (one is the rule)."""
    rows = (
        await session.execute(
            select(SignalTriage)
            .where(
                *signal_triage_service._scope_filter(project_id, *key[:3]),
                SignalTriage.action.in_(sorted(VERDICT_ACTIONS)),
                SignalTriage.bucket == key[3],
            )
            .order_by(SignalTriage.created_at.desc(), SignalTriage.id.desc())
        )
    ).scalars()
    return list(rows)


async def _scope_name(session: AsyncSession, scope_type: str, scope_ref: str) -> str | None:
    """A label for the detection override a false-positive verdict writes."""
    if scope_type == MetricScopeType.project_total.value:
        return "Project total"
    try:
        ref_id = uuid.UUID(scope_ref)
    except ValueError:
        return None
    column = {
        MetricScopeType.event.value: (Event.name, Event.id),
        MetricScopeType.event_type.value: (EventType.display_name, EventType.id),
        MetricScopeType.metric.value: (MetricDefinition.display_name, MetricDefinition.id),
    }.get(scope_type)
    if column is None:
        return None
    label, key = column
    name: str | None = await session.scalar(select(label).where(key == ref_id))
    return name


async def _record_event_change(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    user_id: uuid.UUID,
    old_value: str | None,
    new_value: str | None,
) -> None:
    """Put the verdict on the event's activity feed (event-scope signals only).

    Added to the session, not committed: it lands with the verdict itself.
    """
    if scope_type != MetricScopeType.event.value:
        return
    try:
        event_id = uuid.UUID(scope_ref)
    except ValueError:
        return
    exists = await session.scalar(
        select(Event.id).where(Event.id == event_id, Event.project_id == project_id).limit(1)
    )
    if exists is None:
        return
    session.add(
        create_event_change(
            event_id=event_id,
            user_id=user_id,
            field=EVENT_CHANGE_FIELD,
            old_value=old_value,
            new_value=new_value,
        )
    )


async def set_verdict(
    session: AsyncSession,
    slug: str,
    data: SignalVerdictRequest,
    *,
    user_id: uuid.UUID,
) -> VerdictWrite:
    """Set the signal's verdict, replacing any other one it had.

    404 when the project has no such signal. A repeat of the verdict already on
    record (same note, same reason) changes nothing and reports
    ``changed=False``; in particular it does not ratchet detection twice.
    """
    target, ref = await signal_triage_service.resolve_signal(session, slug, data)
    verdict = SignalVerdict(data.verdict)
    note = _clean_note(data.note)
    reason = (
        SignalExpectedReason(data.expected_reason).value
        if verdict == SignalVerdict.expected and data.expected_reason is not None
        else None
    )
    rows = await _verdict_rows(session, target.project_id, target.key)
    same = next((row for row in rows if str(row.action) == verdict.value), None)
    others = [row for row in rows if row is not same]
    previous = _feed_value(
        str(rows[0].action) if rows else None,
        rows[0].expected_reason if rows else None,
        rows[0].note if rows else None,
    )
    if ref is not None and not rows:
        derived = INCIDENT_STATUS_VERDICT.get(ref.status)
        previous = _feed_value(derived.value if derived else None, None, ref.note)

    incident_action, _incident_status = VERDICT_INCIDENT_ACTION[verdict]
    # Moves only when the incident's current status does not already agree with
    # the verdict: re-posting ``expected`` (say, to edit its note) on a
    # resolved incident, or ``real_issue`` on one fixed and resolved since,
    # must leave it where it is.
    incident_moves = ref is not None and not status_agrees(ref.status, verdict)
    row_changes = (
        same is None or bool(others) or same.note != note or same.expected_reason != reason
    )
    if not row_changes and not incident_moves:
        return VerdictWrite(
            project=target.project,
            project_id=target.project_id,
            key=target.key,
            row_id=same.id if same is not None else None,
            changed=False,
            previous=previous,
            incident_id=ref.correlation_group_id if ref is not None else None,
        )

    await delete_verdict_rows(session, target.project_id, others)

    if ref is not None:
        if incident_moves:
            await alerting_service.apply_signal_verdict_to_incident(
                session,
                project_id=target.project_id,
                correlation_group_id=ref.correlation_group_id,
                action=incident_action,
                note=note,
                user_id=user_id,
            )
        elif note is not None and note != ref.note:
            await alerting_service.apply_signal_verdict_to_incident(
                session,
                project_id=target.project_id,
                correlation_group_id=ref.correlation_group_id,
                action="note",
                note=note,
                user_id=user_id,
            )
    elif verdict == SignalVerdict.false_positive and same is None:
        # Not routed, so there is no incident to carry the tuning: ratchet the
        # signal's own scope, through the one helper the inbox uses too.
        await alerting_service.tune_false_positive_scopes(
            session,
            project_id=target.project_id,
            scopes=[
                alerting_service.FalsePositiveScope(
                    scan_config_id=target.scan_config_id,
                    scope_type=target.scope_type,
                    scope_ref=target.scope_ref,
                    scope_name=await _scope_name(session, target.scope_type, target.scope_ref),
                )
            ],
        )

    await _record_event_change(
        session,
        project_id=target.project_id,
        scope_type=target.scope_type,
        scope_ref=target.scope_ref,
        user_id=user_id,
        old_value=previous,
        new_value=_feed_value(verdict.value, reason, note),
    )

    row_id = await _upsert_row(
        session, target, same, verdict=verdict, reason=reason, note=note, user_id=user_id
    )
    await signal_triage_service._invalidate(target.project_slug)
    return VerdictWrite(
        project=target.project,
        project_id=target.project_id,
        key=target.key,
        row_id=row_id,
        changed=True,
        previous=previous,
        incident_id=ref.correlation_group_id if ref is not None else None,
        incident_action=incident_action if incident_moves else None,
    )


async def _upsert_row(
    session: AsyncSession,
    target: TriageTarget,
    same: SignalTriage | None,
    *,
    verdict: SignalVerdict,
    reason: str | None,
    note: str | None,
    user_id: uuid.UUID,
) -> uuid.UUID | None:
    """Write the verdict row and COMMIT everything pending with it."""
    if same is not None:
        row_id = same.id
        same.note = note
        same.expected_reason = reason
        same.created_by_user_id = user_id
        if same.annotation_id is not None:
            annotation = await session.get(ChartAnnotation, same.annotation_id)
            if annotation is not None:
                annotation.description = note
        await session.commit()
        return row_id

    annotation = None
    if verdict == SignalVerdict.expected:
        annotation = ChartAnnotation(
            project_id=target.project_id,
            scope_type=target.scope_type,
            scope_ref=target.scope_ref,
            bucket=target.bucket,
            label=EXPECTED_ANNOTATION_LABEL,
            description=note,
            color=EXPECTED_ANNOTATION_COLOR,
            created_by_user_id=user_id,
        )
    row = SignalTriage(
        project_id=target.project_id,
        scan_config_id=target.scan_config_id,
        scope_type=target.scope_type,
        scope_ref=target.scope_ref,
        action=verdict.value,
        bucket=target.bucket,
        note=note,
        expected_reason=reason,
        annotation_id=None,
        created_by_user_id=user_id,
    )
    if await signal_triage_service._commit_insert(session, target, row, annotation=annotation):
        return row.id
    # A concurrent click wrote the same verdict first; put this one's note and
    # reason on the winner rather than failing.
    raced = next(
        (
            candidate
            for candidate in await _verdict_rows(session, target.project_id, target.key)
            if str(candidate.action) == verdict.value
        ),
        None,
    )
    if raced is None:  # pragma: no cover - the row that blocked us is gone
        await session.commit()
        return None
    raced_id = raced.id
    raced.note = note
    raced.expected_reason = reason
    raced.created_by_user_id = user_id
    await session.commit()
    return raced_id


async def clear_verdict(
    session: AsyncSession,
    slug: str,
    *,
    scan_config_id: uuid.UUID | None,
    scope_type: MetricScopeType,
    scope_ref: str,
    bucket: datetime,
    user_id: uuid.UUID,
) -> VerdictClear:
    """Take the signal's verdict away. Idempotent.

    On a routed signal whose incident status IS a verdict (acknowledged,
    resolved, false positive), the incident is reopened: otherwise the verdict
    would keep reading off it. Detection tuning a false positive did is not
    undone, as with reopening in the inbox; the override is deleted from
    Detection settings.
    """
    project = await get_project_by_slug(session, slug)
    project_id = project.id
    scope = str(scope_type)
    signal_triage_service._validate_scope_shape(scope, scan_config_id)
    key = signal_triage_service.signal_key(scan_config_id, scope, scope_ref, bucket)
    rows = await _verdict_rows(session, project_id, key)
    refs = await alerting_service.incident_refs_for_signals(session, project_id, [key])
    ref = refs.get(key)
    reopen = ref is not None and ref.status in INCIDENT_STATUS_VERDICT
    if not rows and not reopen:
        return VerdictClear(
            project=project,
            project_id=project_id,
            row_id=None,
            previous=None,
            reopened_incident_id=None,
        )
    if rows:
        previous = _feed_value(str(rows[0].action), rows[0].expected_reason, rows[0].note)
    else:
        assert ref is not None
        derived = INCIDENT_STATUS_VERDICT[ref.status]
        previous = _feed_value(derived.value, None, ref.note)
    row_id = rows[0].id if rows else None
    await delete_verdict_rows(session, project_id, rows)
    if reopen and ref is not None:
        await alerting_service.apply_signal_verdict_to_incident(
            session,
            project_id=project_id,
            correlation_group_id=ref.correlation_group_id,
            action="reopen",
            note=None,
            user_id=user_id,
        )
    await _record_event_change(
        session,
        project_id=project_id,
        scope_type=scope,
        scope_ref=scope_ref,
        user_id=user_id,
        old_value=previous,
        new_value=None,
    )
    await session.commit()
    await signal_triage_service._invalidate(project.slug)
    return VerdictClear(
        project=project,
        project_id=project_id,
        row_id=row_id,
        previous=previous,
        reopened_incident_id=ref.correlation_group_id if reopen and ref is not None else None,
    )


# --- reads ------------------------------------------------------------------


async def _verdicts_for_scope(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    scan_config_id: uuid.UUID | None,
    scope_type: str,
    scope_ref: str,
    buckets: list[datetime],
) -> dict[datetime, SignalVerdictInfo]:
    """Each bucket's verdict on one scope, keyed by the bucket in UTC."""
    if not buckets:
        return {}
    keys = [
        signal_triage_service.signal_key(scan_config_id, scope_type, scope_ref, bucket)
        for bucket in buckets
    ]
    index = (
        await signal_triage_service.load_triage_indexes(
            session, [project_id], min_bucket=min(key[3] for key in keys)
        )
    ).get(project_id) or TriageIndex()
    refs = await alerting_service.incident_refs_for_signals(session, project_id, keys)
    if not index.verdicts and not refs:
        return {}
    names = await load_user_names(
        session,
        [record.author_id for record in index.verdicts.values()]
        + [ref.acted_by for ref in refs.values()],
    )
    out: dict[datetime, SignalVerdictInfo] = {}
    for key in keys:
        info = resolve_verdict(index.verdicts.get(key), refs.get(key), names)
        if info is not None:
            out[key[3]] = info
    return out


def _stamp_points[P: (EventMetricPoint, MetricSeriesPoint)](
    points: list[P], verdicts: dict[datetime, SignalVerdictInfo]
) -> list[P]:
    if not verdicts:
        return points
    out: list[P] = []
    for point in points:
        info = (
            verdicts.get(signal_triage_service.as_utc(point.bucket)) if point.is_anomaly else None
        )
        out.append(point if info is None else point.model_copy(update={"verdict": info}))
    return out


async def _with_latest_signal_state(
    session: AsyncSession, project_id: uuid.UUID, signal: MetricSignalResponse | None
) -> MetricSignalResponse | None:
    if signal is None:
        return None
    triaged = await signal_triage_service.apply_triage(
        session, project_id, [signal], drop_hidden=False
    )
    return triaged[0] if triaged else signal


async def with_chart_verdicts(
    session: AsyncSession,
    project_id: uuid.UUID,
    response: EventMetricsResponse,
    *,
    scope_type: str,
    scope_ref: str,
) -> EventMetricsResponse:
    """The drilldown response with each flagged point's verdict (for the chart
    marker tooltip) and the latest signal's triage, verdict and incident."""
    verdicts = await _verdicts_for_scope(
        session,
        project_id,
        scan_config_id=response.scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        buckets=[point.bucket for point in response.data if point.is_anomaly],
    )
    latest = await _with_latest_signal_state(session, project_id, response.latest_signal)
    if not verdicts and latest is response.latest_signal:
        return response
    return response.model_copy(
        update={"data": _stamp_points(response.data, verdicts), "latest_signal": latest}
    )


async def with_metric_series_verdicts(
    session: AsyncSession,
    project_id: uuid.UUID,
    response: MetricSeriesResponse,
) -> MetricSeriesResponse:
    """``with_chart_verdicts`` for a catalog metric's series (NULL scan config)."""
    verdicts = await _verdicts_for_scope(
        session,
        project_id,
        scan_config_id=None,
        scope_type=MetricScopeType.metric.value,
        scope_ref=str(response.metric_id),
        buckets=[point.bucket for point in response.data if point.is_anomaly],
    )
    latest = await _with_latest_signal_state(session, project_id, response.latest_signal)
    if not verdicts and latest is response.latest_signal:
        return response
    return response.model_copy(
        update={"data": _stamp_points(response.data, verdicts), "latest_signal": latest}
    )


async def verdict_state(
    session: AsyncSession, project_id: uuid.UUID, key: SignalKey
) -> SignalVerdictResponse:
    """The signal's triage fields, verdict and incident after a write."""
    state = await signal_triage_service.state_after_write(session, project_id, key)
    index = (
        await signal_triage_service.load_triage_indexes(session, [project_id], min_bucket=key[3])
    ).get(project_id) or TriageIndex()
    refs = await alerting_service.incident_refs_for_signals(session, project_id, [key])
    ref = refs.get(key)
    record = index.verdicts.get(key)
    names = await load_user_names(
        session,
        [record.author_id if record is not None else None, ref.acted_by if ref else None],
    )
    incident: SignalIncidentBrief | None = incident_brief(ref)
    # A routed signal takes no acknowledge / mute / expected state unless its
    # own pre-routing verdict still prevails; mirror the lists (``apply_triage``).
    base = state if ref is None or record_prevails(record, ref) else SignalTriageState()
    return SignalVerdictResponse(
        **base.model_dump(),
        verdict=resolve_verdict(record, ref, names),
        incident=incident,
    )
