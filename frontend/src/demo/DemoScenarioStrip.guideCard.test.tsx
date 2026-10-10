import { afterEach, describe, expect, it } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { Project } from '@/types'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { DemoScenarioStrip } from './DemoScenarioStrip'
import { holdGuideCardOpen } from './guideCardOpen'
import { buildChapterSteps, initialScenarioState, writeScenarioState } from './scenarioModel'
import { liveLoopState } from './scenarioTestState'

const SLUG = 'acme'
const RUN_SCAN_INSTRUCTION = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())[0]
  .instruction

function demoProject(): Project {
  return {
    id: 'p-1',
    name: 'Demo',
    slug: SLUG,
    is_demo: true,
    generation_status: 'ready',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
  } as Project
}

function renderStrip() {
  writeScenarioState(SLUG, liveLoopState('live-loop/run-scan'))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/p/${SLUG}/events`]}>
        <DemoScenarioProvider project={demoProject()} pollIntervalMs={10_000}>
          <DemoScenarioStrip />
        </DemoScenarioProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  window.localStorage.clear()
})

describe('DemoScenarioStrip — beside an open guide card', () => {
  it("keeps the step's instruction for screen readers only while the card says it in full", () => {
    renderStrip()
    const instruction = screen.getByText(RUN_SCAN_INSTRUCTION)
    expect(instruction).not.toHaveClass('sr-only')

    let release = () => {}
    act(() => {
      release = holdGuideCardOpen()
    })
    // Still in the live region, so a screen reader hears the step change.
    expect(screen.getByText(RUN_SCAN_INSTRUCTION)).toHaveClass('sr-only')

    act(() => release())
    expect(screen.getByText(RUN_SCAN_INSTRUCTION)).not.toHaveClass('sr-only')
  })
})
