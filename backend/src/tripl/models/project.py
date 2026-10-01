from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.enum_types import db_enum
from tripl.semver import DEFAULT_APP_VERSION_KEEP_RELEASES

if TYPE_CHECKING:
    from tripl.models.event_type import EventType
    from tripl.models.event_type_relation import EventTypeRelation
    from tripl.models.meta_field_definition import MetaFieldDefinition
    from tripl.models.variable import Variable


class Project(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        # F20 PR5: a slug names a project only inside its organization.
        UniqueConstraint("organization_id", "slug", name="uq_projects_organization_slug"),
        # The target of the composite (project_id, organization_id) foreign keys
        # on data_sources and api_keys: a project-bound row is in its project's org.
        UniqueConstraint("id", "organization_id", name="uq_projects_id_organization"),
    )

    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    # The owning organization. No ORM or server default (F20 PR5): a write that
    # forgets the organization fails on NOT NULL instead of landing in the
    # default organization.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, default="")
    # One retention policy for every app-version series in this project. The
    # source column remains per scan/metric, but the number of explicit releases
    # is a product-level choice and must not diverge between those sources.
    app_version_keep_releases: Mapped[int] = mapped_column(
        Integer,
        CheckConstraint(
            "app_version_keep_releases BETWEEN 1 AND 100",
            name="ck_projects_app_version_keep_releases_range",
        ),
        default=DEFAULT_APP_VERSION_KEEP_RELEASES,
        server_default=str(DEFAULT_APP_VERSION_KEEP_RELEASES),
        nullable=False,
    )
    # IANA zone name every wall-clock schedule in this project is read in —
    # today only alert digest cadences (`AlertDestination.delivery_schedule_cron`).
    # 'UTC' reproduces the implicit behaviour of every project that predates
    # this column, so the backfill is semantically a no-op. Validated against
    # `zoneinfo.available_timezones()` on write; an unresolvable value degrades
    # to UTC on read rather than stopping every other project's digest.
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC", nullable=False
    )
    # The docs catalog's default languages (lowercase BCP 47 tags): what an
    # agent reading a note without asking for a language gets, and what the
    # app opens notes in for people. NULL serves the original.
    docs_agent_lang: Mapped[str | None] = mapped_column(String(35), nullable=True, default=None)
    docs_human_lang: Mapped[str | None] = mapped_column(String(35), nullable=True, default=None)

    # ── Demo identity & provisioning lifecycle ─────────────────────────────
    # A demo is a first-class, generated project. Identity is this explicit
    # flag — never the slug, DataSource name, or host — so provisioning,
    # reset, cleanup, and runtime maintenance can target demos precisely.
    is_demo: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Which demo scenario recipe produced this workspace, so a reset or a
    # future migration can tell what shape to expect. NULL for real projects.
    demo_recipe_version: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    # Provisioning state machine. Real projects are ``ready`` from creation;
    # demos flow seeding -> ready / failed. Non-``ready`` demos are hidden from
    # normal project lists so a partial/failed demo never looks real.
    generation_status: Mapped[str] = mapped_column(
        db_enum(ProjectGenerationStatus, "project_generation_status"),
        default=ProjectGenerationStatus.ready.value,
        server_default=ProjectGenerationStatus.ready.value,
        nullable=False,
    )
    # Coarse label of the last provisioning step (progress hint). NULL when idle.
    generation_stage: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # Safe, user-facing failure summary when ``generation_status`` is ``failed``.
    # Never carries internals (stack traces, SQL, secrets).
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # Provenance: who created this project (demo self-service or otherwise). Lets
    # the creator manage their own demo without full owner rights. SET NULL so a
    # deleted user does not delete their projects.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    # When demo content last finished seeding — the anchor for retention and
    # runtime maintenance ticks. NULL until the first successful seed.
    demo_seeded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # When the demo-runtime tick (``advance_demos``) last advanced this demo's
    # synthetic clock. NULL until the first tick. Diagnostic/observability only —
    # the tick's correctness comes from the seeded series + DB unique constraints,
    # not this stamp.
    demo_last_tick_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Last explicit activity on this demo (a GET on the project, a reset/run). The
    # tick uses this to pause demos nobody is looking at (older than
    # ``DEMO_IDLE_PAUSE_MINUTES``) so inactive demos stop consuming worker time;
    # the next access resumes them. NULL falls back to ``demo_seeded_at``.
    demo_last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # These four collections support the ORM-level delete cascade in
    # ``project_service.purge_project_rows``. SQLite tests also enable foreign-key
    # cascades; the relationships preserve ORM cleanup independently of that. Nothing reads them —
    # every plan read goes through a branch-scoped query in the services.
    #
    # They must therefore stay lazily loaded. They used to be ``lazy="selectin"``,
    # which made EVERY ``get_project_by_slug()`` hydrate the whole plan — on a real
    # project that is four extra round trips and thousands of ORM rows (1272 Variable
    # rows on the largest one) to answer a request that only wanted ``project.id``
    # (tripl-jfm3.54). ``await session.delete(project)`` still loads them on demand:
    # AsyncSession.delete is a coroutine precisely so cascade can lazy-load.
    event_types: Mapped[list[EventType]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    meta_field_definitions: Mapped[list[MetaFieldDefinition]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    relations: Mapped[list[EventTypeRelation]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    variables: Mapped[list[Variable]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
