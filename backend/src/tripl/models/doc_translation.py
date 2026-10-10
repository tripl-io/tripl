"""Stored translations of docs catalog notes.

A note can carry one translation per language (``lang``, a lowercase BCP 47
tag such as ``en`` or ``pt-br``). A translation is made once by the
organization's AI model, in a Celery task, and stored; after that it is an
ordinary text that people may edit. It never changes on its own: when the
original moves past ``source_revision`` the translation is OUTDATED, and the
reader is told so until someone translates it again or marks it current.

Access is the original's: there is no visibility of its own, and deleting the
note deletes its translations. History is kept in
:class:`DocTranslationRevision`, one full copy per write, so translating again
over a person's edits never loses them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin


class DocTranslation(UUIDMixin, TimestampMixin, Base):
    """One language of one note."""

    __tablename__ = "doc_translations"
    __table_args__ = (
        UniqueConstraint("doc_file_id", "lang", name="uq_doc_translations_doc_lang"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_doc_translations_status"
        ),
    )

    doc_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("doc_files.id", ondelete="CASCADE"), index=True
    )
    lang: Mapped[str] = mapped_column(String(35))
    # ``pending`` while the task runs, ``ready`` once it holds text, ``failed``
    # when the last AI run did not produce one (``content`` keeps the text it had).
    status: Mapped[str] = mapped_column(String(8), default="pending")
    content: Mapped[str] = mapped_column(Text, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), default="")
    #: The original's revision ``content`` was translated from (or, while
    #: ``pending``, is being translated from).
    source_revision: Mapped[int] = mapped_column(Integer)
    #: Saves of ``content``; 0 until the first one lands.
    revision: Mapped[int] = mapped_column(Integer, default=0)
    #: True while ``content`` is the model's output as it came; a person's edit
    #: clears it, so translating again asks before replacing their work.
    machine: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )


class DocTranslationRevision(UUIDMixin, Base):
    """One saved state of a translation."""

    __tablename__ = "doc_translation_revisions"
    __table_args__ = (
        UniqueConstraint("translation_id", "number", name="uq_doc_translation_revisions_number"),
        CheckConstraint(
            "action IN ('translate', 'edit', 'restore')",
            name="ck_doc_translation_revisions_action",
        ),
    )

    translation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("doc_translations.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
