"""URL builders + small text helpers used by alert payloads.

Every builder here is HANDED ``app_base_url``; none of them reads it back out
of the database. All three used to open with
``app_settings_service.get_runtime_config_sync()``, and called with no session
that helper checks out a SECOND pooled connection and runs two ``app_settings``
SELECTs — it has no cache of any kind. ``_build_item_paths`` runs twice per
alert item (once for the typed ``AlertDeliveryItem``, once for the frozen
``payload_snapshot``), and the two runs together cost 3 of those reads per item
for an ordinary scope, 2 for a release regression, so a 24-item digest paid for
48-72 of them. Worse, it paid while ``alert_flush._build_digest`` held FOR
UPDATE locks on the whole buffer plus the flush advisory lock on the FIRST
connection: a bounded pool and a second connection taken under a lock is the
classic way to turn a slow settings read into a stuck flush.

Taking the value as an argument also makes it CONSISTENT. That helper swallows
every exception and falls back to the env config, so one failing read used to
give an item a link and its neighbour none — inside a single delivery, with
nothing but a ``logger.warning`` to say so, and with the typed row and the
snapshot disagreeing about the same item. ``dispatch._create_deliveries``
resolves it ONCE, on the session it already holds, and builds every item of
that delivery from that one value.

``app_base_url`` is a ``str``, never ``None``: ``RuntimeConfig.app_base_url``
is non-optional and an unconfigured instance carries ``""``. The empty string
is the "emit no link" case, and every builder below still guards for it, so
behaviour at an unconfigured base URL is exactly what it was.

Every link names the project's ORGANIZATION as well (F20 PR8):
``{base}/o/{org_slug}/p/{project_slug}/...``, built by
``services.project_links.project_url``. A project slug is unique only inside
its organization, so a slug-only link opened whichever project of that slug the
reader's organization held. ``org_slug`` is resolved once per delivery beside
``app_base_url`` (``dispatch._create_deliveries`` via :func:`_get_org_slug`);
organization slugs are immutable, so a sent link never goes stale on a rename.
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.alerting_matching import (
    SCOPE_METRIC,
    SCOPE_RELEASE_REGRESSION,
)
from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
)
from tripl.models.project import Project
from tripl.services.project_links import project_org_slugs_sync, project_url

# The one scope with nowhere else to go at all.
#
# The audit row renders a fixed 8 columns — Grp / Scope / Direction / Actual /
# Expected / Abs delta / % delta / Link (frontend AlertDeliveryRow.tsx) — and
# ``drift_field`` and ``sample_value`` are not among them. For a drift scope
# those two fields ARE the finding, so the audit row shows a reader strictly
# LESS than the message that brought them there. It is only an upgrade for
# release regression, whose actual/expected/% delta exist nowhere else.
#
# Hence the rule, which is a last resort rather than a default: link an item
# to the audit row only when NO page shows more than the message does.
#
# ``variable_value_drift`` was briefly in this set and should not have been: its
# ``event_id`` is NOT NULL, and /monitoring/event/{event_id} mounts
# EventValueDriftPanel, which names the variable, lists every observed value
# (not the truncation the message sends) and can resolve the drift. ``schema``
# and ``distribution`` stay out for the first reason above; they carry a NULL
# ``event_id``, so they simply get no link, which at least does not mislead.
_SCOPES_LINKED_TO_ALERT_AUDIT = frozenset({SCOPE_RELEASE_REGRESSION})


def _build_monitoring_url(
    project_slug: str,
    *,
    org_slug: str,
    app_base_url: str,
    scope_type: str,
    scope_ref: str,
) -> str | None:
    if not app_base_url:
        return None

    def link(path: str) -> str:
        return project_url(org_slug, project_slug, path, base_url=app_base_url)

    if scope_type == SCOPE_PROJECT_TOTAL:
        return link(f"/monitoring/project-total/{scope_ref}")
    if scope_type == SCOPE_EVENT_TYPE:
        return link(f"/monitoring/event-type/{scope_ref}")
    if scope_type == SCOPE_METRIC:
        # Catalog-metric anomalies carry scope_ref = metric_definition_id, and
        # the metric drilldown is its own route. The fallthrough used to send
        # them to /monitoring/event/{metric_definition_id} — same defect class
        # as the release-regression link, just a quieter one.
        return link(f"/monitoring/metric/{scope_ref}")
    if scope_type == SCOPE_EVENT:
        return link(f"/monitoring/event/{scope_ref}")
    # Everything else — schema drift, distribution drift, release regression,
    # variable-value drift — describes a SLICE of a scan rather than one catalog
    # entity, so no entity-level monitoring page can show it. They get no link
    # rather than a guessed one, which is the backend half of a decision the
    # frontend had already made on its own: ``getMonitoringPath``
    # (frontend/src/lib/monitoring.ts) THROWS for exactly these rather than fall
    # through to the event route, because "routing them to the event URL (the
    # previous silent default) mis-renders an unrelated event".
    #
    # This return used to BE that silent default, and it is this function's
    # whole history of defects. For a release regression it emitted the event's
    # own monitoring page, which charts ALL versions over the chart's own range
    # against the seasonal baseline — a different numerator, denominator, window
    # AND estimator than the alert's release-cohort comparison, so it could not
    # corroborate the alert even in principle. For an event-TYPE-scoped release
    # regression it was worse: scope_ref is an event_type_id, so it emitted
    # ``/monitoring/event/{event_type_id}`` — a valid-looking URL for a page that
    # does not exist. The same reasoning had already killed the sparkline on
    # these items; nobody had applied it here.
    #
    # So: name every scope above, and let an unknown one fall to None. The set
    # that used to be written out here as a constant is deliberately gone — it
    # was never read, and a list that has to be kept in step with the branches
    # above is one more thing that can drift out of step with them.
    return None


def _build_event_details_url(
    project_slug: str,
    event_id: uuid.UUID | None,
    *,
    org_slug: str,
    app_base_url: str,
) -> str | None:
    if not app_base_url or event_id is None:
        return None
    return project_url(
        org_slug, project_slug, f"/monitoring/event/{event_id}", base_url=app_base_url
    )


# Query parameter naming the ONE item inside a delivery that a given message
# line was printed for. A query param rather than a fragment because that is the
# convention this frontend already uses for cross-page focus state — branch
# context, saved views, the events route state and the metrics catalog all ride
# ``useSearchParams``, and nothing in the app reads ``location.hash``.
ALERT_AUDIT_ITEM_PARAM = "item"

# Query parameter naming the INCIDENT the alert belongs to — the thing the page
# can act on. Same convention as above, and separate from ``item`` on purpose:
# the item picks the row a message line quoted, the incident picks the card that
# holds the actions, and one delivery can carry items from more than one.
ALERT_INCIDENT_PARAM = "incident"


def _alert_audit_item_anchor(scope_type: str, scope_ref: str) -> str:
    """Identity of one item within a delivery, as a URL query value.

    ``scope_ref`` alone is NOT an identity, even inside a single delivery: a
    release regression's scope_ref IS its event id, and one rule can carry both
    ``include_events`` and ``include_release_regressions``, so the event-scoped
    item and the regression arrive together holding the same ref. The pair
    ``(scope_type, scope_ref)`` is unique by construction — it is the key
    ``_prepare_alert_deliveries`` dedupes candidates on, and the partition
    cooldown is applied over.

    The pair is also what the frozen ``payload_snapshot`` stores, so the anchor
    stays resolvable against the audit record itself, which a row primary key
    (not present in the snapshot, and not yet minted when the URL is built)
    would not be.

    ``scope_type`` is a DB enum and can never contain ``:``, so the separator is
    unambiguous however free-form the ref gets; the value is percent-encoded
    because ``AlertDeliveryItem.scope_ref`` is a plain ``String(64)`` and the
    drift scopes key theirs on a field name.
    """
    return quote(f"{scope_type}:{scope_ref}", safe=":")


def _build_alert_audit_url(
    project_slug: str,
    delivery_id: uuid.UUID | None,
    *,
    org_slug: str,
    app_base_url: str,
    scope_type: str,
    scope_ref: str,
    correlation_group_id: uuid.UUID | None = None,
) -> str | None:
    """Deep link to one ITEM of one delivery's own audit row.

    The only surface that can show a release regression's ``actual`` /
    ``expected`` / ``% delta`` for one scope is the delivery that carried them:
    they are read back from the frozen delivery record, so the page can never
    disagree with the message a reader is holding, and it never goes stale.
    The monitoring panel cannot do that job — its rows are deleted and
    recomputed on every scan, so they only ever describe the CURRENT latest
    release.

    The delivery alone is not the destination, though. ``_delivery_chunks``
    packs up to ``_MAX_ITEMS_PER_DELIVERY`` (8, telegram) items into one
    delivery, so a delivery-keyed URL made every line of an 8-scope message
    byte-identical: clicking the sixth opened a row expanded over 8 siblings
    with nothing marking the one the message quoted. The production alert that
    prompted this link carried a single item, which is why it looked right.
    """
    if not app_base_url or delivery_id is None:
        return None
    anchor = _alert_audit_item_anchor(scope_type, scope_ref)
    url = project_url(
        org_slug,
        project_slug,
        f"/alerting/{delivery_id}?{ALERT_AUDIT_ITEM_PARAM}={anchor}",
        base_url=app_base_url,
    )
    # The incident is what the page acts on — Ack / Resolve / Mute / False
    # positive all key on it — so the link carries it and the page can open the
    # right card with its actions in view. Without it the reader landed on the
    # delivery and had to find the matching incident in a second list further up
    # the page. The delivery id and item anchor stay in the URL:
    # they still pick the exact row the message quoted, and links sent before
    # this change keep working.
    #
    # Note what this bakes in. The handle is DERIVED, not stable: it is a uuid5
    # over the scope's partition, rule, scope_type, scope_ref and direction
    # (``dispatch._correlation_group_id``), so re-defining any of those inputs
    # re-keys every live incident — and this string is then a SECOND, untyped
    # copy of the handle, one no schema knows about. It has happened once
    # already: f4a8d3c72e19 moves catalog metrics onto a project-global
    # partition, and it rewrites this substring alongside the column for exactly
    # the rows it re-keys, because ``get_alert_inbox_group`` answers a retired
    # handle with 404 and nothing records old -> new. A link is the right thing
    # to send — the reader has to land on the card that holds the actions — but
    # any future revision that re-keys the handle owes ``details_path`` the same
    # pass, or it ships messages pointing at an incident that exists nowhere.
    if correlation_group_id is not None:
        url = f"{url}&{ALERT_INCIDENT_PARAM}={correlation_group_id}"
    return url


def _build_item_paths(
    project_slug: str,
    *,
    org_slug: str,
    app_base_url: str,
    scope_type: str,
    scope_ref: str,
    event_id: uuid.UUID | None,
    delivery_id: uuid.UUID | None,
    correlation_group_id: uuid.UUID | None = None,
) -> tuple[str | None, str | None]:
    """``(details_path, monitoring_path)`` for one alert item.

    One choke point so the two builders can never be applied to a scope they
    were not written for. ``_build_event_details_url`` in particular takes no
    scope_type at all and fired for anything with a non-null ``event_id`` —
    which for an event-scoped release regression meant the event page, the one
    page guaranteed to contradict the alert.

    Being the choke point is also why ``app_base_url`` is required here rather
    than defaulted: this function is the only writer of
    ``AlertDeliveryItem.details_path`` / ``monitoring_path`` and of the matching
    pair in ``payload_snapshot``, so it mints every link an alert carries, and a
    default would let a new call site ship link-less alerts in silence. See the
    module docstring for why the value is passed in at all.

    ``org_slug`` is required for the same reason (F20 PR8): since project slugs
    are unique per organization only, a link without the organization opens
    whichever project of that slug the READER's organization holds.
    """
    # One destination for every alert, whatever fired it: the incident, where the
    # actions are. Previously only release regressions came here and everything
    # else was sent to the event/monitoring page — which shows neither what was
    # sent nor Ack / Resolve / Mute, so acting on an anomaly alert meant finding
    # the incident by hand on a different page. ``monitoring_path``
    # stays empty to keep the message to one link; the incident card carries the
    # link onward to the chart.
    if correlation_group_id is not None:
        return (
            _build_alert_audit_url(
                project_slug,
                delivery_id,
                org_slug=org_slug,
                app_base_url=app_base_url,
                scope_type=scope_type,
                scope_ref=scope_ref,
                correlation_group_id=correlation_group_id,
            ),
            None,
        )
    # No incident to point at — pre-tripl-jfm3.91 rows, and anything the inbox
    # cannot act on. Keep the old behaviour rather than emitting a link to a card
    # that will not be there.
    if scope_type in _SCOPES_LINKED_TO_ALERT_AUDIT:
        # ``details`` rather than ``monitoring``: the destination is this
        # alert's own detail, not a monitoring page. Leaving monitoring_path
        # empty also keeps the message to one link.
        return (
            _build_alert_audit_url(
                project_slug,
                delivery_id,
                org_slug=org_slug,
                app_base_url=app_base_url,
                scope_type=scope_type,
                scope_ref=scope_ref,
            ),
            None,
        )
    return (
        _build_event_details_url(
            project_slug, event_id, org_slug=org_slug, app_base_url=app_base_url
        ),
        _build_monitoring_url(
            project_slug,
            org_slug=org_slug,
            app_base_url=app_base_url,
            scope_type=scope_type,
            scope_ref=scope_ref,
        ),
    )


def _get_project_slug(session: Session, project_id: uuid.UUID) -> str:
    slug = session.execute(
        select(Project.slug).where(Project.id == project_id)
    ).scalar_one_or_none()
    if slug is None:
        msg = f"Project {project_id} not found"
        raise ValueError(msg)
    return slug


def _get_org_slug(session: Session, project_id: uuid.UUID) -> str:
    """The slug of the organization owning ``project_id``, for its deep links."""
    org_slug = project_org_slugs_sync(session, [project_id]).get(project_id)
    if org_slug is None:
        msg = f"Project {project_id} not found"
        raise ValueError(msg)
    return org_slug


def _trim_alert_text(value: str | None, *, max_length: int = 500) -> str | None:
    if value is None or len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."
