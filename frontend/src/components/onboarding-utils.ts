/**
 * Pure done-state helpers for the onboarding checklist.
 *
 * Kept out of `onboarding-checklist.tsx` so that file only exports a component
 * (react-refresh/only-export-components) while these stay independently
 * unit-testable.
 */

import type { ProjectSummary } from '@/types'

/**
 * "Run a scan" must reflect a REAL, executed job — not a merely-seeded config.
 * A demo (or any project) can have `scan_count > 0` from a seeded ScanConfig
 * that never ran; that must not tick the step. A `latest_scan_job` row only
 * exists once a scan was actually triggered, so we require one that has left the
 * `pending` (queued) state.
 */
export function hasExecutedScanJob(summary: ProjectSummary): boolean {
  const job = summary.latest_scan_job
  return job != null && job.status !== 'pending'
}

/**
 * Whether a source is a demo's own warehouse rather than a connection someone
 * configured: today's `demo` sources and an older demo's `synthetic` one. The
 * server flags both `is_synthetic`; the type is checked too, so a server that
 * forgot the flag on a new demo type still cannot count it as real.
 */
export function isDemoSource(source: { is_synthetic?: boolean; db_type?: string }): boolean {
  return source.is_synthetic === true || source.db_type === 'demo' || source.db_type === 'synthetic'
}

/**
 * "Connect a data source" must mean a REAL source — a demo's warehouse doesn't
 * count, so a demo never auto-completes the connect-a-real-source step.
 */
export function countRealSources(
  sources: ReadonlyArray<{ is_synthetic?: boolean; db_type?: string }>,
): number {
  return sources.filter((source) => !isDemoSource(source)).length
}
