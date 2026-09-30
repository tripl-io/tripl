from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin


class PhotoStorageConfig(UUIDMixin, TimestampMixin, Base):
    """One version of an organization's own photo storage (F20 PR11).

    An organization that sets its own storage (a GCS bucket with its own
    service-account JSON, or — self-hosted only — the local backend) writes each
    blob with the configuration in force at that moment. Every
    ``event_photos`` row written under an organization's own storage points at
    the version it was written with (``EventPhoto.storage_config_id``), so a
    later change of bucket or credentials never sends a read of an older photo
    to the wrong store.

    Rows are immutable: a version is identified by ``config_hash`` (a digest of
    every value a driver is built from) and is written once, when the first blob
    is stored with it. ``value`` holds the driver values; the credential JSON is
    encrypted with :mod:`tripl.crypto`, like every stored secret. Rows of the
    OPERATOR's storage do not exist: a photo row with no ``storage_config_id``
    is in the operator's store, named by its ``storage_backend``.
    """

    __tablename__ = "photo_storage_configs"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "config_hash", name="uq_photo_storage_configs_org_hash"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    config_hash: Mapped[str] = mapped_column(String(64))
    backend: Mapped[str] = mapped_column(String(20))
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
