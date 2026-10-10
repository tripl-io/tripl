/**
 * Presentation helpers for the plan health score (F15, #268): grade tones and
 * labels, the numbers the badge and the breakdown print. No React, so the
 * catalog, the event page, the event types list and the Overview agree on
 * every word and every rounding.
 */
import type { ChipTone } from '@/components/primitives/chip-variants'
import type {
  ComponentAverage,
  EventHealth,
  HealthComponent,
  HealthComponentKey,
  HealthGrade,
  ProjectHealthTrendPoint,
} from '@/types/health'
import { countOf } from '@/lib/plural'

/** The backend's thresholds (health_weights.py), for a score without a grade. */
export const GRADE_HEALTHY_MIN = 80
export const GRADE_WARNING_MIN = 50

/** The fixed order the server sends the components in. */
export const HEALTH_COMPONENT_ORDER: readonly HealthComponentKey[] = [
  'implemented_seen',
  'contract',
  'drifts',
  'signals',
  'freshness',
  'documentation',
]

/** Fallback labels, used where only a key is known (component averages). */
export const HEALTH_COMPONENT_LABELS: Record<HealthComponentKey, string> = {
  implemented_seen: 'Implemented & seen',
  contract: 'Contract',
  drifts: 'Drifts',
  signals: 'Signals',
  freshness: 'Freshness',
  documentation: 'Documentation',
}

export const HEALTH_GRADE_TONE: Record<HealthGrade, ChipTone> = {
  healthy: 'success',
  warning: 'warning',
  unhealthy: 'danger',
}

export const HEALTH_GRADE_LABEL: Record<HealthGrade, string> = {
  healthy: 'Healthy',
  warning: 'Needs attention',
  unhealthy: 'Unhealthy',
}

/** The CSS colour token of a grade, for bars and figures outside a Chip. */
export const HEALTH_GRADE_COLOR: Record<HealthGrade, string> = {
  healthy: 'var(--success)',
  warning: 'var(--warning)',
  unhealthy: 'var(--danger)',
}

/** The grade a score falls in, by the backend's thresholds. */
export function gradeForScore(score: number): HealthGrade {
  // Grade the figure the badge shows, so "50" is never coloured as below 50.
  const shown = Math.round(score)
  if (shown >= GRADE_HEALTHY_MIN) return 'healthy'
  if (shown >= GRADE_WARNING_MIN) return 'warning'
  return 'unhealthy'
}

/** What a screen reader hears for a badge: "Health 72 of 100". */
export function healthAriaLabel(score: number): string {
  return `Health ${Math.round(score)} of 100`
}

/** A component value (0..1) as a whole percent; "—" when excluded. */
export function formatHealthValue(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${Math.round(clamp01(value) * 100)}%`
}

/** A weight or effective weight in percent, at most one decimal: "20%", "23.5%". */
export function formatHealthWeight(weight: number | null | undefined): string {
  if (weight === null || weight === undefined || !Number.isFinite(weight)) return '—'
  return `${trimDecimal(weight)}%`
}

/** Points out of 100, at most one decimal: "18.5". */
export function formatHealthPoints(points: number | null | undefined): string {
  if (points === null || points === undefined || !Number.isFinite(points)) return '—'
  return trimDecimal(points)
}

/**
 * The change against the previous snapshot: "+3", "−3" (a real minus sign),
 * "±0"; null when either side is missing.
 */
export function formatHealthDelta(
  score: number | null | undefined,
  previous: number | null | undefined,
): string | null {
  if (score === null || score === undefined || previous === null || previous === undefined) return null
  const delta = Math.round(score - previous)
  if (delta === 0) return '±0'
  return delta > 0 ? `+${delta}` : `−${Math.abs(delta)}`
}

/** The tone of a delta: a drop is danger, a rise success, none neutral. */
export function healthDeltaTone(
  score: number | null | undefined,
  previous: number | null | undefined,
): ChipTone {
  if (score === null || score === undefined || previous === null || previous === undefined) return 'neutral'
  const delta = Math.round(score - previous)
  if (delta < 0) return 'danger'
  if (delta > 0) return 'success'
  return 'neutral'
}

/** The breakdown's footnote when some components did not count. */
export function renormalizedFootnote(applicableCount: number): string {
  return `Weights renormalized over ${countOf(applicableCount, 'applicable component', 'applicable components')}`
}

/** The six components in the fixed order, whatever order they arrived in. */
export function orderedComponents(health: Pick<EventHealth, 'components'>): HealthComponent[] {
  const components = health.components ?? []
  const rank = (key: HealthComponentKey) => {
    const index = HEALTH_COMPONENT_ORDER.indexOf(key)
    return index < 0 ? HEALTH_COMPONENT_ORDER.length : index
  }
  return [...components].sort((a, b) => rank(a.key) - rank(b.key))
}

/** Component averages in the fixed order. */
export function orderedAverages(averages: readonly ComponentAverage[] | undefined): ComponentAverage[] {
  const rank = (key: HealthComponentKey) => {
    const index = HEALTH_COMPONENT_ORDER.indexOf(key)
    return index < 0 ? HEALTH_COMPONENT_ORDER.length : index
  }
  return [...(averages ?? [])].sort((a, b) => rank(a.key) - rank(b.key))
}

/** Splits ids into requests the batch endpoint accepts (HEALTH_BATCH_MAX_IDS per call). */
export function chunkIds(ids: readonly string[], size: number): string[][] {
  if (size < 1) throw new Error('chunk size must be at least 1')
  const chunks: string[][] = []
  for (let start = 0; start < ids.length; start += size) {
    chunks.push(ids.slice(start, start + size))
  }
  return chunks
}

/** The trend window the Overview's Plan health panel asks for and names. */
export const PLAN_HEALTH_TREND_DAYS = 30

/** The scores of the trend's days that have one, oldest first. */
export function trendScores(trend: readonly ProjectHealthTrendPoint[] | undefined): number[] {
  return (trend ?? [])
    .map((point) => point.score)
    .filter((score): score is number => typeof score === 'number')
}

/** What the sparkline says in words. */
export function trendLabel(scores: readonly number[], days: number): string {
  if (scores.length === 0) return `No plan health history in the last ${days} days`
  const first = scores[0]
  const last = scores[scores.length - 1]
  return `Plan health over the last ${days} days: from ${first} to ${last} of 100`
}

/** The server answered with an event health payload, not something else. */
export function isEventHealth(value: unknown): value is EventHealth {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<EventHealth>
  return typeof candidate.score === 'number' && Array.isArray(candidate.components)
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}

function trimDecimal(value: number): string {
  const rounded = Math.round(value * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}
