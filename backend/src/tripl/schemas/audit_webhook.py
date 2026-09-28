"""The audit webhook's request and response bodies (F20, GH #273)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AuditWebhookDeliveryStatus = Literal["pending", "failed", "sent", "dead"]


class AuditWebhookUpdate(BaseModel):
    """``PUT /orgs/{org}/audit/webhook``: create it, or change its URL or switch.

    The secret is never sent: it is generated on create and on
    ``POST .../rotate-secret``, and shown once.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _clean(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or any(ch.isspace() or ord(ch) < 32 for ch in stripped):
            raise ValueError("must not be empty or contain whitespace")
        return stripped


class AuditWebhookResponse(BaseModel):
    """The webhook as its owner reads it; ``configured`` false when there is none."""

    configured: bool
    url: str = ""
    enabled: bool = False
    #: The secret itself is shown once (``AuditWebhookSaved.secret``), never again.
    secret_configured: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_success_at: datetime | None = None
    #: A short fixed text (``HTTP 500``, ``timeout``), never the receiver's body.
    last_error: str | None = None
    last_error_at: datetime | None = None


class AuditWebhookSaved(AuditWebhookResponse):
    """What a create or a rotation answers: the new secret, this once."""

    secret: str | None = None


class AuditWebhookTestResult(BaseModel):
    ok: bool
    #: The receiver's HTTP status, when it answered.
    status_code: int | None = None
    error: str | None = None
    #: ``X-Tripl-Event-Id`` of the synthetic ``audit.webhook_test`` event.
    event_id: uuid.UUID


class AuditWebhookDeliveryResponse(BaseModel):
    id: uuid.UUID
    audit_log_id: uuid.UUID
    action: str
    status: AuditWebhookDeliveryStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
