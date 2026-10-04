/**
 * The estimated-progress list shared by demo create and demo reset.
 *
 * Both are single blocking POSTs of ~10 s with no server-side stage feed, so the
 * list narrates *expected* phases on a timer. Passed phases are deliberately NOT
 * ticked while the request is open — the client cannot observe that the server
 * finished them — and the caption says so.
 */

import { useEffect, useState } from 'react'
import { Check, Loader2 } from 'lucide-react'
import { DEMO_PROVISION_SLOW_MS, PROVISIONING_PHASES } from './provisioningPhases'

const DEFAULT_SLOW_MESSAGE =
  'Taking longer than usual — you can cancel; the server may still finish.'

/**
 * True once the list has been on screen for `ms`. Mount-scoped like the phase
 * pointer: callers mount the list only while their request is open, so every
 * request starts the clock afresh.
 */
function useElapsed(ms: number, running: boolean): boolean {
  const [elapsed, setElapsed] = useState(false)
  useEffect(() => {
    if (!running) return
    const timer = setTimeout(() => setElapsed(true), ms)
    return () => clearTimeout(timer)
  }, [ms, running])
  return running && elapsed
}

type PhaseState = 'done' | 'estimated' | 'active' | 'pending'

export function ProvisioningPhaseList({
  phaseIndex,
  complete = false,
  slowMessage = DEFAULT_SLOW_MESSAGE,
}: {
  phaseIndex: number
  /** True once the request has actually resolved — the only proof work is done. */
  complete?: boolean
  /**
   * Shown once the wait is well past the estimate (DEMO-21), so a pointer
   * parked on "Finalizing" is not the only signal. Each caller names the way
   * out it actually offers.
   */
  slowMessage?: string
}) {
  const slow = useElapsed(DEMO_PROVISION_SLOW_MS, !complete)
  return (
    <div className="space-y-2">
      <ol className="space-y-1.5">
        {PROVISIONING_PHASES.map((phase, index) => {
          const state: PhaseState = complete
            ? 'done'
            : index < phaseIndex
              ? 'estimated'
              : index === phaseIndex
                ? 'active'
                : 'pending'
          return (
            <li key={phase.id} className="flex items-center gap-2.5 text-body-sm">
              <PhaseIcon state={state} />
              <span
                style={{
                  color: state === 'pending' ? 'var(--fg-faint)' : 'var(--fg)',
                  fontWeight: state === 'active' ? 600 : 400,
                }}
              >
                {phase.label}
              </span>
            </li>
          )
        })}
      </ol>
      {!complete && (
        <p className="text-caption text-fg-tertiary">
          Estimated steps — the server reports only the final result, not the stage it is on.
        </p>
      )}
      {slow && (
        <p className="text-caption text-fg-secondary">
          {slowMessage}
        </p>
      )}
    </div>
  )
}

function PhaseIcon({ state }: { state: PhaseState }) {
  if (state === 'done') {
    return (
      <span
        aria-hidden="true"
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-success-soft text-success"
      >
        <Check className="h-3 w-3" />
      </span>
    )
  }
  if (state === 'estimated') {
    return (
      <span
        aria-hidden="true"
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-surface-hover"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-fg-tertiary" />
      </span>
    )
  }
  if (state === 'active') {
    return (
      <Loader2
        aria-hidden="true"
        className="h-5 w-5 shrink-0 animate-spin text-accent"
      />
    )
  }
  return (
    <span
      aria-hidden="true"
      className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-border"
    />
  )
}
