"""Owner routing payloads (F07, #260).

Their own module because two payload families carry them — the alerting ones
(``schemas/alerting.py``) and the signal ones (``schemas/event_metric.py``) —
and neither should import the other.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from tripl.models.alert_owner_notification import AlertOwnerNotificationStatus


class AlertOwnerRef(BaseModel):
    """An owner of the affected event type or catalog metric, for display.

    Only current project members with an email address are listed — exactly the
    people "Notify owners" would email. ``name`` falls back to the local part of
    the address, never to the whole address.
    """

    user_id: uuid.UUID
    name: str


class AlertOwnerNotificationResponse(BaseModel):
    """One owner email: who, where to, and what became of it.

    ``user_id`` is NULL once the user was deleted; the address stays on record.
    ``pending`` is a short-lived claim a worker holds while it sends.
    """

    user_id: uuid.UUID | None
    name: str | None
    email: str
    status: AlertOwnerNotificationStatus
    # Why a ``failed`` / ``skipped`` row did not send.
    error: str | None = None
    sent_at: datetime | None = None


class NotifyOwnersResponse(BaseModel):
    """What a manual "Notify owners" did: one row per owner it tried."""

    owners: list[AlertOwnerNotificationResponse] = Field(default_factory=list)
