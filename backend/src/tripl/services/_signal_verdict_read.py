"""How a signal's verdict reads (F01, #254) — shared by the lists, the badge and the charts.

A signal's verdict comes from one of two places:

* its own ``SignalTriage`` verdict row, when no rule routed it to an incident;
* the incident, when one did. The incident is the source of truth: its status
  IS the verdict (acknowledged -> ``real_issue``, resolved -> ``expected``,
  false_positive -> ``false_positive``; open and muted -> none). A verdict row
  on the signal that agrees with that status refines it — ``tracking_bug``
  rather than ``real_issue`` on an acknowledged incident, a reason and note on
  an ``expected`` one — and one the incident has since moved away from (someone
  reopened it in the inbox) is ignored, so the two surfaces cannot disagree.
  The inbox also deletes such a row when it moves the incident
  (``_alerting_deliveries.prune_disagreeing_signal_verdicts``), so a later move
  back cannot resurrect it.

One exception: a verdict set on the signal BEFORE a rule routed it keeps
winning while the incident is still open and nobody has acted on it
(``acted_at IS NULL``) — routing must not silently discard a call someone
already made (``record_prevails``).

Kept apart from ``signal_triage_service`` so the write-side verdict service and
the read-side triage index can both import it without a cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.bucketing import to_utc
from tripl.models.domain_enums import (
    AlertInboxStatus,
    SignalExpectedReason,
    SignalVerdict,
)
from tripl.models.user import User
from tripl.schemas.event_metric import SignalIncidentBrief, SignalVerdictInfo

# The triage actions that are verdicts; a signal carries at most one of them.
VERDICT_ACTIONS: frozenset[str] = frozenset(verdict.value for verdict in SignalVerdict)

# The verdict an incident status reads as, when the signal has no agreeing row.
INCIDENT_STATUS_VERDICT: dict[str, SignalVerdict] = {
    AlertInboxStatus.acknowledged.value: SignalVerdict.real_issue,
    AlertInboxStatus.resolved.value: SignalVerdict.expected,
    AlertInboxStatus.false_positive.value: SignalVerdict.false_positive,
}

# The signal verdicts each incident status agrees with. ``resolved`` takes a
# real issue or tracking bug too: fixing an acknowledged incident resolves it,
# and that must not rewrite the signal's verdict to "expected". ``open`` and
# ``muted`` agree with none.
STATUS_AGREES_WITH: dict[str, frozenset[SignalVerdict]] = {
    AlertInboxStatus.acknowledged.value: frozenset(
        {SignalVerdict.real_issue, SignalVerdict.tracking_bug}
    ),
    AlertInboxStatus.resolved.value: frozenset(
        {SignalVerdict.expected, SignalVerdict.real_issue, SignalVerdict.tracking_bug}
    ),
    AlertInboxStatus.false_positive.value: frozenset({SignalVerdict.false_positive}),
}

# The inbox action a verdict writes through to the signal's incident, and the
# status that action leaves it in.
VERDICT_INCIDENT_ACTION: dict[SignalVerdict, tuple[str, str]] = {
    SignalVerdict.false_positive: ("false_positive", AlertInboxStatus.false_positive.value),
    SignalVerdict.real_issue: ("acknowledge", AlertInboxStatus.acknowledged.value),
    SignalVerdict.tracking_bug: ("acknowledge", AlertInboxStatus.acknowledged.value),
    SignalVerdict.expected: ("resolve", AlertInboxStatus.resolved.value),
}


@dataclass(frozen=True)
class VerdictRecord:
    """One stored verdict row, as the read side needs it."""

    verdict: SignalVerdict
    expected_reason: SignalExpectedReason | None
    note: str | None
    author_id: uuid.UUID | None
    set_at: datetime | None


class IncidentLike(Protocol):
    """Structural stand-in for ``alerting_service.SignalIncidentRef``.

    A protocol so this module need not import the alerting services, which pull
    in far more than a read helper should.
    """

    @property
    def correlation_group_id(self) -> uuid.UUID: ...
    @property
    def status(self) -> str: ...
    @property
    def acted_by(self) -> uuid.UUID | None: ...
    @property
    def acted_at(self) -> datetime | None: ...
    @property
    def note(self) -> str | None: ...
    @property
    def routed_at(self) -> datetime | None: ...


def is_verdict_action(action: str) -> bool:
    return str(action) in VERDICT_ACTIONS


def record_from_row(
    action: str,
    expected_reason: str | None,
    note: str | None,
    author_id: uuid.UUID | None,
    set_at: datetime | None,
) -> VerdictRecord:
    return VerdictRecord(
        verdict=SignalVerdict(str(action)),
        expected_reason=(
            SignalExpectedReason(str(expected_reason)) if expected_reason is not None else None
        ),
        note=note,
        author_id=author_id,
        set_at=set_at,
    )


def status_agrees(status: str, verdict: SignalVerdict | str) -> bool:
    """Whether an incident in ``status`` agrees with a signal ``verdict``."""
    return SignalVerdict(str(verdict)) in STATUS_AGREES_WITH.get(str(status), frozenset())


def record_prevails(record: VerdictRecord | None, ref: IncidentLike | None) -> bool:
    """Whether the signal's own row wins over its incident's status.

    True when the row was set before the signal was routed and the incident is
    still open and untouched: nobody has decided anything on it yet, so the
    verdict already made on the signal is the only call on record.
    """
    if record is None or ref is None:
        return False
    if ref.status != AlertInboxStatus.open.value or ref.acted_at is not None:
        return False
    if record.set_at is None or ref.routed_at is None:
        return False
    return to_utc(record.set_at) <= to_utc(ref.routed_at)


def incident_brief(ref: IncidentLike | None) -> SignalIncidentBrief | None:
    if ref is None:
        return None
    return SignalIncidentBrief(id=ref.correlation_group_id, status=AlertInboxStatus(ref.status))


def resolve_verdict(
    record: VerdictRecord | None,
    ref: IncidentLike | None,
    names: dict[uuid.UUID, str],
) -> SignalVerdictInfo | None:
    """The verdict a signal shows, given its own row and its incident (if any)."""
    if ref is None:
        if record is None:
            return None
        return _from_record(record, names)
    if record is not None and (
        record_prevails(record, ref) or status_agrees(ref.status, record.verdict)
    ):
        return _from_record(record, names)
    derived = INCIDENT_STATUS_VERDICT.get(ref.status)
    if derived is None:
        return None
    return SignalVerdictInfo(
        verdict=derived,
        expected_reason=None,
        note=ref.note,
        author_name=names.get(ref.acted_by) if ref.acted_by is not None else None,
        created_at=ref.acted_at,
        source="incident",
    )


def _from_record(record: VerdictRecord, names: dict[uuid.UUID, str]) -> SignalVerdictInfo:
    return SignalVerdictInfo(
        verdict=record.verdict,
        expected_reason=record.expected_reason,
        note=record.note,
        author_name=names.get(record.author_id) if record.author_id is not None else None,
        created_at=record.set_at,
        source="signal",
    )


def has_verdict(record: VerdictRecord | None, ref: IncidentLike | None) -> bool:
    """Whether the signal reads as having a verdict — the badge's exclusion test."""
    if ref is None:
        return record is not None
    return record_prevails(record, ref) or ref.status in INCIDENT_STATUS_VERDICT


async def load_user_names(
    session: AsyncSession, user_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    """``{user_id: display name}``; the name, else the email."""
    wanted = {user_id for user_id in user_ids if user_id is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(User.id, User.name, User.email).where(User.id.in_(wanted)))
    return {user_id: (name or email) for user_id, name, email in rows.all()}
