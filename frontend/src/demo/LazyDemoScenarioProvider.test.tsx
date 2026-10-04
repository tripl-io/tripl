import { useEffect } from 'react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { Project } from '@/types'
import { useDemoScenario, useDemoScenarioActions, useScenarioArtifacts } from './demoScenarioContext'
import { LazyDemoScenarioProvider } from './LazyDemoScenarioProvider'

const SLUG = 'acme'

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p-1',
    name: 'Demo',
    slug: SLUG,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
    ...overrides,
  } as Project
}

/** Counts its own mounts, so a remount under the provider shows up. */
const mounts = { count: 0 }

function Probe() {
  const { available, active, step } = useDemoScenario()
  const { startChapter } = useDemoScenarioActions()
  const { scanJobId } = useScenarioArtifacts()
  useEffect(() => {
    mounts.count += 1
  }, [])
  return (
    <div>
      <span data-testid="available">{String(available)}</span>
      <span data-testid="active">{String(active)}</span>
      <span data-testid="step">{step.id}</span>
      <span data-testid="scan-job">{scanJobId ?? 'none'}</span>
      <button type="button" onClick={() => startChapter('edit-event')}>
        start edit-event
      </button>
    </div>
  )
}

function renderProvider(scope: Project | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/p/${SLUG}/overview`]}>
        <LazyDemoScenarioProvider project={scope}>
          <Probe />
        </LazyDemoScenarioProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// Transform the runtime's module graph once, outside any test's wait.
beforeAll(async () => {
  await import('./DemoScenarioRuntime')
}, 30_000)

afterEach(() => {
  mounts.count = 0
  window.localStorage.clear()
})

describe('LazyDemoScenarioProvider', () => {
  it('stays inert for a project that is not a demo', () => {
    renderProvider(project({ is_demo: false }))

    expect(screen.getByTestId('available')).toHaveTextContent('false')
    expect(screen.getByTestId('active')).toHaveTextContent('false')
    expect(screen.getByTestId('scan-job')).toHaveTextContent('none')
    // Its actions do nothing without a scenario.
    fireEvent.click(screen.getByRole('button', { name: 'start edit-event' }))
    expect(screen.getByTestId('step')).toHaveTextContent('live-loop/run-scan')
  })

  it('shows the page at once and brings the scenario in when its model arrives', async () => {
    renderProvider(project())

    // The page does not wait for the model.
    expect(screen.getByRole('button', { name: 'start edit-event' })).toBeInTheDocument()

    // Generous: the first test to import the runtime pays for transforming it.
    expect(
      await screen.findByText('true', { selector: '[data-testid="available"]' }, { timeout: 5000 }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('active')).toHaveTextContent('true')
    expect(screen.getByTestId('step')).toHaveTextContent('live-loop/run-scan')

    fireEvent.click(screen.getByRole('button', { name: 'start edit-event' }))
    expect(await screen.findByText('edit-event/open-editor')).toBeInTheDocument()
  })

  it('does not remount the page when the model arrives', async () => {
    renderProvider(project())

    await screen.findByText('true', { selector: '[data-testid="available"]' }, { timeout: 5000 })
    expect(mounts.count).toBe(1)
  })

  it('stays inert for a demo that is still seeding', () => {
    renderProvider(project({ generation_status: 'seeding' }))

    expect(screen.getByTestId('available')).toHaveTextContent('false')
    expect(screen.getByTestId('active')).toHaveTextContent('false')
  })
})
