/**
 * The coached demo scenario as the app shell mounts it.
 *
 * A thin context: the scenario model and its runtime are a chunk of their own,
 * fetched only when the project in scope is a ready demo — every other project
 * never downloads them. Until the chunk lands (and for every non-demo project)
 * the contexts hold their inert values, which every consumer already handles.
 *
 * The runtime renders beside the children and publishes up, rather than
 * wrapping them, so the page keeps its place in the tree however the scenario
 * loads; see DemoScenarioRuntime.
 */

import { Suspense, useState, type ReactNode } from 'react'
import { ErrorBoundary } from '@/components/error-boundary'
import { lazyWithReload } from '@/lib/lazyWithReload'
import type { Project } from '@/types'
import { DemoScenarioContexts } from './DemoScenarioContexts'
import type { PublishedScenario } from './DemoScenarioRuntime'
import {
  INERT_ACTIONS,
  INERT_SCENARIO,
  isCoachableDemo,
  useCoachPresenceState,
} from './demoScenarioContext'

const DemoScenarioRuntime = lazyWithReload(() => import('./DemoScenarioRuntime'))

export function LazyDemoScenarioProvider({
  project,
  children,
}: {
  /** The project in scope, or undefined outside a project route. */
  project?: Project
  children: ReactNode
}) {
  const coachable = isCoachableDemo(project)
  const [published, setPublished] = useState<PublishedScenario | null>(null)
  const presence = useCoachPresenceState()

  // Only what was computed for THIS project: between a switch and the runtime's
  // next publish, the previous demo's steps must not show on the new one.
  const current = coachable && published?.slug === project?.slug ? published : null

  return (
    <>
      <DemoScenarioContexts
        value={current?.value ?? INERT_SCENARIO}
        actions={current?.actions ?? INERT_ACTIONS}
        presence={presence}
      >
        {children}
      </DemoScenarioContexts>
      {coachable && (
        // A chunk that fails to load leaves the scenario inert, not the app
        // blank: coaching is an extra, the page is not.
        <ErrorBoundary fallback={() => null}>
          <Suspense fallback={null}>
            <DemoScenarioRuntime project={project} onPublish={setPublished} />
          </Suspense>
        </ErrorBoundary>
      )}
    </>
  )
}
