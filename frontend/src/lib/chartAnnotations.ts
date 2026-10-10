/**
 * Display rules for chart annotations (deploy / release / incident markers).
 *
 * Red belongs to anomalies. The backend's column default is `#ef4444`, the same
 * hue as the anomaly dots, so every marker created without a colour — which was
 * every marker, the form never sent one — drew as a red dashed line beside the
 * red anomaly points it was meant to explain.
 */

import { formatTimestamp, formatUtcOffset } from '@/lib/datetime'
import { APP_LOCALE } from '@/lib/format'
import type { ChartAnnotation, ChartAnnotationSource, PlannedEvent } from '@/types'

/** What the annotation form now sends: a theme token, so dark mode follows. */
export const ANNOTATION_DEFAULT_COLOR = 'var(--info)'

/** The backend column default; rows stored with it were never given a colour. */
const LEGACY_DEFAULT_COLOR = '#ef4444'

/** The colour to draw an annotation in: never the anomaly red by default. */
export function annotationDisplayColor(color: string | null | undefined): string {
  if (!color || color.toLowerCase() === LEGACY_DEFAULT_COLOR) return ANNOTATION_DEFAULT_COLOR
  return color
}

/**
 * The ink of a release or API marker (#256): muted, so the worker's "Release
 * 1.4.0" lines and a deploy script's markers sit behind the markers people
 * placed by hand instead of competing with them. Whatever colour the row
 * stores is ignored for these.
 */
export const AUTOMATIC_ANNOTATION_COLOR = 'var(--fg-subtle)'

/** True for a marker nobody placed by hand: a release or an API annotation. */
export function isAutomaticAnnotation(annotation: Pick<ChartAnnotation, 'source'>): boolean {
  return annotation.source === 'release' || annotation.source === 'api'
}

/** The colour to draw a marker in, by who made it. */
export function annotationMarkerColor(annotation: Pick<ChartAnnotation, 'source' | 'color'>): string {
  return isAutomaticAnnotation(annotation)
    ? AUTOMATIC_ANNOTATION_COLOR
    : annotationDisplayColor(annotation.color)
}

/** How a source reads in the list and in a marker's tooltip. */
export function annotationSourceLabel(source: ChartAnnotationSource): string {
  switch (source) {
    case 'release':
      return 'Release'
    case 'api':
      return 'API'
    default:
      return 'Manual'
  }
}

/**
 * The annotation's link when it is safe to open: http or https only. The API
 * validates this already; a `javascript:` URL must still never reach an href.
 */
export function safeAnnotationUrl(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

/** The backend's `label` cap (schemas/chart_annotation.py). */
export const ANNOTATION_LABEL_MAX = 200

/** How much of a label fits above a chart before neighbouring markers overlap. */
const CHART_LABEL_MAX = 24

/** A label short enough to draw at the top of a chart; the list keeps it whole. */
export function truncateAnnotationLabel(label: string): string {
  return label.length > CHART_LABEL_MAX ? `${label.slice(0, CHART_LABEL_MAX - 1)}…` : label
}

const UTC_DAY: Intl.DateTimeFormatOptions = {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  timeZone: 'UTC',
}

/**
 * An expected window's span as the lists print it. A holiday is a UTC
 * calendar day (the calendar writes `[00:00Z, next 00:00Z)`), so it reads as
 * that day — "Oct 3, 2026, all day UTC" — rather than "3:00 AM – 3:00 AM" for
 * a reader east of Greenwich. Any other window is in local time, as every
 * instant the app prints, with the offset named: the holidays beside it say
 * UTC, so a local time there says which zone it is in too.
 */
export function formatExpectedWindowSpan(
  window: Pick<PlannedEvent, 'starts_at' | 'ends_at' | 'source'>,
): string {
  const start = new Date(window.starts_at)
  if (Number.isNaN(start.getTime())) return ''
  if (window.source === 'holiday') {
    // The end is exclusive: the last day is the one just before it.
    const lastDay = new Date(new Date(window.ends_at).getTime() - 1)
    const first = start.toLocaleDateString(APP_LOCALE, UTC_DAY)
    const last = Number.isNaN(lastDay.getTime()) ? first : lastDay.toLocaleDateString(APP_LOCALE, UTC_DAY)
    return `${first === last ? first : `${first} – ${last}`}, all day UTC`
  }
  return `${formatTimestamp(window.starts_at)} – ${formatTimestamp(window.ends_at)} ${formatUtcOffset(start)}`
}
