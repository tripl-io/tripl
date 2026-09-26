/**
 * Display rules for chart annotations (deploy / release / incident markers).
 *
 * Red belongs to anomalies. The backend's column default is `#ef4444`, the same
 * hue as the anomaly dots, so every marker created without a colour — which was
 * every marker, the form never sent one — drew as a red dashed line beside the
 * red anomaly points it was meant to explain (MON-25).
 */

import type { ChartAnnotation, ChartAnnotationSource } from '@/types'

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

const pad = (value: number): string => String(value).padStart(2, '0')

/** `date` as a `datetime-local` input value (local wall-clock time, minutes). */
export function toDatetimeLocalValue(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * The browser's UTC offset at `date`, e.g. "UTC+3", "UTC−5:30", "UTC". The
 * annotation input takes local time while the charts bucket in UTC, so the form
 * names the offset instead of printing a format the native picker does not use
 * (MON-27).
 */
export function formatUtcOffset(date: Date): string {
  const minutes = -date.getTimezoneOffset()
  if (minutes === 0) return 'UTC'
  const sign = minutes > 0 ? '+' : '−'
  const hours = Math.floor(Math.abs(minutes) / 60)
  const rest = Math.abs(minutes) % 60
  return `UTC${sign}${hours}${rest ? `:${pad(rest)}` : ''}`
}
