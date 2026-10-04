/**
 * Drives the estimated phase pointer for a blocking demo request.
 *
 * There is no server-side stage feed for either create or reset, so the pointer
 * is a timer. This is the ONE copy of that loop: the reset dialog
 * mounts it only while its request is in flight, and the create controller
 * passes `running` — each time it turns true the pointer starts again from
 * phase 0, so a retry never resumes where the failed attempt parked.
 */

import { useEffect, useState } from 'react'
import { PHASE_TICK_MS, nextPhaseIndex } from './provisioningPhases'

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
