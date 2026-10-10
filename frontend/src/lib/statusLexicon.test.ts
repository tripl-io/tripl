import { describe, expect, it } from 'vitest'
import {
  ALERT_DELIVERY,
  ALERT_DELIVERY_TONE,
  DATA_SOURCE_HEALTH,
  EVENT_STATUS_LABELS,
  EVENT_STATUS_TONE,
  MONITOR_STATUS,
  MONITOR_STATUS_LABEL,
  MONITOR_STATUS_TONE,
  REVIEW_STATUS,
  SCAN_RUN_STATUS,
  SIGNAL_DIRECTION,
  SIGNAL_LEVEL,
  coverageTone,
  dataSourceHealthLexeme,
  rowSignalLevel,
  signalDirectionColor,
  signalDirectionTone,
  toneVar,
} from './statusLexicon'

describe('statusLexicon — colour meaning key', () => {
  it('maps monitor status to firing=danger, warning=warning, healthy=success', () => {
    expect(MONITOR_STATUS.firing).toEqual({ label: 'Firing', tone: 'danger' })
    expect(MONITOR_STATUS.warning).toEqual({ label: 'Warning', tone: 'warning' })
    expect(MONITOR_STATUS.healthy).toEqual({ label: 'Healthy', tone: 'success' })
  })

  it('exposes flattened tone/label maps derived from MONITOR_STATUS', () => {
    expect(MONITOR_STATUS_TONE).toEqual({ firing: 'danger', warning: 'warning', healthy: 'success' })
    expect(MONITOR_STATUS_LABEL).toEqual({ firing: 'Firing', warning: 'Warning', healthy: 'Healthy' })
  })

  it('maps scan-run status: succeeded=success, failed=danger, running=info, queued/cancelled/never=neutral', () => {
    expect(SCAN_RUN_STATUS.succeeded.tone).toBe('success')
    expect(SCAN_RUN_STATUS.failed.tone).toBe('danger')
    expect(SCAN_RUN_STATUS.running.tone).toBe('info')
    expect(SCAN_RUN_STATUS.pending).toEqual({ label: 'Queued', tone: 'neutral' })
    expect(SCAN_RUN_STATUS.cancelled.tone).toBe('neutral')
    expect(SCAN_RUN_STATUS.never.tone).toBe('neutral')
  })

  it('treats a latest-scan signal as Open (danger) and an older signal as Recent (warning)', () => {
    expect(rowSignalLevel('latest_scan')).toBe(SIGNAL_LEVEL.firing)
    expect(rowSignalLevel('recent')).toBe(SIGNAL_LEVEL.warning)
    expect(SIGNAL_LEVEL.firing.tone).toBe('danger')
    expect(SIGNAL_LEVEL.warning.tone).toBe('warning')
    // signals get their OWN words. Detection raises a signal with
    // or without a monitor, so borrowing "Firing"/"Warning" from MONITOR_STATUS
    // put a monitor verdict on rows of a project that had no monitors at all.
    expect(SIGNAL_LEVEL.firing.label).toBe('Open')
    // "Live" belongs to the lifecycle status.
    expect(SIGNAL_LEVEL.firing.label).not.toBe(EVENT_STATUS_LABELS.live)
    expect(SIGNAL_LEVEL.warning.label).toBe('Recent')
    expect(SIGNAL_LEVEL.firing.label).not.toBe(MONITOR_STATUS.firing.label)
    expect(SIGNAL_LEVEL.warning.label).not.toBe(MONITOR_STATUS.warning.label)
  })

  it('maps review status: reviewed=success, needs_review=neutral', () => {
    expect(REVIEW_STATUS.reviewed.tone).toBe('success')
    expect(REVIEW_STATUS.needs_review.tone).toBe('neutral')
  })

  it('maps alert delivery: sent=success, failed=danger, pending=info', () => {
    expect(ALERT_DELIVERY.sent.tone).toBe('success')
    expect(ALERT_DELIVERY.failed.tone).toBe('danger')
    expect(ALERT_DELIVERY.pending.tone).toBe('info')
    expect(ALERT_DELIVERY_TONE).toEqual({ sent: 'success', failed: 'danger', pending: 'info' })
  })

  it('reuses the canonical event lifecycle words and tones (no duplication)', () => {
    expect(EVENT_STATUS_LABELS.live).toBe('Live')
    expect(EVENT_STATUS_TONE.live).toBe('success')
    expect(EVENT_STATUS_LABELS.in_review).toBe('In review')
    expect(EVENT_STATUS_TONE.in_review).toBe('warning')
    expect(EVENT_STATUS_TONE.draft).toBe('neutral')
  })
})

describe('dataSourceHealthLexeme — failed is red, not amber', () => {
  it('renders a failed connection test as danger (matches the error banner)', () => {
    // This is the spot-fix: the connections grid used to paint a failed test
    // amber while its own inline banner painted it red.
    expect(dataSourceHealthLexeme('failed', false)).toBe(DATA_SOURCE_HEALTH.failing)
    expect(dataSourceHealthLexeme('failed', false).tone).toBe('danger')
  })

  it('renders a fresh successful test as healthy/success', () => {
    expect(dataSourceHealthLexeme('success', false)).toEqual({ label: 'Healthy', tone: 'success' })
  })

  it('downgrades a stale successful test to stale/warning', () => {
    expect(dataSourceHealthLexeme('success', true)).toEqual({ label: 'Stale', tone: 'warning' })
  })

  it('renders an untested/unknown source as neutral', () => {
    expect(dataSourceHealthLexeme(null, false)).toEqual({ label: 'Untested', tone: 'neutral' })
    expect(dataSourceHealthLexeme(undefined, true).tone).toBe('neutral')
  })
})

describe('coverageTone — good coverage is green (success), never accent', () => {
  it('uses success/warning/danger across the 90/70 thresholds', () => {
    expect(coverageTone(95)).toBe('success')
    expect(coverageTone(90)).toBe('success')
    expect(coverageTone(80)).toBe('warning')
    expect(coverageTone(70)).toBe('warning')
    expect(coverageTone(50)).toBe('danger')
  })

  it('returns neutral for an unknown percentage', () => {
    expect(coverageTone(undefined)).toBe('neutral')
    expect(coverageTone(null)).toBe('neutral')
  })

  it('never paints good coverage with the brand accent', () => {
    expect(coverageTone(95)).not.toBe('accent')
  })
})

describe('toneVar — tone to CSS property', () => {
  it('resolves the semantic CSS property for a tone', () => {
    expect(toneVar('success')).toBe('var(--success)')
    expect(toneVar('warning')).toBe('var(--warning)')
    expect(toneVar('danger')).toBe('var(--danger)')
    expect(toneVar(coverageTone(95))).toBe('var(--success)')
  })
})

describe('signal direction', () => {
  it('paints a spike as danger and a drop as warning, on every surface', () => {
    expect(signalDirectionTone('spike')).toBe('danger')
    expect(signalDirectionTone('drop')).toBe('warning')
    expect(signalDirectionColor('spike')).toBe('var(--danger)')
    expect(signalDirectionColor('drop')).toBe('var(--warning)')
    expect(SIGNAL_DIRECTION.spike).toEqual({ label: 'Spike', tone: 'danger' })
    expect(SIGNAL_DIRECTION.drop).toEqual({ label: 'Drop', tone: 'warning' })
  })
})
