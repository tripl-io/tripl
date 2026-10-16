/**
 * The progress list shared by demo create and demo reset.
 *
 * While the server reports its stage the list states it: phases before it are
 * done, and the history phase carries how far it has got. From a server that
 * reports none, the list narrates *expected* phases on a timer — passed phases
 * are then deliberately NOT ticked while the request is open (the client
 * cannot observe that the server finished them) and the caption calls the
 * steps approximate.
 */

import { useEffect, useState } from 'react'
import { Check, Loader2, Minus } from 'lucide-react'
import { DEMO_PROVISION_SLOW_MS, PROVISIONING_PHASES, phaseLabel } from './provisioningPhases'

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
  confirmed = false,
  percent = null,
  complete = false,
  slowMessage = DEFAULT_SLOW_MESSAGE,
}: {
  phaseIndex: number
  /** The server reported `phaseIndex` itself: earlier phases are known to be done. */
  confirmed?: boolean
  /** The share of the active phase done, 0..100, when the server reports one. */
  percent?: number | null
  /** True once the request has actually resolved — the only proof work is done. */
  complete?: boolean
  /**
   * Shown once the wait is well past the estimate, so a pointer
   * parked on the last phase is not the only signal. Each caller names the way
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
              ? confirmed
                ? 'done'
                : 'estimated'
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
                {state === 'active' ? phaseLabel(index, percent) : phase.label}
              </span>
            </li>
          )
        })}
      </ol>
      {!complete && !confirmed && (
        <p className="text-caption text-fg-tertiary">
          Steps are approximate — the demo opens as soon as it is ready.
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
  // Probably passed, never confirmed: a muted dash, not the check that only
  // the finished request (or the server's own word) earns. A dot in a grey
  // circle read as a selected radio button.
  if (state === 'estimated') {
    return (
      <span aria-hidden="true" className="flex h-5 w-5 shrink-0 items-center justify-center">
        <Minus className="h-3 w-3 text-fg-tertiary" />
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
