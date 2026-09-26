import type { MetricScopeType } from '@/types'
import { APP_LOCALE } from '@/lib/format'
import { ratioDelta } from '@/lib/percentDelta'
import { relativeEffect, signalMagnitudeWord } from '@/lib/signalMagnitude'

// The subset of scopes that map to a monitoring detail route / API scope.
// `metric` is the catalog-metric drilldown (its series/versions/breakdowns
// power the MonitoringDetailPage tabs). The MetricScopeType members
// schema / distribution / release_regression have no standalone detail page.
export type MonitoringScope = 'project_total' | 'event_type' | 'event' | 'metric'

/**
 * Build the canonical monitoring-detail URL for a catalog metric. Metrics are
 * not a `MetricScopeType` member (anomaly signals never carry them), so they
 * get a dedicated helper instead of overloading {@link getMonitoringPath}.
 */
export function getMetricMonitoringPath(slug: string, metricId: string): string {
  return `/p/${slug}/monitoring/metric/${metricId}`
}

/** Scopes `getMonitoringPath` can route; the rest have no detail page. */
const SCOPES_WITH_MONITORING_ROUTE = new Set<string>([
  'project_total',
  'event_type',
  'event',
  'metric',
])

/**
 * Scopes that must never reach the event fallback below, however well-formed
 * their `event_id` looks.
 *
 * A release regression compares a release COHORT against the previous release.
 * The event monitoring page charts every version over the chart's own range
 * against the seasonal baseline — a different numerator, denominator, window AND
 * estimator — so, in the words of the backend builder that refuses to emit this
 * same link (`_build_monitoring_url`, backend/src/tripl/worker/tasks/metrics/
 * urls.py), it "could not corroborate the alert even in principle". A reader who
 * follows it sees a chart that disagrees with the alert and cannot tell which
 * one is wrong. The backend made that call and the frontend was still sending
 * people there (tripl-oxkt.21) — one decision, made twice, in opposite
 * directions.
 *
 * A deny-set rather than a narrower fallback because a release regression's
 * `scope_ref` IS its event id and its `event_id` is populated, so every cheaper
 * guard would still let it through. The delivery that carried the regression is
 * the only surface holding its actual / expected / % delta, and the incident
 * card links there already; this returns null so no second, contradicting link
 * is offered beside it.
 */
const SCOPES_WITH_NO_SUBSTANTIATING_PAGE = new Set<string>(['release_regression'])

/**
 * Where to look at the thing an alert fired on, or `null` when there is nowhere
 * to look.
 *
 * `getMonitoringPath` throws for the scopes with no detail route, which is right
 * for callers that must not link to a wrong page and wrong for a table cell
 * deciding whether to offer a link at all. This answers that question instead of
 * making every caller wrap it in a try.
 *
 * The event fallback matters for drift scopes: they have no route of their own,
 * but the event they were detected on does. `variable_value_drift` in
 * particular has a NOT NULL `event_id`, and /monitoring/event/:id mounts the
 * panel that names the variable and lists its observed values — that link is
 * the whole reason the fallback exists, so narrow it with the deny-set above
 * rather than removing it.
 */
export function getScopeMonitoringPath(
  slug: string,
  scope: { scope_type: string; scope_ref: string; event_id?: string | null },
): string | null {
  if (SCOPES_WITH_NO_SUBSTANTIATING_PAGE.has(scope.scope_type)) return null
  if (SCOPES_WITH_MONITORING_ROUTE.has(scope.scope_type)) {
    return getMonitoringPath(slug, {
      scope_type: scope.scope_type as MetricScopeType,
      scope_ref: scope.scope_ref,
    })
  }
  if (scope.event_id) return `/p/${slug}/monitoring/event/${scope.event_id}`
  return null
}

/**
 * Scopes whose `scope_ref` is an event_type_id exactly when `event_id` is absent.
 *
 * Only release regressions, and only because their builder says so: a scan with
 * an app version column detects over BOTH partitions on every pass and persists
 * one row per finding — `event_id` set with `event_type_id` NULL for the event
 * partition, the reverse for the event-TYPE one, and `scope_ref` holding
 * whichever id that row is about (worker/tasks/metrics/regression.py). The
 * delivery and the inbox group carry `scope_ref` and `event_id` but no
 * `event_type_id`, so a null `event_id` on one of these IS the event-type
 * partition and its ref is the id /monitoring/event-type/:id wants.
 *
 * Nothing else may join without that same guarantee. Schema and distribution
 * drift also carry an `event_type_id` on the backend row, but their `scope_ref`
 * is a FIELD NAME — reading it as an id here would rebuild the
 * valid-looking-URL-for-a-nonexistent-page defect one scope to the left
 * (tripl-wkwv.12, tripl-oxkt.21).
 */
const SCOPES_WHOSE_REF_IS_AN_EVENT_TYPE = new Set<string>(['release_regression'])

/**
 * Where a caller may send a reader to LOOK at what an alert names, and which
 * monitoring page that is.
 *
 * The page is on the result because the two destinations chart different
 * entities: an event page and an event-type page announced with the same words
 * is the event/event-type conflation this whole area was fenced against
 * (tripl-oxkt.21), so callers must word the link from `scope`.
 */
export interface ScopeNavigationTarget {
  path: string
  scope: 'event' | 'event_type'
}

/**
 * Where to go and LOOK at the entity an alert names, for the scopes
 * {@link getScopeMonitoringPath} has already refused — `null` when there is no
 * such place either.
 *
 * Two different questions, and the deny-set above was being made to answer both
 * (tripl-wkwv.12). `getScopeMonitoringPath` answers "does a page corroborate
 * this alert?" — for a release regression the answer is still no, and nothing
 * here weakens it: the monitoring page charts its entity's volume against the
 * seasonal baseline over that page's own range, a different numerator,
 * denominator, window and estimator, and the By version panel it does mount is
 * keyed by the SCAN and only ever describes the current latest release. This
 * answers "is there anywhere to go and look at the thing this alert names?",
 * which for a quarter of the production inbox had no answer at all — the event
 * name rendered as dead text and the reader had to go find it by hand on the
 * Events page, which is the complaint the linked scope name existed to fix.
 *
 * GUARDED by the COMPLEMENT of {@link getScopeMonitoringPath} rather than by a
 * second copy of the deny-set, and that form is load-bearing: it can never
 * return a path a caller is already linking, so a surface that renders both can
 * never show two links to one page — the exact failure the deny-set's own
 * comment ("no second, contradicting link is offered beside it") exists to
 * prevent. Neither branch below relaxes that guard, so it holds however the
 * deny-set changes.
 *
 * BOTH partitions of a release regression get an answer, and they are different
 * pages — the event-scoped row through its `event_id`, the event-TYPE-scoped one
 * through its `scope_ref` (see the set above). Answering only the first left the
 * event type's name as dead text beside a page that renders it perfectly well,
 * for a reason ("the row carries no event") that is true of the EVENT route and
 * says nothing about the event-type one.
 *
 * Callers must label the result as navigation and not as evidence: it is only
 * ever offered for a scope whose alert the destination cannot substantiate.
 */
export function getScopeNavigationTarget(
  slug: string,
  scope: { scope_type: string; scope_ref: string; event_id?: string | null },
): ScopeNavigationTarget | null {
  if (getScopeMonitoringPath(slug, scope) !== null) return null
  // For the EVENT page, read `event_id` and never `scope_ref`, even though a
  // release regression's scope_ref IS its event id on this partition: on the
  // other one that same field is an event_type_id, and building the event URL
  // out of it is how /monitoring/event/{event_type_id} — a valid-looking link to
  // a page that does not exist — got emitted (tripl-oxkt.21).
  if (scope.event_id) {
    return { path: `/p/${slug}/monitoring/event/${scope.event_id}`, scope: 'event' }
  }
  // No event, but the ref may still be an id a page is mounted on. Gated on the
  // scope type rather than on the ref's shape, because "is this a uuid?" is the
  // guess that produced the wrong route in the first place.
  if (SCOPES_WHOSE_REF_IS_AN_EVENT_TYPE.has(scope.scope_type)) {
    return {
      // Through the shared builder, so the event-type route is spelled in one
      // place and this cannot drift away from what App.tsx mounts.
      path: getMonitoringPath(slug, { scope_type: 'event_type', scope_ref: scope.scope_ref }),
      scope: 'event_type',
    }
  }
  return null
}

export function getMonitoringPath(
  slug: string,
  signal: { scope_type: MetricScopeType; scope_ref: string },
) {
  switch (signal.scope_type) {
    case 'project_total':
      return `/p/${slug}/monitoring/project-total/${signal.scope_ref}`
    case 'event_type':
      return `/p/${slug}/monitoring/event-type/${signal.scope_ref}`
    case 'event':
      return `/p/${slug}/monitoring/event/${signal.scope_ref}`
    case 'metric':
      // Catalog-metric anomalies carry scope_ref = metric_definition_id.
      return getMetricMonitoringPath(slug, signal.scope_ref)
    case 'schema':
    case 'distribution':
    case 'release_regression':
    case 'variable_value_drift':
    case 'source_freshness':
      // These scopes have no entity-level monitoring detail route; routing
      // them to the event URL (the previous silent default) mis-renders an
      // unrelated event. Fail loudly so callers don't link to a wrong page.
      throw new Error(
        `getMonitoringPath: scope_type "${signal.scope_type}" has no monitoring detail route`,
      )
  }
}

export function routeScopeToApiScope(scope: string | undefined): MonitoringScope {
  switch (scope) {
    case 'project-total':
      return 'project_total'
    case 'event-type':
      return 'event_type'
    case 'event':
      return 'event'
    case 'metric':
      return 'metric'
    default:
      throw new Error(`routeScopeToApiScope: unknown monitoring scope "${scope}"`)
  }
}

/**
 * Resolves the API scope for the monitoring detail page from its route params.
 *
 * The legacy `/p/:slug/events/detail/:eventId` route carries no `:scope`
 * segment, so when `scope` is absent but an `eventId` is present we default to
 * the event scope instead of throwing. Truly-unknown scope strings still throw
 * via {@link routeScopeToApiScope}, so a malformed canonical URL fails loudly.
 */
export function resolveDetailScope(
  scope: string | undefined,
  eventId: string | undefined,
): MonitoringScope {
  if (scope === undefined && eventId) return 'event'
  return routeScopeToApiScope(scope)
}

/** Minimal shape a signal needs for its severity label; both
 * {@link MonitoringSignal} and {@link TopMoverItem} satisfy it structurally. */
export interface SignalSeverityInput {
  actual_count: number
  expected_count: number
  z_score: number
  direction: string
}

/**
 * Human-readable severity label for a monitoring-signal cell.
 *
 * A drop that bottomed out at zero has its z-score clamped by the detector, so
 * every such signal reads an identical, low-information `z=-20.0`. The useful
 * fact is that the series went to zero, so surface "dropped to zero" instead.
 * Every other case keeps the numeric `z=X.X` (prefix included, so call sites can
 * render the return value directly without adding their own "z=").
 */
export function formatSignalSeverity(signal: SignalSeverityInput): string {
  if (signal.direction === 'drop' && signal.actual_count === 0) {
    return 'dropped to zero'
  }
  return `z=${signal.z_score.toFixed(1)}`
}

/** What {@link formatSignalEffect} reads; `relative_effect` is the server's. */
export interface SignalEffectInput extends SignalSeverityInput {
  relative_effect?: number | null
}

/**
 * A signal's size as a signed % change from expected: "+203%", "−64%", or
 * "dropped to zero" / "up from zero" where a percentage says nothing (MO-2).
 *
 * The primary severity on every signal row. A z-score means nothing to most
 * readers and "3,846 vs 1,268" makes them do the arithmetic; the event hero
 * already said "+203% vs. baseline", and now the lists say it too.
 *
 * The figure is {@link ratioDelta}, the same unfloored |actual − expected| /
 * |expected| the event hero and the monitoring detail page print, so one
 * signal reads one number everywhere. It used to be {@link relativeEffect},
 * whose count-shaped denominator is floored at 1: expected 0.4 and actual 5
 * read "+460%" in the lists and "+1150%" on the hero. The floor stays where it
 * belongs, in the Significant / Major gate; it only ever shrinks the gated
 * value, so a row listed under "≥50%" still never reads below 50%.
 * `relativeEffect` remains the fallback for a baseline `ratioDelta` refuses.
 */
export function formatSignalEffect(signal: SignalEffectInput): string {
  if (signal.direction === 'drop' && signal.actual_count === 0) return 'dropped to zero'
  if (signal.direction !== 'drop' && signal.expected_count <= 0) return 'up from zero'
  const delta = ratioDelta(signal.actual_count, signal.expected_count)
  const pct = delta === null ? relativeEffect(signal) * 100 : Math.abs(delta)
  // Whole percent from 10% up; one decimal below, where rounding "4.6" to "5"
  // would move a row across a reader's mental threshold.
  const digits = pct >= 10 ? 0 : 1
  const figure = pct.toLocaleString(APP_LOCALE, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  // A real minus sign (U+2212), not a hyphen, so "+" and "−" line up.
  return `${signal.direction === 'drop' ? '\u2212' : '+'}${figure}%`
}

/**
 * The secondary line for {@link formatSignalEffect}: the word bucket and the
 * z-score, for a tooltip or a screen reader — "Major · z=40.7" (JR-31).
 */
export function formatSignalEffectDetail(signal: SignalEffectInput): string {
  return `${signalMagnitudeWord(signal)} · z=${signal.z_score.toFixed(1)}`
}
