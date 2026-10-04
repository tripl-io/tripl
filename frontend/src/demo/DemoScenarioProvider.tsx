/**
 * The coached demo scenario, loaded eagerly: the runtime and the model come in
 * with this module, and the contexts are live on the first render.
 *
 * The app shell does not use it — it mounts LazyDemoScenarioProvider, which
 * keeps the model off the first load and fetches it only for a demo project.
 * This one serves the tests and anything that renders the
 * scenario outside the shell, and wraps the same runtime, so the two cannot
 * drift apart.
 */

import type { ReactNode } from 'react'
import type { Project } from '@/types'
import { DemoScenarioContexts } from './DemoScenarioContexts'
import { useCoachPresenceState } from './demoScenarioContext'
import { useDemoScenarioRuntime } from './useDemoScenarioRuntime'

interface DemoScenarioProviderProps {
  /** The project in scope, or undefined outside a project route. */
  project?: Project
  /** Poll cadence override — tests only. */
  pollIntervalMs?: number
  children: ReactNode
}

export function DemoScenarioProvider({
  project,
  pollIntervalMs,
  children,
}: DemoScenarioProviderProps) {
  const { value, actions } = useDemoScenarioRuntime(project, pollIntervalMs)
  const presence = useCoachPresenceState()
  return (
    <DemoScenarioContexts value={value} actions={actions} presence={presence}>
      {children}
    </DemoScenarioContexts>
  )
}
