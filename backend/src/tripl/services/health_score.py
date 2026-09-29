"""The event health score (F15, #268): a pure function of plain facts.

``score_event(facts, now)`` touches no database. Every input it reads is on the
frozen :class:`EventHealthFacts`, which ``event_health_service.load_facts``
builds in a fixed number of batched queries, so the same facts and the same
``now`` always give the same score and unit tests can build facts by hand.

Six components each map to a value in ``0..1`` or are excluded with a reason.
The score is ``round(100 * sum(w_i * v_i) / sum(w_i))`` over the components that
apply, so an excluded component does not drag an event down; the others are
renormalized over their own weight. Weights and thresholds live in
``health_weights``.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tripl.core.bucketing import to_utc
from tripl.schemas.health import EventHealth, HealthComponent
from tripl.services import health_weights as hw
from tripl.services.health_weights import HealthComponentKey, HealthGrade
from tripl.services.source_freshness import format_duration

# Short labels for contract expectation kinds in the component detail.
CONTRACT_KIND_LABELS: dict[str, str] = {
    "required_null_violation": "required",
    "enum_violation": "enum",
    "regex_violation": "regex",
    "range_violation": "range",
}
# How many failing rules the contract detail names before "and N more".
_CONTRACT_DETAIL_MAX = 3

NOT_IMPLEMENTED_REASON = "Not implemented yet"
NO_CONTRACT_RULES_REASON = "No contract rules on this event type"
NOT_COVERED_REASON = "Not covered by any scan"
DETECTION_OFF_REASON = "Anomaly detection is off for its scans"
NO_SCHEDULED_SOURCE_REASON = "No scheduled source"


@dataclass(frozen=True)
class EventHealthFacts:
    """Everything the score of one event depends on, as plain values."""

    event_id: uuid.UUID
    event_type_id: uuid.UUID
    name: str
    status: str
    last_seen_at: datetime | None = None
    lifecycle_kinds: frozenset[str] = frozenset()
    has_description: bool = False
    has_owner: bool = False
    contract_total: int = 0
    contract_violated: int = 0
    # ``(field_name, drift_type)`` of each violated expectation, sorted.
    contract_violations: tuple[tuple[str, str], ...] = ()
    schema_drifts: int = 0
    value_drifts: int = 0
    # Open per-event property drifts (F23): new property, missing required.
    property_drifts: int = 0
    distribution_drifts: int = 0
    covered: bool = False
    detection_enabled: bool = False
    open_signals: int = 0
    # ``(config_name, status, lag_seconds)`` for each covering scan config.
    freshness: tuple[tuple[str, str, int | None], ...] = field(default=())


@dataclass(frozen=True)
class _Part:
    applies: bool
    value: float | None
    detail: str
    excluded_reason: str | None = None
    counts: dict[str, int] = field(default_factory=dict)


def _excluded(reason: str) -> _Part:
    return _Part(applies=False, value=None, detail=reason, excluded_reason=reason)


def _is_planned(status: str) -> bool:
    return status in hw.PLANNED_STATUSES


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _implemented_seen(facts: EventHealthFacts, now: datetime) -> _Part:
    if _is_planned(facts.status):
        return _excluded(f"{NOT_IMPLEMENTED_REASON} (status: {facts.status})")
    if facts.status == "deprecated":
        if "sunset_overdue" in facts.lifecycle_kinds:
            return _Part(
                applies=True,
                value=hw.LIFECYCLE_SUNSET_OVERDUE_VALUE,
                detail="Still receiving data after sunset",
                counts={"sunset_overdue": 1},
            )
        if "successor_silent" in facts.lifecycle_kinds:
            return _Part(
                applies=True,
                value=hw.LIFECYCLE_SUCCESSOR_SILENT_VALUE,
                detail="Its successor receives no data",
                counts={"successor_silent": 1},
            )
        return _Part(applies=True, value=1.0, detail="Deprecated with no open lifecycle findings")
    if facts.last_seen_at is None:
        return _Part(applies=True, value=0.0, detail="Never seen in data")
    age = to_utc(now) - to_utc(facts.last_seen_at)
    days = max(0, age.days)
    detail = "Last seen today" if days == 0 else f"Last seen {days}d ago"
    if age <= timedelta(days=hw.SEEN_RECENT_DAYS):
        value = 1.0
    elif age <= timedelta(days=hw.SEEN_STALE_DAYS):
        value = hw.SEEN_STALE_VALUE
    else:
        value = 0.0
    return _Part(applies=True, value=value, detail=detail, counts={"days_since_seen": days})


def _contract(facts: EventHealthFacts) -> _Part:
    if _is_planned(facts.status):
        return _excluded(NOT_IMPLEMENTED_REASON)
    # Violations exist only as drift rows a scan writes: with no covering scan,
    # "all rules pass" would be a claim nothing verified.
    if not facts.covered:
        return _excluded(NOT_COVERED_REASON)
    if facts.contract_total <= 0:
        return _excluded(NO_CONTRACT_RULES_REASON)
    total = facts.contract_total
    violated = min(max(0, facts.contract_violated), total)
    counts = {"violated": violated, "total": total}
    if violated == 0:
        return _Part(
            applies=True,
            value=1.0,
            detail=f"All {_plural(total, 'contract rule')} pass",
            counts=counts,
        )
    named = [
        f"{field_name} ({CONTRACT_KIND_LABELS.get(kind, kind)})"
        for field_name, kind in facts.contract_violations[:_CONTRACT_DETAIL_MAX]
    ]
    rest = len(facts.contract_violations) - len(named)
    listing = ", ".join(named) + (f" and {rest} more" if rest > 0 else "")
    detail = f"{violated} of {_plural(total, 'contract rule')} failing"
    if listing:
        detail = f"{detail}: {listing}"
    return _Part(applies=True, value=1.0 - violated / total, detail=detail, counts=counts)


def _drifts(facts: EventHealthFacts) -> _Part:
    if _is_planned(facts.status):
        return _excluded(NOT_IMPLEMENTED_REASON)
    if not facts.covered:
        return _excluded(NOT_COVERED_REASON)
    counts = {
        "schema": facts.schema_drifts,
        "value": facts.value_drifts,
        "property": facts.property_drifts,
        "distribution": facts.distribution_drifts,
    }
    n = sum(counts.values())
    parts = [
        _plural(count, f"{label} drift")
        for label, count in (
            ("schema", facts.schema_drifts),
            ("value", facts.value_drifts),
            ("property", facts.property_drifts),
            ("distribution", facts.distribution_drifts),
        )
        if count
    ]
    detail = ", ".join(parts) if parts else "No open drifts"
    return _Part(
        applies=True, value=max(0.0, 1.0 - hw.DRIFT_PENALTY * n), detail=detail, counts=counts
    )


def _signals(facts: EventHealthFacts) -> _Part:
    if _is_planned(facts.status):
        return _excluded(NOT_IMPLEMENTED_REASON)
    if not facts.covered or not facts.detection_enabled:
        return _excluded(DETECTION_OFF_REASON)
    n = max(0, facts.open_signals)
    if n == 0:
        detail = "No signals need a verdict"
    elif n == 1:
        detail = "1 signal needs a verdict"
    else:
        detail = f"{n} signals need a verdict"
    return _Part(
        applies=True,
        value=max(0.0, 1.0 - hw.SIGNAL_PENALTY * n),
        detail=detail,
        counts={"open": n},
    )


def _freshness(facts: EventHealthFacts) -> _Part:
    if _is_planned(facts.status):
        return _excluded(NOT_IMPLEMENTED_REASON)
    known = [
        (name, status, lag) for name, status, lag in facts.freshness if status in hw.FRESHNESS_VALUE
    ]
    if not known:
        return _excluded(NO_SCHEDULED_SOURCE_REASON)
    # Worst first; ties by name so the detail is deterministic.
    worst_name, worst_status, worst_lag = min(
        known, key=lambda item: (hw.FRESHNESS_VALUE[item[1]], item[0])
    )
    value = hw.FRESHNESS_VALUE[worst_status]
    counts = {
        "sources": len(known),
        "late": sum(1 for _n, status, _l in known if status == "late"),
        "overdue": sum(1 for _n, status, _l in known if status == "overdue"),
    }
    if worst_status == "fresh":
        detail = f"Source '{worst_name}' is fresh" if len(known) == 1 else "All sources are fresh"
    else:
        lag = f" (lag {format_duration(timedelta(seconds=worst_lag))})" if worst_lag else ""
        detail = f"Source '{worst_name}' is {worst_status}{lag}"
    return _Part(applies=True, value=value, detail=detail, counts=counts)


def _documentation(facts: EventHealthFacts) -> _Part:
    value = 0.5 * facts.has_description + 0.5 * facts.has_owner
    missing = [
        text
        for text, present in (
            ("No description", facts.has_description),
            ("No owner", facts.has_owner),
        )
        if not present
    ]
    detail = ", ".join(missing) if missing else "Description and owner set"
    return _Part(
        applies=True,
        value=value,
        detail=detail,
        counts={
            "has_description": int(facts.has_description),
            "has_owner": int(facts.has_owner),
        },
    )


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def grade_for(score: int) -> HealthGrade:
    if score >= hw.GRADE_HEALTHY_MIN:
        return "healthy"
    if score >= hw.GRADE_WARNING_MIN:
        return "warning"
    return "unhealthy"


def _parts(facts: EventHealthFacts, now: datetime) -> dict[HealthComponentKey, _Part]:
    return {
        "implemented_seen": _implemented_seen(facts, now),
        "contract": _contract(facts),
        "drifts": _drifts(facts),
        "signals": _signals(facts),
        "freshness": _freshness(facts),
        "documentation": _documentation(facts),
    }


def score_event(facts: EventHealthFacts, now: datetime) -> EventHealth:
    """Score one event. Pure and deterministic in ``(facts, now)``."""
    parts = _parts(facts, now)
    applicable = [key for key in hw.COMPONENT_ORDER if parts[key].applies]
    weight_sum = sum(hw.COMPONENT_WEIGHTS[key] for key in applicable)
    weighted = sum(hw.COMPONENT_WEIGHTS[key] * (parts[key].value or 0.0) for key in applicable)
    # Documentation always applies, so ``weight_sum`` is never 0 in practice;
    # the guard keeps the function total anyway.
    raw = 100.0 * weighted / weight_sum if weight_sum else 0.0
    score = max(0, min(100, _round_half_up(raw)))

    components: list[HealthComponent] = []
    top_issue: str | None = None
    top_lost = 0.0
    for key in hw.COMPONENT_ORDER:
        part = parts[key]
        effective: float | None = None
        points: float | None = None
        if part.applies and weight_sum:
            share = hw.COMPONENT_WEIGHTS[key] / weight_sum * 100.0
            value = part.value or 0.0
            effective = round(share, 1)
            points = round(share * value, 1)
            lost = share * (1.0 - value)
            # Strictly greater: on a tie the earlier component in
            # COMPONENT_ORDER (the heavier one) names the issue.
            if lost > top_lost + 1e-9:
                top_lost = lost
                top_issue = part.detail
        components.append(
            HealthComponent(
                key=key,
                label=hw.COMPONENT_LABELS[key],
                weight=hw.COMPONENT_WEIGHTS[key],
                applies=part.applies,
                excluded_reason=part.excluded_reason,
                value=round(part.value, 4) if part.applies and part.value is not None else None,
                effective_weight=effective,
                points=points,
                detail=part.detail,
                counts=part.counts,
            )
        )

    excluded = [key for key in hw.COMPONENT_ORDER if not parts[key].applies]
    return EventHealth(
        event_id=facts.event_id,
        event_type_id=facts.event_type_id,
        name=facts.name,
        score=score,
        grade=grade_for(score),
        renormalized=bool(excluded),
        excluded=excluded,
        top_issue=top_issue,
        components=components,
    )
