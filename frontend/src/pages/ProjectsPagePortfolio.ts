import type { Project } from '@/types'

/**
 * Pure roll-ups behind the workspace page. They lived inline in the
 * page body and re-ran on every render; the page now memoises one call.
 */

export type StatTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

/** The strong and the soft colour of each tone, as CSS custom properties. */
export const TONE_VARS: Readonly<Record<StatTone, { color: string; soft: string }>> = {
  success: { color: 'var(--success)', soft: 'var(--success-soft)' },
  warning: { color: 'var(--warning)', soft: 'var(--warning-soft)' },
  danger: { color: 'var(--danger)', soft: 'var(--danger-soft)' },
  info: { color: 'var(--info)', soft: 'var(--info-soft)' },
  neutral: { color: 'var(--fg-subtle)', soft: 'var(--surface-hover)' },
}

export type PortfolioTotals = {
  activeEventCount: number
  alertDestinationCount: number
  eventCount: number
  implementedEventCount: number
  monitoringSignalCount: number
  projectCount: number
  reviewPendingEventCount: number
  scanCount: number
  variableCount: number
}

export type Portfolio = {
  /** Projects, most recently updated first. A new array; the input is untouched. */
  projects: Project[]
  totals: PortfolioTotals
  projectsWithScans: number
  projectsWithSignals: number
  projectsWithLatestScanJob: number
  projectsWithRunningScan: number
  /**
   * Projects with ANY scan config whose LATEST run failed — NOT just the single
   * newest job across the project. A config that fails every hourly run is
   * invisible in latest_scan_job once a different config logs a newer success,
   * so the rollup follows the per-config failing_scan_config_count instead.
   */
  projectsWithFailedScan: number
  failingScanConfigCount: number
}

export function summarizePortfolio(input: readonly Project[]): Portfolio {
  const projects = [...input].sort(
    (left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at),
  )
  const totals: PortfolioTotals = {
    activeEventCount: 0,
    alertDestinationCount: 0,
    eventCount: 0,
    implementedEventCount: 0,
    monitoringSignalCount: 0,
    projectCount: 0,
    reviewPendingEventCount: 0,
    scanCount: 0,
    variableCount: 0,
  }
  let projectsWithScans = 0
  let projectsWithSignals = 0
  let projectsWithLatestScanJob = 0
  let projectsWithRunningScan = 0
  let projectsWithFailedScan = 0
  let failingScanConfigCount = 0
  for (const { summary } of projects) {
    totals.activeEventCount += summary.active_event_count
    totals.alertDestinationCount += summary.alert_destination_count
    totals.eventCount += summary.event_count
    totals.implementedEventCount += summary.implemented_event_count
    totals.monitoringSignalCount += summary.monitoring_signal_count
    totals.projectCount += 1
    totals.reviewPendingEventCount += summary.review_pending_event_count
    totals.scanCount += summary.scan_count
    totals.variableCount += summary.variable_count
    if (summary.scan_count > 0) projectsWithScans += 1
    if (summary.monitoring_signal_count > 0) projectsWithSignals += 1
    if (summary.latest_scan_job != null) projectsWithLatestScanJob += 1
    if (summary.latest_scan_job?.status === 'running') projectsWithRunningScan += 1
    const failing = summary.failing_scan_config_count ?? 0
    if (failing > 0) projectsWithFailedScan += 1
    failingScanConfigCount += failing
  }
  return {
    projects,
    totals,
    projectsWithScans,
    projectsWithSignals,
    projectsWithLatestScanJob,
    projectsWithRunningScan,
    projectsWithFailedScan,
    failingScanConfigCount,
  }
}

/** How many projects the Review-queue hint names before it summarises the rest. */
const REVIEW_HINT_PROJECT_LIMIT = 3

/**
 * Where the workspace review backlog actually sits, biggest queue first.
 *
 * "across 3 projects" told the operator nothing about which project holds the
 * 1441 of the 2292 events, and the tile's single link opened a different one.
 */
export function reviewQueueHint(projects: readonly Project[]): string {
  const pending = projects
    .filter((project) => project.summary.review_pending_event_count > 0)
    .sort(
      (left, right) =>
        right.summary.review_pending_event_count - left.summary.review_pending_event_count,
    )
  if (pending.length === 0) return 'No pending event reviews'
  const named = pending
    .slice(0, REVIEW_HINT_PROJECT_LIMIT)
    .map((project) => `${project.summary.review_pending_event_count} in ${project.name}`)
    .join(' · ')
  const remaining = pending.length - REVIEW_HINT_PROJECT_LIMIT
  return remaining > 0 ? `${named} · +${remaining} more` : named
}
