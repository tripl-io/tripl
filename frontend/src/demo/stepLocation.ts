/**
 * Where a scenario step's link leads, and where the user stands relative to
 * it — shared by the strip, which offers the link, and the guide host, which
 * says what to do when no coach mark can.
 */

import { useEffect, useState } from 'react'
import { useLocation, useParams } from 'react-router-dom'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { useDemoScenario } from './demoScenarioContext'
import { useWelcomeDismissed } from './welcomeDismissal'

interface Place {
  pathname: string
  search: string
}

/** The step link's path, without the tab it may name. */
export function stepPath(to: string): string {
  return to.split('?')[0] ?? to
}

/** Every query param the step's link names is present in `search` with the same value. */
export function searchMatches(search: string, to: string): boolean {
  const query = to.split('?')[1]
  if (!query) return true
  const current = new URLSearchParams(search)
  return [...new URLSearchParams(query)].every(([key, value]) => current.get(key) === value)
}

/**
 * On the page the step's link opens — the page itself, not a page under it:
 * from a scan's detail the link back to the Scans list still goes somewhere.
 * A step that names a tab (`?section=monitors`) is on its page only on that
 * tab, so from another tab the link stays and takes the user to the control.
 */
export function isOnStepPage(place: Place, to: string): boolean {
  return (
    place.pathname.replace(/\/$/, '') === stepPath(to).replace(/\/$/, '') &&
    searchMatches(place.search, to)
  )
}

/**
 * On the step's surface: its page or a page under it, on the tab it names —
 * where the step's control is expected to be on screen.
 */
export function isOnStepSurface(place: Place, to: string): boolean {
  return place.pathname.startsWith(stepPath(to)) && searchMatches(place.search, to)
}

/** True only after `value` held true for `delayMs` without interruption. */
export function useDeferredFlag(value: boolean, delayMs: number): boolean {
  const [deferred, setDeferred] = useState(false)
  useEffect(() => {
    // A zero-delay timer (rather than a sync set) also handles the reset, so
    // the effect never calls setState synchronously.
    const timer = window.setTimeout(() => setDeferred(value), value ? delayMs : 0)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return value && deferred
}

/**
 * True while `value` is, and for `delayMs` after it turns false: lowered late,
 * never raised late.
 */
export function useLingeringFlag(value: boolean, delayMs: number): boolean {
  const [held, setHeld] = useState(value)
  useEffect(() => {
    // Timers both ways, as in useDeferredFlag: never a sync set in the effect.
    const timer = window.setTimeout(() => setHeld(value), value ? 0 : delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return value || held
}

/**
 * First visit: the Overview's welcome panel already offers every chapter, and
 * banner + strip + panel stacked three demo blocks above the page title.
 * Until the user engages, the panel stands in for the coaching there instead
 * of beside it. Engaging is more than leaving the first step: a user who
 * picked "Run the live loop" or pressed Restart is on that very step too, and
 * must keep the coaching.
 */
export function useWelcomeStandsIn(): boolean {
  const { state, activeChapter, step } = useDemoScenario()
  const location = useLocation()
  const { slug } = useParams()
  const welcomeDismissed = useWelcomeDismissed(slug ?? '')
  const welcomeShowing =
    slug !== undefined &&
    !welcomeDismissed &&
    location.pathname === projectPath(currentOrgSlug(), slug, '/overview')
  const untouched =
    !state.engaged && activeChapter === 'live-loop' && step.id === 'live-loop/run-scan'
  return welcomeShowing && untouched
}
