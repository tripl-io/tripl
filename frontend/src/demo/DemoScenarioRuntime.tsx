/**
 * The scenario runtime as a lazily loaded, render-nothing component.
 * It sits BESIDE the page, not around it, and hands what it
 * computes up to LazyDemoScenarioProvider — so the chunk arriving, or a project
 * switch between demo and non-demo, never remounts the page under it.
 */

import { useLayoutEffect } from 'react'
import type { Project } from '@/types'
import type { DemoScenarioActions, DemoScenarioValue } from './demoScenarioContext'
import { useDemoScenarioRuntime } from './useDemoScenarioRuntime'

/** What the runtime publishes, tagged with the project it was computed for. */
export interface PublishedScenario {
  slug: string | undefined
  value: DemoScenarioValue
  actions: DemoScenarioActions
}

export default function DemoScenarioRuntime({
  project,
  pollIntervalMs,
  onPublish,
}: {
  project: Project | undefined
  pollIntervalMs?: number
  onPublish: (published: PublishedScenario | null) => void
}) {
  const { value, actions } = useDemoScenarioRuntime(project, pollIntervalMs)
  const slug = project?.slug

  // Layout effects, so a step the user just completed reaches the page before
  // the browser paints the stale one.
  useLayoutEffect(() => {
    onPublish({ slug, value, actions })
  }, [onPublish, slug, value, actions])
  useLayoutEffect(() => () => onPublish(null), [onPublish])

  return null
}
