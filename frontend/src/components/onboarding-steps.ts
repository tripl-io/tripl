/**
 * The getting-started steps, as plain data (#250 JR-2 / JR-3 / JR-34).
 *
 * Kept out of `onboarding-checklist.tsx` so that file only exports components
 * (react-refresh/only-export-components), and so the workspace project cards
 * and the "Back to checklist" bar can read the same steps and progress the
 * checklist shows instead of a second, drifting copy.
 */

import type { ProjectSummary } from '@/types'
import { hasExecutedScanJob } from '@/components/onboarding-utils'
import { currentOrgSlug, projectPath, settingsPath, stripOrgPrefix } from '@/lib/navigation'

export type OnboardingStepId = 'source' | 'scan' | 'review' | 'metric' | 'alert'

export interface OnboardingStep {
  id: OnboardingStepId
  title: string
  hint: string
  href: string
  done: boolean
  /**
   * Actionable only by an owner (e.g. connecting a data source). For a non-owner
   * such a step is shown but never counted toward progress, so it can't block
   * the checklist from completing (tripl-yfsj.4).
   */
  ownerOnly?: boolean
  /** Hint shown to a non-owner in place of `hint` on an owner-only step. */
  ownerHint?: string
  /** A secondary way through the step, shown under the hint. */
  alternative?: { label: string; href: string }
}

/**
 * The fastest path first (JR-2): a newcomer with a warehouse connects it, a
 * scan imports the plan, they review what came in, then define the metric
 * that detection and alerts are built on. Adding events by hand is the
 * alternative on the review step, for someone with no warehouse yet.
 */
export const ONBOARDING_STEP_ORDER: readonly OnboardingStepId[] = [
  'source',
  'scan',
  'review',
  'metric',
  'alert',
]

export const ONBOARDING_STEP_TITLES: Readonly<Record<OnboardingStepId, string>> = {
  source: 'Connect a data source',
  scan: 'Run a catalog + monitoring scan',
  review: 'Review imported events',
  metric: 'Define a key metric',
  alert: 'Set up alerting',
}

/** Query keys the step links carry, read by the "Back to checklist" bar (JR-3). */
export const ONBOARDING_STEP_PARAM = 'onboarding'
export const ONBOARDING_PROJECT_PARAM = 'project'
const ONBOARDING_POSITION_PARAM = 'step'

function isStepId(value: string | null): value is OnboardingStepId {
  return value != null && (ONBOARDING_STEP_ORDER as readonly string[]).includes(value)
}

/**
 * A step's link, tagged so the page it opens can show where the reader came
 * from: the step, its place in the list the reader saw ("2-of-5"), and — for
 * links outside the project, such as the data-sources settings — the project
 * slug, since their URL does not carry it.
 */
export function onboardingStepHref(
  path: string,
  step: OnboardingStepId,
  slug: string,
  position?: { number: number; total: number },
): string {
  const params = new URLSearchParams({ [ONBOARDING_STEP_PARAM]: step })
  if (position) params.set(ONBOARDING_POSITION_PARAM, `${position.number}-of-${position.total}`)
  if (!path.startsWith(projectPath(currentOrgSlug(), slug, '/'))) params.set(ONBOARDING_PROJECT_PARAM, slug)
  return `${path}?${params.toString()}`
}

function parsePosition(raw: string | null): { number: number; total: number } | null {
  const match = raw ? /^(\d+)-of-(\d+)$/.exec(raw) : null
  if (!match) return null
  const number = Number(match[1])
  const total = Number(match[2])
  return number >= 1 && number <= total ? { number, total } : null
}

/** Where a step link landed: which step, its number, and whose checklist. */
export interface OnboardingReturn {
  step: OnboardingStepId
  number: number
  total: number
  title: string
  slug: string
}

/** Reads a step link's tags back off the current location, or null. */
export function parseOnboardingReturn(pathname: string, search: string): OnboardingReturn | null {
  const params = new URLSearchParams(search)
  const step = params.get(ONBOARDING_STEP_PARAM)
  if (!isStepId(step)) return null
  const slug = params.get(ONBOARDING_PROJECT_PARAM) ?? /^\/p\/([^/]+)/.exec(stripOrgPrefix(pathname))?.[1] ?? null
  if (!slug) return null
  const position = parsePosition(params.get(ONBOARDING_POSITION_PARAM)) ?? {
    number: ONBOARDING_STEP_ORDER.indexOf(step) + 1,
    total: ONBOARDING_STEP_ORDER.length,
  }
  return { step, ...position, title: ONBOARDING_STEP_TITLES[step], slug }
}

/**
 * A caller that knows the metric count passes it; otherwise the project
 * summary's `metric_count` answers (JR-2). With neither — a summary built
 * before the field — the metric step is left out rather than shown as a step
 * that can never tick off.
 */
function knownMetricCount(summary: ProjectSummary, metricCount: number | undefined): number | undefined {
  if (metricCount !== undefined) return metricCount
  return typeof summary.metric_count === 'number' ? summary.metric_count : undefined
}

/**
 * Derive the core-loop steps with auto-computed done-state.
 *
 * "Review imported events" ticks once at least one event has left the review
 * queue: a scan imports everything into review, so an accepted event is the
 * sign the reader has looked at what came in. Events added by hand never enter
 * the queue, so the by-hand path ticks it too. It used to tick on coverage
 * (`implemented_event_count`), which says data arrives, not that anyone
 * reviewed anything (JR-2).
 */
export function buildOnboardingSteps(
  slug: string,
  summary: ProjectSummary,
  sourceCount: number,
  metricCount?: number,
): OnboardingStep[] {
  const base = projectPath(currentOrgSlug(), slug)
  const metrics = knownMetricCount(summary, metricCount)
  const steps: OnboardingStep[] = [
    {
      id: 'source',
      title: ONBOARDING_STEP_TITLES.source,
      hint: 'Point tripl at the warehouse or database that holds your events.',
      href: settingsPath('/settings/data-sources'),
      // `sourceCount` is already the count of REAL (non-synthetic) sources — a
      // demo's synthetic warehouse is excluded by the caller (countRealSources).
      done: sourceCount > 0,
      // Data-source creation is owner-only (DataSourcesPage: `canManageDataSources
      // = user?.role === 'owner'`). An editor can't action this step, so for a
      // non-owner it is shown-but-non-blocking rather than a dead-end.
      ownerOnly: true,
      ownerHint: 'Managed by owners — ask an owner to connect one.',
    },
    {
      id: 'scan',
      title: ONBOARDING_STEP_TITLES.scan,
      // A run writes events and fields — never a metric point. Volume arrives
      // later, from the scheduled collection a Catalog + monitoring scan gets,
      // so the hint promises the import now and the tracking after (tripl-3y7z).
      hint: 'Imports your events and fields. Catalog + monitoring also starts tracking their volume.',
      href: `${base}/scans`,
      // A seeded ScanConfig (scan_count > 0) does NOT count — require an
      // actually-executed job.
      done: hasExecutedScanJob(summary),
    },
    {
      id: 'review',
      title: ONBOARDING_STEP_TITLES.review,
      hint: 'Accept or fix what the scan found before it becomes your plan.',
      href: `${base}/events/review`,
      done: summary.active_event_count > summary.review_pending_event_count,
      alternative: {
        label: 'No warehouse yet? Add events by hand.',
        href: `${base}/events`,
      },
    },
  ]
  if (metrics !== undefined) {
    steps.push({
      id: 'metric',
      title: ONBOARDING_STEP_TITLES.metric,
      hint: 'Anomaly detection and alerts watch metrics, so start with the one you care about most.',
      href: `${base}/metrics/new`,
      done: metrics > 0,
    })
  }
  steps.push({
    id: 'alert',
    title: ONBOARDING_STEP_TITLES.alert,
    // A destination is only half of it: the RULE is what routes a signal to
    // that destination. Saying "add a destination" taught the wrong mental
    // model and matched a done-check that ticked on the destination alone.
    hint: 'Add a destination, then a rule — the rule is what routes anomalies to it.',
    href: `${base}/alerting`,
    // Both halves, because a destination with no enabled rule delivers
    // nothing: ticking this off on the destination alone let a user stop
    // half-way and read 5 of 5 while no anomaly could reach anyone
    // (tripl-jfm3.81). `alert_rule_count` counts ENABLED rules only.
    done: summary.alert_destination_count > 0 && summary.alert_rule_count > 0,
  })
  // Tag every link with its step and place, so the page it opens can say
  // "Step 2 of 5" and lead back to the checklist (JR-3).
  return steps.map((step, index) => {
    const position = { number: index + 1, total: steps.length }
    return {
      ...step,
      href: onboardingStepHref(step.href, step.id, slug, position),
      alternative: step.alternative && {
        ...step.alternative,
        href: onboardingStepHref(step.alternative.href, step.id, slug, position),
      },
    }
  })
}

/** Progress as the checklist counts it for this role (owner-only steps drop out for others). */
export interface OnboardingProgress {
  completed: number
  total: number
  /** The first step this role can take that is not done yet. */
  next: OnboardingStep | null
}

export function onboardingProgress(steps: readonly OnboardingStep[], isOwner: boolean): OnboardingProgress {
  const counted = steps.filter((step) => isOwner || !step.ownerOnly)
  const remaining = counted.filter((step) => !step.done)
  return {
    completed: counted.length - remaining.length,
    total: counted.length,
    next: remaining[0] ?? null,
  }
}
