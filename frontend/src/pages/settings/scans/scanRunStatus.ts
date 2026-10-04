import type { RunPillStatus, ScanJob } from '@/types'

// Canonical status for the run-status pill. Kept text-first: every state carries
// a written label and a distinct icon shape, so the cue never relies on hue
// alone (colorblind- and screen-reader-safe). Lives in its own module so the
// component files that render the pill stay component-only (react-refresh).
// The type itself is in types/scans.ts, so lib/ can use it without importing
// this page module; re-exported for the existing callers.
export type { RunPillStatus }

const JOB_STATUS_TO_PILL: Record<ScanJob['status'], RunPillStatus> = {
  pending: 'pending',
  running: 'running',
  completed: 'succeeded',
  failed: 'failed',
  cancelled: 'cancelled',
}

// Map a raw scan-job status onto the canonical pill status.
export function runPillStatus(status: ScanJob['status']): RunPillStatus {
  return JOB_STATUS_TO_PILL[status]
}
