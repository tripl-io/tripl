"""Collaboration builder: what the team decided and keeps an eye on.

Authors three things a working team leaves behind:

* one dismissed near-duplicate. The roster carries a deliberate pair
  (``DUPLICATE_PAIRS``: a live event and a ready-for-dev twin in another team's
  naming, describing the same moment),
  which stays open for the duplicates view to flag; a pair that only LOOKS
  alike (:data:`NOT_A_DUPLICATE`) was dismissed, so the view shows both
  outcomes;
* watch subscriptions for the creator on the events, the event type and the
  catalog metric the coached chapters revisit;
* two project docs pages: the tracking-plan conventions and the weekly promo
  runbook, linking the plan entities they talk about.

Everything needs a real author, so nothing is written without a creator.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.duplicate_dismissal import DuplicateDismissal
from tripl.models.project import Project
from tripl.models.subscription import Subscription, SubscriptionEntityType, SubscriptionReason
from tripl.models.user import User
from tripl.services import _docs_store as docs_store
from tripl.services.demo.builders.catalog import REVENUE_METRIC_NAME
from tripl.services.demo.scenario import DemoContext
from tripl.services.docs_frontmatter import parse_frontmatter

#: Two events whose names differ by one word and mean opposite things.
NOT_A_DUPLICATE = ("Push Permission Granted", "Push Permission Denied")
#: (entity type, name) the creator watches.
WATCHED: tuple[tuple[SubscriptionEntityType, str], ...] = (
    (SubscriptionEntityType.event, "Paywall View"),
    (SubscriptionEntityType.event, "Purchase Completed"),
    (SubscriptionEntityType.event_type, "purchase"),
    (SubscriptionEntityType.metric, REVENUE_METRIC_NAME),
)

_DOCS_WRITTEN_BEFORE_SEED = timedelta(days=9)

DOCS: tuple[tuple[str, str], ...] = (
    (
        "tracking-plan/conventions.md",
        """---
title: Tracking plan conventions
description: How events, properties and statuses are named in this plan
tags: [plan, conventions]
---
# Tracking plan conventions

* **Events are named "Object Action"** in Title Case: [[event:Paywall View]],
  [[event:Buy Button Click]], [[event:Purchase Completed]]. The apps send the
  same string as `event_name`.
* **The event type is the family**: every screen is a `screen_view`, every tap
  a `click`, every store transaction a `purchase`.
* **Shared values are variables.** `${platform}` and `${product_id}` are filled
  in from the row; the documented products live on [[variable:product_id]].
* **Statuses**: draft → in review → ready for dev → implemented → live. Retire
  an event by deprecating it with a successor and a sunset date, then archive it
  once nothing sends it.
* **Before adding an event**, search the plan: a near-duplicate costs two
  dashboards that never agree.
""",
    ),
    (
        "runbooks/weekly-promo.md",
        """---
title: Weekly promo runbook
description: What the Tuesday promo email does to the paywall, and what to ignore
tags: [runbook, alerts]
---
# Weekly promo runbook

Every **Tuesday at 10:00 UTC** Growth sends the weekly promo email. For about two
hours [[event:Paywall View]] runs at two to three times its usual volume.

* The spike is expected: mark it **Expected → campaign** in the inbox, or accept
  the suggested planned window so it stops alerting.
* A Tuesday *without* the spike means the send failed. Check with Growth.
* If [[event:Purchase Completed]] does not follow the paywall up, the checkout is
  the problem, not the email.
""",
    ),
)


async def build_collaboration(session: AsyncSession, ctx: DemoContext) -> None:
    if ctx.created_by is None:
        return
    user = await session.get(User, ctx.created_by)
    project = await session.get(Project, ctx.project_id)
    if user is None or project is None:
        return
    _build_duplicate_dismissal(session, ctx)
    _build_watches(session, ctx)
    for index, (path, content) in enumerate(DOCS):
        doc = docs_store.new_doc(project, "project", path)
        revision = await docs_store.apply_content(
            session,
            doc,
            content=content,
            parsed=parse_frontmatter(content, path),
            action="create",
            user=user,
            message="Initial version",
        )
        # Written during the history, not at the seed instant.
        written_at = ctx.now - _DOCS_WRITTEN_BEFORE_SEED + timedelta(days=index)
        doc.created_at = doc.updated_at = revision.created_at = written_at
    await session.flush()


def _build_duplicate_dismissal(session: AsyncSession, ctx: DemoContext) -> None:
    ids = [ctx.event_ids.get(name) for name in NOT_A_DUPLICATE]
    if None in ids:
        return
    # Stored ordered: one spelling per unordered pair (``ck_duplicate_dismissal_ordered``).
    first, second = sorted(event_id for event_id in ids if event_id is not None)
    session.add(
        DuplicateDismissal(
            project_id=ctx.project_id,
            event_a_id=first,
            event_b_id=second,
            dismissed_by=ctx.created_by,
        )
    )


def _build_watches(session: AsyncSession, ctx: DemoContext) -> None:
    assert ctx.created_by is not None
    ids_by_kind = {
        SubscriptionEntityType.event: ctx.event_ids,
        SubscriptionEntityType.event_type: ctx.event_type_ids,
        SubscriptionEntityType.metric: ctx.metric_ids,
    }
    for entity_type, name in WATCHED:
        entity_id = ids_by_kind[entity_type].get(name)
        if entity_id is None:
            continue
        session.add(
            Subscription(
                user_id=ctx.created_by,
                project_id=ctx.project_id,
                entity_type=entity_type.value,
                entity_id=entity_id,
                reasons=[SubscriptionReason.manual.value],
            )
        )
