import { describe, expect, it } from 'vitest'
import {
  ANNOTATION_DEFAULT_COLOR,
  AUTOMATIC_ANNOTATION_COLOR,
  annotationMarkerColor,
  annotationSourceLabel,
  isAutomaticAnnotation,
  safeAnnotationUrl,
  formatUtcOffset,
  toDatetimeLocalValue,
  annotationDisplayColor,
  truncateAnnotationLabel,
} from './chartAnnotations'

describe('annotationDisplayColor (MON-25)', () => {
  it('never draws an uncoloured marker in the anomaly red', () => {
    expect(annotationDisplayColor('#ef4444')).toBe(ANNOTATION_DEFAULT_COLOR)
    expect(annotationDisplayColor('#EF4444')).toBe(ANNOTATION_DEFAULT_COLOR)
    expect(annotationDisplayColor('')).toBe(ANNOTATION_DEFAULT_COLOR)
    expect(annotationDisplayColor(null)).toBe(ANNOTATION_DEFAULT_COLOR)
  })

  it('keeps a colour someone actually chose', () => {
    expect(annotationDisplayColor('#22c55e')).toBe('#22c55e')
  })
})

describe('truncateAnnotationLabel (MON-27)', () => {
  it('shortens long labels for the chart and leaves short ones alone', () => {
    expect(truncateAnnotationLabel('v1.4 deploy')).toBe('v1.4 deploy')
    const long = 'Rolled out the new checkout flow to every region'
    expect(truncateAnnotationLabel(long)).toHaveLength(24)
    expect(truncateAnnotationLabel(long).endsWith('…')).toBe(true)
  })
})

describe('annotation form helpers (MON-27)', () => {
  it('formats a date as a datetime-local value in local time', () => {
    expect(toDatetimeLocalValue(new Date(2026, 0, 2, 9, 5))).toBe('2026-01-02T09:05')
  })

  it('names the UTC offset, with minutes only when there are some', () => {
    const at = (offsetMinutes: number) => ({ getTimezoneOffset: () => offsetMinutes }) as Date
    expect(formatUtcOffset(at(0))).toBe('UTC')
    expect(formatUtcOffset(at(-180))).toBe('UTC+3')
    expect(formatUtcOffset(at(300))).toBe('UTC−5')
    expect(formatUtcOffset(at(-330))).toBe('UTC+5:30')
  })
})

describe('release and API annotations (#256)', () => {
  it('treats release and API rows as automatic, manual ones not', () => {
    expect(isAutomaticAnnotation({ source: 'release' })).toBe(true)
    expect(isAutomaticAnnotation({ source: 'api' })).toBe(true)
    expect(isAutomaticAnnotation({ source: 'manual' })).toBe(false)
  })

  it('draws automatic markers muted whatever colour they store', () => {
    expect(annotationMarkerColor({ source: 'release', color: '#22c55e' })).toBe(AUTOMATIC_ANNOTATION_COLOR)
    expect(annotationMarkerColor({ source: 'api', color: '#22c55e' })).toBe(AUTOMATIC_ANNOTATION_COLOR)
    expect(annotationMarkerColor({ source: 'manual', color: '#22c55e' })).toBe('#22c55e')
    expect(annotationMarkerColor({ source: 'manual', color: '#ef4444' })).toBe(ANNOTATION_DEFAULT_COLOR)
  })

  it('names each source', () => {
    expect(annotationSourceLabel('release')).toBe('Release')
    expect(annotationSourceLabel('api')).toBe('API')
    expect(annotationSourceLabel('manual')).toBe('Manual')
  })

  it('only ever links http and https URLs', () => {
    expect(safeAnnotationUrl('https://github.com/acme/app/releases/v1.4.0'))
      .toBe('https://github.com/acme/app/releases/v1.4.0')
    expect(safeAnnotationUrl('http://ci.local/run/1')).toBe('http://ci.local/run/1')
    expect(safeAnnotationUrl('javascript:alert(1)')).toBeNull()
    expect(safeAnnotationUrl('data:text/html,hi')).toBeNull()
    expect(safeAnnotationUrl('not a url')).toBeNull()
    expect(safeAnnotationUrl('')).toBeNull()
    expect(safeAnnotationUrl(null)).toBeNull()
  })
})
