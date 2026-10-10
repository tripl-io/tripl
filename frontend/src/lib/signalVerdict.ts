/**
 * Signal verdicts (#254): what a person decided a signal was, the words every
 * surface uses for them, and the one "needs a verdict" test the Anomalies
 * filter, the Overview headline and the bell share.
 */
import type { ChipTone } from '@/components/primitives/chip-variants'
import { formatTimestamp } from '@/lib/datetime'
import { formatSignalEffect } from '@/lib/monitoring'
import { formatSignalValues } from '@/lib/signalMetricFormat'
import type {
  MonitoringSignal,
  SignalExpectedReason,
  SignalVerdictInfo,
  SignalVerdictKind,
} from '@/types'

export interface VerdictOption {
  verdict: SignalVerdictKind
  label: string
  /** What recording it does, said where it is chosen. */
  hint: string
}

/** The four verdicts, in the order every surface offers them. */
export const VERDICT_OPTIONS: readonly VerdictOption[] = [
  {
    verdict: 'expected',
    label: 'Expected',
    hint: 'Adds an annotation on this bucket and hides the signal.',
  },
  {
    verdict: 'tracking_bug',
    label: 'Tracking bug',
    hint: 'The data is wrong, not the product. Raise it on the event.',
  },
  {
    verdict: 'false_positive',
    label: 'False positive',
    hint: 'Detection was too sensitive here; its threshold for this scope is raised.',
  },
  {
    verdict: 'real_issue',
    label: 'Real issue',
    hint: 'The move is real and somebody owes a fix.',
  },
]

export const EXPECTED_REASON_OPTIONS: ReadonlyArray<{ reason: SignalExpectedReason; label: string }> = [
  { reason: 'campaign', label: 'Campaign' },
  { reason: 'release', label: 'Release' },
  { reason: 'seasonality', label: 'Seasonality' },
  { reason: 'other', label: 'Other' },
]

/** The reason picker's "nothing picked yet" value. */
export type ReasonChoice = SignalExpectedReason | ''

// Matches the verdict note's limit on the server (the triage note's too).
export const VERDICT_NOTE_MAX_LENGTH = 2000

/** Whether a verdict's fields are complete enough to save: `expected` needs its reason. */
export function verdictFieldsReady(verdict: SignalVerdictKind, reason: ReasonChoice): boolean {
  return verdict !== 'expected' || reason !== ''
}

const VERDICT_LABEL: Record<SignalVerdictKind, string> = {
  expected: 'Expected',
  tracking_bug: 'Tracking bug',
  false_positive: 'False positive',
  real_issue: 'Real issue',
}

const REASON_LABEL: Record<SignalExpectedReason, string> = {
  campaign: 'campaign',
  release: 'release',
  seasonality: 'seasonality',
  other: 'other',
}

/**
 * A verdict's chip tone. Expected is settled (success); a tracking bug is the
 * data's problem (warning); a false positive is a call about the detector,
 * accent as the incident's is; a real issue is the one that owes a fix.
 */
const VERDICT_TONE: Record<SignalVerdictKind, ChipTone> = {
  expected: 'success',
  tracking_bug: 'warning',
  false_positive: 'accent',
  real_issue: 'danger',
}

export function verdictTone(kind: SignalVerdictKind): ChipTone {
  return VERDICT_TONE[kind]
}

/** "Expected · campaign", "Tracking bug": the verdict as one short phrase. */
export function verdictLabel(verdict: Pick<SignalVerdictInfo, 'verdict' | 'expected_reason'>): string {
  const base = VERDICT_LABEL[verdict.verdict]
  if (verdict.verdict === 'expected' && verdict.expected_reason) {
    return `${base} · ${REASON_LABEL[verdict.expected_reason]}`
  }
  return base
}

/**
 * Who decided and when: "by Ann Lee, Sep 25, 7:00 PM". An incident-sourced
 * verdict says it came from the incident, since that is where it is changed.
 * A verdict without a known time leaves the time out.
 */
export function verdictAttribution(verdict: SignalVerdictInfo): string {
  const who = verdict.author_name ? `by ${verdict.author_name}` : null
  const when = verdict.created_at ? formatTimestamp(verdict.created_at) : null
  const parts = [who, when].filter(Boolean).join(', ')
  return verdict.source === 'incident' ? `From the incident${parts ? ` · ${parts}` : ''}` : parts
}

/**
 * Whether a signal still waits for somebody's call. Acknowledged is not a
 * verdict (it only says "seen"), so an acknowledged signal still needs one.
 */
export function needsVerdict(signal: Pick<MonitoringSignal, 'verdict'>): boolean {
  return !signal.verdict
}

/**
 * The text "Open a comment on the event" starts the composer with, for a
 * tracking bug: which bucket, how far it moved, and the verdict's note, so
 * the thread reads on its own without the chart beside it.
 */
export function signalCommentDraft(
  signal: Pick<
    MonitoringSignal,
    'bucket' | 'direction' | 'actual_count' | 'expected_count' | 'z_score' | 'unit' | 'relative_effect'
  >,
  note?: string | null,
): string {
  const move = signal.direction === 'drop' ? 'drop' : 'spike'
  const lines = [
    `Tracking bug: ${move} at ${formatTimestamp(signal.bucket)} (${formatSignalEffect(signal)}, ${formatSignalValues(signal)} expected).`,
  ]
  const trimmed = note?.trim()
  if (trimmed) lines.push('', trimmed)
  return lines.join('\n')
}
