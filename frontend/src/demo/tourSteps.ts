/**
 * Capability-aware product tour model.
 *
 * A concise guided path through the product's core surfaces. Each step links to
 * the REAL surface (deep link, slug-parameterised) so the tour is a launchpad,
 * not a slideshow. Kept as pure data so the ordering, coverage and links are
 * unit-testable without rendering.
 */

import { buildNavGroups, currentOrgSlug, projectPath } from '@/lib/navigation'

export interface TourStep {
  id: string
  /**
   * The sidebar group this surface sits in — Plan, Observe or Govern — read
   * from `buildNavGroups` itself, never typed here (#251 JR-22): the typed
   * copy said Govern for Plan branches and Alerting and drifted every time the
   * sidebar moved an item. Null for a step with no sidebar item (search lives
   * in the command palette), which prints no chip rather than a wrong one.
   */
  area: string | null
  /** The sidebar label, so "Open X" names the page the sidebar calls X. */
  title: string
  blurb: string
  /** Deep link to the real surface for this step. */
  to: string
  /**
   * What the step's button does instead of following `to` (DEMO-18). The
   * search step's surface is the command palette, which has no URL: linking to
   * the Overview the user is usually already on just closed the tour.
   */
  action?: 'open-command-palette'
}

/**
 * The metric building blocks a newcomer must be able to reach directly from the
 * welcome flow. Each is a REAL deep link — the three catalog kinds
 * (fact / sql / event_composition) open the catalog already filtered to that
 * kind, event volume opens the Events catalog where the per-event series lives,
 * and fact tables open their own tab. They previously all pointed at a bare
 * /metrics, so the links existed but discovered nothing.
 *
 * Note "the four metric kinds" in the original acceptance is a miscount: the
 * backend MetricKind enum has three members. Event volume is a scan-collected
 * per-event series, not a MetricDefinition kind.
 */
export interface MetricBuildingBlock {
  id: string
  label: string
  blurb: string
  to: string
}

export function buildMetricBuildingBlocks(slug: string): MetricBuildingBlock[] {
  const metrics = projectPath(currentOrgSlug(), slug, '/metrics')
  return [
    {
      // Event volume is NOT a catalog metric kind — MetricKind is only
      // fact/sql/event_composition. It is the per-event volume series a scan
      // collects, and it lives on the Events catalog, so that is where this
      // block points. Sending it to /metrics (as it used to) dropped the user on
      // a page that does not contain the thing being described.
      id: 'event-count',
      label: 'Event volume',
      blurb: 'How often each event fires over time — collected per event by a scan.',
      to: projectPath(currentOrgSlug(), slug, '/events'),
    },
    {
      id: 'fact',
      label: 'Fact',
      blurb: 'Aggregate a numeric column from a fact table.',
      to: `${metrics}?kind=fact`,
    },
    {
      id: 'sql',
      label: 'SQL',
      blurb: 'A metric defined by a custom SQL query.',
      to: `${metrics}?kind=sql`,
    },
    {
      id: 'event_composition',
      label: 'Event composition',
      blurb: 'Ratios and per-user metrics composed from events.',
      to: `${metrics}?kind=event_composition`,
    },
    {
      id: 'fact-tables',
      label: 'Fact tables',
      blurb: 'The warehouse tables that fact & SQL metrics read from.',
      to: `${metrics}/fact-tables`,
    },
  ]
}

/** A step as written here: which sidebar item it is, and what it says. */
interface TourStepSpec {
  id: string
  /** The `buildNavGroups` item id the step's area and title come from. */
  navId: string | null
  /** Only for a step that is a section of a page, not the page (Alert rules). */
  title?: string
  blurb: string
  to: string
  action?: TourStep['action']
}

export function buildTourSteps(slug: string): [TourStep, ...TourStep[]] {
  const base = projectPath(currentOrgSlug(), slug)
  const nav = new Map(
    buildNavGroups(slug, undefined).flatMap((group) =>
      group.items.map((item) => [item.id, { area: group.label, label: item.label }] as const),
    ),
  )
  const resolve = (spec: TourStepSpec): TourStep => {
    const item = spec.navId ? nav.get(spec.navId) : undefined
    return {
      id: spec.id,
      area: item?.area ?? null,
      title: spec.title ?? item?.label ?? spec.id,
      blurb: spec.blurb,
      to: spec.to,
      ...(spec.action ? { action: spec.action } : {}),
    }
  }
  const [first, ...rest] = tourStepSpecs(base)
  return [resolve(first), ...rest.map(resolve)]
}

function tourStepSpecs(base: string): [TourStepSpec, ...TourStepSpec[]] {
  return [
    {
      id: 'events',
      navId: 'events',
      blurb: 'The catalog of events you track and their implementation status.',
      to: `${base}/events`,
    },
    {
      id: 'scans',
      navId: 'scans',
      // A scan is not "pull volume to learn a baseline": that describes only the
      // scheduled metrics collection of a monitoring scan. What EVERY scan does
      // is fill the tracking plan.
      blurb:
        'Read your warehouse into the tracking plan. Catalog + monitoring also records metric points on a schedule.',
      to: `${base}/scans`,
    },
    {
      // The step id keeps its old name so saved tour progress still matches.
      id: 'live-activity',
      navId: 'overview',
      // "as scans ... run" counted an execution as a scan, the same slip the
      // activity rail's burst summary made. An execution is a *run*.
      blurb: 'The Overview updates live as scan runs and metric collection land.',
      to: `${base}/overview`,
    },
    {
      id: 'metrics',
      navId: 'metrics',
      blurb: 'Event-volume, fact, SQL and event-composition metrics, plus the fact tables behind them.',
      to: `${base}/metrics`,
    },
    {
      id: 'monitors',
      // A section of Alerting, not a page of its own: it takes Alerting's
      // group, and its own name — one name for the object: alert rule (#238 JR-28).
      navId: 'alerting',
      title: 'Alert rules',
      blurb: 'The rules that decide which spikes and drops are worth notifying about, and their live state.',
      // The section, not the standalone page: that page rendered these same
      // rules under a second noun and was merged in. `/monitors`
      // would still resolve, but only through a redirect.
      to: `${base}/alerting?section=monitors`,
    },
    {
      id: 'anomalies',
      navId: 'anomalies',
      blurb: 'Detected anomalies across the project with severity and direction.',
      to: `${base}/anomalies`,
    },
    {
      id: 'coverage',
      navId: 'coverage',
      // What the page measures: plan implementation, not which platforms
      // report (#251 JR-22).
      blurb: 'How much of your plan is implemented, and which implemented events went silent.',
      to: `${base}/coverage`,
    },
    {
      id: 'reconciliation',
      navId: 'reconciliation',
      blurb: 'Which planned events are actually arriving vs the plan.',
      to: `${base}/reconciliation`,
    },
    {
      id: 'branches',
      navId: 'branches',
      blurb: 'Propose and review changes to the tracking plan on a branch.',
      to: `${base}/branches`,
    },
    {
      id: 'alerting',
      navId: 'alerting',
      blurb: 'Route anomalies to destinations and preview a simulated firing.',
      to: `${base}/alerting`,
    },
    {
      id: 'search',
      // The command palette, on every page: no sidebar item, so no group.
      navId: null,
      title: 'Search by meaning',
      blurb:
        'Press Ctrl K (or ⌘K) and try "purchase funnel" or "money back" — semantic matches are marked.',
      to: `${base}/overview`,
      action: 'open-command-palette',
    },
  ]
}
