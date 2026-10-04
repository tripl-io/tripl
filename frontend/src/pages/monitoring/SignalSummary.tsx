import type { ReactNode } from 'react'
import { CalendarPlus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { formatTimestamp } from '@/lib/datetime'
import { NO_BASELINE_LABEL, ratioDelta } from '@/lib/percentDelta'
import { signalDirectionTone } from '@/lib/statusLexicon'
import type { MonitoringSignal } from '@/types'

/**
 * The detail page's signal card as one sentence and its reason (MO-2 / MO-4):
 * "Sep 25, 6:00 PM: 5,767 events, 82% above the expected 3,174 (16.0σ)."
 * The 4-up grid of raw figures it replaces asked the reader to assemble that
 * sentence themselves, and never said why the bucket was flagged.
 */
export function SignalSummary({
  signal,
  formatActual,
  formatExpected,
  sigmaThreshold,
  onAnnotate,
}: {
  signal: MonitoringSignal
  /** The flagged value with its noun, e.g. "5,767 events" or "$1,234". */
  formatActual: (value: number) => string
  /** The expectation; value-aware, since a baseline can be sub-unit. */
  formatExpected: (value: number) => string
  /** The scope's detection threshold, when the payload names one. */
  sigmaThreshold?: number | null
  /**
   * Start an annotation on the flagged bucket. Given on the scopes without the
   * event hero, whose signal banner already carries one (JR-5); omitted for a
   * viewer, who cannot annotate.
   */
  onAnnotate?: (bucket: string) => void
}) {
  const tone = signalDirectionTone(signal.direction)
  const when = formatTimestamp(signal.bucket)
  const sigma = `${Math.abs(signal.z_score).toFixed(1)}σ`
  const delta = ratioDelta(signal.actual_count, signal.expected_count)
  const droppedToZero = signal.direction === 'drop' && signal.actual_count === 0
  const expected = formatExpected(signal.expected_count)

  let change: ReactNode
  let why: string
  if (droppedToZero) {
    // The z-score of a series that bottomed out is clamped, not informative,
    // so neither line leans on it.
    change = <>dropped to <span style={{ color: `var(--${tone})` }}>zero</span>, against an expected {expected}.</>
    why = `the series fell to zero where ${expected} was expected.`
  } else if (delta === null) {
    change = <>{formatActual(signal.actual_count)}, with {NO_BASELINE_LABEL} to compare against.</>
    why = 'it fired where nothing was expected.'
  } else {
    change = (
      <>
        {formatActual(signal.actual_count)},{' '}
        <span style={{ color: `var(--${tone})` }}>
          {Math.abs(delta).toFixed(0)}% {delta >= 0 ? 'above' : 'below'}
        </span>{' '}
        the expected {expected} ({sigma}).
      </>
    )
    why = typeof sigmaThreshold === 'number'
      ? `${sigma} from the expected value; anything past ${sigmaThreshold}σ is flagged.`
      : `${sigma} from the expected value, outside the normal range.`
  }

  return (
    <div data-testid="signal-summary" className="rounded-card border px-4 py-3">
      <p className="text-body text-fg">
        <span className="whitespace-nowrap">{when}</span>: {change}
      </p>
      <p className="mt-1 text-body-sm text-fg-secondary">
        Why flagged: {why}
      </p>
      {onAnnotate && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-2"
          onClick={() => onAnnotate(signal.bucket)}
        >
          <CalendarPlus aria-hidden="true" />
          Annotate
        </Button>
      )}
    </div>
  )
}
