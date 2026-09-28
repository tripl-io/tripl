from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import EventPhotoKind, EventPhotoStorageBackend
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from tripl.models.event import Event


class EventPhoto(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "event_photos"
    __table_args__ = (Index("ix_event_photo_event_order", "event_id", "sort_order"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    original_filename: Mapped[str] = mapped_column(String(500), default="")
    content_type: Mapped[str] = mapped_column(String(120), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # Attachment shape. "photo" rows have storage_backend/storage_key populated
    # and no external_url; "figma" rows store the embed URL and leave the
    # storage_* columns NULL (no blob is uploaded).
    kind: Mapped[str] = mapped_column(
        db_enum(EventPhotoKind, "event_photo_kind"),
        default=EventPhotoKind.photo.value,
        server_default=EventPhotoKind.photo.value,
    )
    external_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # "local" or "gcs" — drives URL resolution and delete behavior. NULL for
    # figma-kind rows that have no uploaded blob.
    storage_backend: Mapped[str | None] = mapped_column(
        db_enum(EventPhotoStorageBackend, "event_photo_storage_backend"), nullable=True
    )
    # Relative key under the configured storage root. Same shape for both
    # backends: "orgs/<org_id>/events/<event_id>/<photo_id>.jpg" since F20 PR11,
    # "events/<event_id>/<photo_id>.jpg" before.
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Which organization's upload wrote the blob and with which storage (F20
    # PR11). ``storage_org_id`` is the organization whose ``orgs/{id}/`` key
    # prefix the blob lives under (NULL: written before PR11, legacy
    # ``events/...`` key). ``storage_config_id`` is the version of that
    # organization's OWN storage the blob was written with; NULL means the
    # operator's store named by ``storage_backend``. Reads, deletes and the
    # orphan sweep follow the row, never the current setting. Branch twins copy
    # all four storage columns with the key.
    storage_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, default=None
    )
    storage_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("photo_storage_configs.id", ondelete="RESTRICT"),
        nullable=True,
        default=None,
        index=True,
    )

    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    event: Mapped[Event] = relationship(lazy="selectin")
