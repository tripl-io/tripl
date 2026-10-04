/**
 * Triage for open signals and their verdicts (#254).
 *
 * A signal no alert rule routed to an incident can be acknowledged (seen,
 * stays listed) or have its scope muted (hidden for 24 h, 7 d or until
 * unmuted); a routed one (it carries an incident) leaves those to the inbox.
 * EVERY signal can take a verdict — expected (with its reason; an annotation
 * on the bucket and the signal hidden), tracking bug, false positive or real
 * issue. On a routed signal the verdict is written to its incident, which
 * stays the source of truth. Hidden signals and signals with a verdict leave
 * the open-signal counts.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { eventMetricsApi } from '@/api/eventMetrics'
import { formatTimestamp } from '@/lib/datetime'
import {
  activeSignalsKey,
  activityKey,
  alertInboxGroupKey,
  alertInboxKey,
  projectChartAnnotationsKey,
  projectEventHistoryKey,
  projectKey,
  projectMonitoringSeriesKey,
  projectsKey,
} from '@/lib/queryKeys'
import { verdictLabel } from '@/lib/signalVerdict'
import type {
  MonitoringSignal,
  SignalExpectedReason,
  SignalMuteDuration,
  SignalTriageScope,
  SignalVerdictKind,
} from '@/types'

/** The mute lengths the row menu offers, in the order it lists them. */
export const MUTE_OPTIONS: ReadonlyArray<{ duration: SignalMuteDuration; label: string }> = [
  { duration: '24h', label: 'For 24 hours' },
  { duration: '7d', label: 'For 7 days' },
  { duration: 'until_unmuted', label: 'Until unmuted' },
]

/** Scopes a verdict can be recorded on: the ones the signal lists surface. */
const TRIAGE_SCOPES = new Set(['project_total', 'event_type', 'event', 'metric'])

/** The incident a signal was routed into, from either payload shape. */
export function signalIncidentId(signal: MonitoringSignal): string | null {
  return signal.incident?.id ?? signal.incident_id ?? null
}

/** Whether this row gets acknowledge and mute: open here, and not an incident. */
export function canTriageSignal(signal: MonitoringSignal): boolean {
  return !signalIncidentId(signal) && TRIAGE_SCOPES.has(signal.scope_type)
}

/** Whether a verdict can be recorded on this signal: any listed scope, routed or not. */
export function canSetVerdict(signal: MonitoringSignal): boolean {
  return TRIAGE_SCOPES.has(signal.scope_type)
}

/**
 * Whether the signal's own verdict can be cleared from here. An incident's
 * verdict is the incident's status, so it is changed in the inbox.
 */
export function canClearVerdict(signal: MonitoringSignal): boolean {
  return canSetVerdict(signal) && signal.verdict?.source === 'signal'
}

/**
 * Whether clearing the signal's verdict also reopens its incident: a routed
 * signal's verdict set the incident's status, and clearing it puts the
 * incident back to open, so the action says so and asks first.
 */
export function clearVerdictReopensIncident(signal: MonitoringSignal): boolean {
  return canClearVerdict(signal) && signalIncidentId(signal) !== null
}

/** The clear action's label, naming the incident it reopens when there is one. */
export function clearVerdictLabel(signal: MonitoringSignal): string {
  return clearVerdictReopensIncident(signal) ? 'Clear verdict and reopen incident' : 'Clear verdict'
}

/** The confirmation asked before a clear that reopens the signal's incident. */
export const CLEAR_AND_REOPEN_CONFIRM = {
  title: 'Clear the verdict and reopen the incident?',
  message:
    'This signal belongs to an incident its verdict updated. Clearing the verdict reopens the incident in Alerting.',
  confirmLabel: 'Clear and reopen',
  variant: 'danger',
} as const

/** The key a verdict is written under, the way the signal keys itself. */
export function triageScopeOf(signal: MonitoringSignal): SignalTriageScope {
  return {
    // A catalog metric is project-global: it carries no scan config.
    scan_config_id: signal.scope_type === 'metric' ? null : signal.scan_config_id,
    scope_type: signal.scope_type,
    scope_ref: signal.scope_ref,
    bucket: signal.bucket,
  }
}

/**
 * The verdict a row shows next to its name, or null when it has none.
 * A recorded verdict wins (it answers this one signal), then expected, then
 * muted, and all over acknowledged, which does not hide anything.
 */
export function triageStatusLabel(signal: MonitoringSignal): string | null {
  if (signal.verdict) return verdictLabel(signal.verdict)
  if (signal.expected) return 'Expected'
  if (signal.muted) {
    return signal.muted_until ? `Muted until ${formatTimestamp(signal.muted_until)}` : 'Muted'
  }
  if (signal.acknowledged_at) return 'Acknowledged'
  return null
}

/** How many signals in `signals` a verdict hides: the "Show hidden (n)" count. */
export function countHiddenSignals(signals: readonly MonitoringSignal[]): number {
  return signals.filter((signal) => signal.hidden).length
}

export type TriageVerb =
  | { kind: 'acknowledge' }
  | { kind: 'unacknowledge' }
  | { kind: 'mute'; duration: SignalMuteDuration }
  | { kind: 'unmute' }
  | {
      kind: 'verdict'
      verdict: SignalVerdictKind
      expectedReason: SignalExpectedReason | null
      note: string | null
    }
  | { kind: 'clearVerdict' }

function runVerb(slug: string, scope: SignalTriageScope, verb: TriageVerb): Promise<unknown> {
  switch (verb.kind) {
    case 'acknowledge':
      return eventMetricsApi.acknowledgeSignal(slug, scope)
    case 'unacknowledge':
      return eventMetricsApi.unacknowledgeSignal(slug, scope)
    case 'mute':
      return eventMetricsApi.muteSignalScope(slug, scope, verb.duration)
    case 'unmute':
      return eventMetricsApi.unmuteSignalScope(slug, scope)
    case 'verdict':
      return eventMetricsApi.setSignalVerdict(slug, {
        ...scope,
        verdict: verb.verdict,
        // The reason belongs to `expected` alone; the server rejects it elsewhere.
        expected_reason: verb.verdict === 'expected' ? verb.expectedReason : null,
        note: verb.note,
      })
    case 'clearVerdict':
      return eventMetricsApi.clearSignalVerdict(slug, scope)
  }
}

/** The undo of each triage action, offered on its confirmation toast. */
const UNDO: Partial<Record<TriageVerb['kind'], TriageVerb>> = {
  acknowledge: { kind: 'unacknowledge' },
  mute: { kind: 'unmute' },
}

/** Verdicts that write to the incident when the signal was routed to one. */
const INCIDENT_VERBS = new Set<TriageVerb['kind']>(['verdict', 'clearVerdict'])

const DONE_MESSAGE: Record<TriageVerb['kind'], string> = {
  acknowledge: 'Signal acknowledged',
  unacknowledge: 'Acknowledgement removed',
  mute: 'Scope muted',
  unmute: 'Scope unmuted',
  verdict: 'Verdict saved',
  clearVerdict: 'Verdict cleared',
}

function doneMessage(verb: TriageVerb, routed: boolean): string {
  if (verb.kind !== 'verdict') return DONE_MESSAGE[verb.kind]
  const label = verdictLabel({ verdict: verb.verdict, expected_reason: verb.expectedReason })
  return routed ? `Marked as ${label.toLowerCase()} — the incident was updated` : `Marked as ${label.toLowerCase()}`
}

/**
 * One mutation for every triage action and verdict. On success it refreshes
 * every surface that reads the signals — the lists and the verdict counts, the
 * sidebar badge (project summaries), the drilldown series whose points and
 * latest signal carry the verdict, the activity feeds and the chart
 * annotations (an expected verdict adds one); a verdict on a routed signal
 * refreshes the inbox its incident lives in. Acknowledge and mute confirm with
 * an Undo; a verdict is undone with "Clear verdict", since on an incident it
 * moved a status the toast cannot put back.
 */
export function useSignalTriage(slug: string) {
  const qc = useQueryClient()
  const mutation = useMutation({
    mutationFn: ({ scope, verb }: { scope: SignalTriageScope; verb: TriageVerb; routed: boolean }) =>
      runVerb(slug, scope, verb),
    onSuccess: (_data, { scope, verb, routed }) => {
      void qc.invalidateQueries({ queryKey: activeSignalsKey(slug) })
      void qc.invalidateQueries({ queryKey: projectKey(slug) })
      void qc.invalidateQueries({ queryKey: projectsKey() })
      const isVerdict = INCIDENT_VERBS.has(verb.kind)
      if (isVerdict) {
        void qc.invalidateQueries({ queryKey: projectMonitoringSeriesKey(slug) })
        void qc.invalidateQueries({ queryKey: activityKey(slug) })
        void qc.invalidateQueries({ queryKey: projectEventHistoryKey(slug) })
        void qc.invalidateQueries({ queryKey: projectChartAnnotationsKey(slug) })
        if (routed) {
          void qc.invalidateQueries({ queryKey: alertInboxKey(slug) })
          void qc.invalidateQueries({ queryKey: alertInboxGroupKey(slug) })
        }
      }
      const undo = UNDO[verb.kind]
      toast.success(doneMessage(verb, routed), {
        action: undo
          ? { label: 'Undo', onClick: () => mutation.mutate({ scope, verb: undo, routed }) }
          : undefined,
      })
    },
  })
  return {
    isPending: mutation.isPending,
    // A failure is reported by the app-wide mutation error toast.
    run: (signal: MonitoringSignal, verb: TriageVerb, onDone?: () => void) =>
      mutation.mutate(
        { scope: triageScopeOf(signal), verb, routed: signalIncidentId(signal) !== null },
        { onSuccess: onDone },
      ),
  }
}
