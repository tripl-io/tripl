/**
 * Drives the phase pointer for a demo create or reset.
 *
 * The server names the stage it is on (`generation_stage` on the polled
 * shell); while it does, that stage is the pointer, and the phases before it
 * are known to be done. A server that names none — an older one, or a reset
 * answered inside its request — gets a timer instead: this is the ONE copy of
 * that loop. Each time `running` turns true the pointer starts again from
 * phase 0, so a retry never resumes where the failed attempt parked.
 */

import { useEffect, useState } from 'react'
import {
  PHASE_TICK_MS,
  nextPhaseIndex,
  phaseIndexForStage,
  type ProvisioningProgress,
} from './provisioningPhases'

export function useEstimatedPhase(running = true): number {
  const [phaseIndex, setPhaseIndex] = useState(0)
  // Restart from the first phase on every new run. Adjusted during render
  // rather than in an effect, so the first frame of a run never shows the
  // previous run's phase.
  const [wasRunning, setWasRunning] = useState(running)
  if (wasRunning !== running) {
    setWasRunning(running)
    if (running) setPhaseIndex(0)
  }

  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setPhaseIndex(nextPhaseIndex), PHASE_TICK_MS)
    return () => clearInterval(timer)
  }, [running])

  return phaseIndex
}

export interface ProvisioningPhaseState {
  /** Index into PROVISIONING_PHASES. */
  index: number
  /** True when `index` is the server's own stage rather than the estimate. */
  confirmed: boolean
  /** The share of the history collected, while that is the stage; else null. */
  percent: number | null
}

/**
 * The server's stage when the latest shell named one this build knows, the
 * timer's estimate otherwise. Once the server speaks, its word wins even where
 * the estimate had run ahead of it: the list then states what is known, not
 * what was guessed.
 */
export function useProvisioningPhase(
  running: boolean,
  progress: ProvisioningProgress | null,
): ProvisioningPhaseState {
  const estimated = useEstimatedPhase(running)
  const reported = phaseIndexForStage(progress?.stage)
  if (reported === null) return { index: estimated, confirmed: false, percent: null }
  const percent =
    progress?.stage === 'history' && progress.percent !== null ? progress.percent : null
  return { index: reported, confirmed: true, percent }
}
