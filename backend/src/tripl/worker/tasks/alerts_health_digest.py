"""The weekly digest's plan health lines (F15, #268).

Read from ``project_health_snapshots`` only — the digest never scores inline.
A project without a snapshot from the last ``SNAPSHOT_FRESH_DAYS`` days gets no
health lines at all, rather than a stale number.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.models.project_health_snapshot import ProjectHealthSnapshot
from tripl.services.health_weights import SNAPSHOT_FRESH_DAYS, TREND_DELTA_DAYS


def _delta_text(current: int, previous: int | None) -> str:
    if previous is None:
        return ""
    delta = current - previous
    if delta == 0:
        return " (no change vs last week)"
    return f" ({delta:+d} vs last week)"


def _health_digest_lines(session: Session, project_id: uuid.UUID, now: datetime) -> list[str]:
    """``- Plan health: 72/100 (-3 vs last week)`` plus the least healthy events.

    Returns ``[]`` when there is no fresh snapshot or it scored no events.
    """
    today = now.astimezone(UTC).date()
    latest: ProjectHealthSnapshot | None = session.scalar(
        select(ProjectHealthSnapshot)
        .where(
            ProjectHealthSnapshot.project_id == project_id,
            ProjectHealthSnapshot.day >= today - timedelta(days=SNAPSHOT_FRESH_DAYS),
        )
        .order_by(ProjectHealthSnapshot.day.desc())
        .limit(1)
    )
    if latest is None or latest.score is None:
        return []
    previous_score: int | None = session.scalar(
        select(ProjectHealthSnapshot.score).where(
            ProjectHealthSnapshot.project_id == project_id,
            ProjectHealthSnapshot.day == latest.day - timedelta(days=TREND_DELTA_DAYS),
        )
    )
    lines = [f"- Plan health: {latest.score}/100{_delta_text(latest.score, previous_score)}"]
    worst_lines: list[str] = []
    for entry in latest.worst_events or []:
        name = entry.get("name")
        score = entry.get("score")
        if name is None or score is None:
            continue
        issue = entry.get("top_issue")
        suffix = f" ({issue})" if issue else ""
        worst_lines.append(f"- {name}: {score}/100{suffix}")
    if worst_lines:
        lines.extend(["", "Least healthy events:", *worst_lines])
    return lines
