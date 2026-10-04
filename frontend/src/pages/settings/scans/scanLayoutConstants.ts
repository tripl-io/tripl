import type { ChipTone } from '@/components/primitives/chip'
import type { DotTone } from '@/components/primitives/dot'
import { SCAN_RUN_STATUS } from '@/lib/statusLexicon'

// ─── Interval labels (long form). Picker codes stay 15m / 1h / 6h / 1d / 1w. ───
export const INTERVAL_LABEL: Record<string, string> = {
  '15m': 'Every 15 min',
  '1h': 'Every hour',
  '6h': 'Every 6 hours',
  '1d': 'Every day',
  '1w': 'Every week',
}

// Status derived from the latest job. The real ScanConfig has no status field, so
// callers map their job state into this canonical set.
// `unknown` covers the window before a scan's job query resolves — the row has
// no verdict yet, so it must not claim "Never run".
export type ScanStatus = 'ok' | 'running' | 'failed' | 'idle' | 'unknown'

// Colour only. The WORD comes from SCAN_STATUS_LABEL below, so a scan's status
// cannot be phrased two different ways on the same screen.
export const STATUS_META: Record<ScanStatus, { tone: DotTone; chip: ChipTone }> = {
  ok: { tone: 'success', chip: 'success' },
  running: { tone: 'info', chip: 'info' },
  failed: { tone: 'danger', chip: 'danger' },
  idle: { tone: 'neutral', chip: 'neutral' },
  unknown: { tone: 'neutral', chip: 'neutral' },
}

/**
 * The word for a scan's derived status, read from SCAN_RUN_STATUS — the same
 * table the run pill directly underneath the header uses.
 *
 * STATUS_META used to carry its own labels, so a scan whose latest run had
 * succeeded read "Healthy" in the detail header and "Succeeded" in the row two
 * lines below it (D3(c)). Worse, "Healthy" is the Monitors lexeme
 * (statusLexicon.ts MONITOR_STATUS): statusLexicon's own notes record the
 * decision that monitor words must not leak onto non-monitor surfaces, because
 * a firing/healthy verdict implies an alert rule that a scan does not have.
 */
export const SCAN_STATUS_LABEL: Record<ScanStatus, string> = {
  ok: SCAN_RUN_STATUS.succeeded.label,
  running: SCAN_RUN_STATUS.running.label,
  failed: SCAN_RUN_STATUS.failed.label,
  idle: SCAN_RUN_STATUS.never.label,
  // Not a lexeme: `unknown` is the absence of a verdict while the job query is
  // still in flight, not a run state the backend can report.
  unknown: 'Loading…',
}

const COUNT_UNITS = [
  { divisor: 1e3, suffix: 'K', digits: 1 },
  { divisor: 1e6, suffix: 'M', digits: 2 },
  { divisor: 1e9, suffix: 'B', digits: 2 },
] as const

// Format a row/count number compactly (e.g. 1.8M) to mirror the mockup's fmtS.
// The unit is chosen AFTER rounding: 999,950 rounds to 1000.0 thousands, which
// used to print as "1000K" instead of moving up to "1M" (DATA-38).
export function formatCount(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value < 1e3) return String(value)
  for (const [index, { divisor, suffix, digits }] of COUNT_UNITS.entries()) {
    const isLast = index === COUNT_UNITS.length - 1
    const rounded = (value / divisor).toFixed(digits)
    if (!isLast && Number(rounded) >= 1000) continue
    return `${rounded.replace(/\.?0+$/, '')}${suffix}`
  }
  return String(value)
}
