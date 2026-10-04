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
 * "Connect a data source" must mean a REAL source — the demo's synthetic,
 * in-memory warehouse doesn't count. Excludes `is_synthetic` sources so a demo
 * never auto-completes the connect-a-real-source step.
 */
export function countRealSources(
  sources: ReadonlyArray<{ is_synthetic?: boolean }>,
): number {
  return sources.filter((source) => !source.is_synthetic).length
}
