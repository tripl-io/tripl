import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { Project } from '@/types'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { DemoWelcomePanel } from './DemoWelcomePanel'

const SLUG = 'acme'

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

function renderPanel() {
  const project = demoProject()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/p/${SLUG}/overview`]}>
        <DemoScenarioProvider project={project} pollIntervalMs={10_000}>
          <Routes>
            <Route path="/p/:slug/*" element={<DemoWelcomePanel project={project} />} />
          </Routes>
        </DemoScenarioProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

describe('DemoWelcomePanel — what Start begins', () => {
  it('says what a chapter is at every width, a phone included', () => {
    renderPanel()

    // `hidden md:block` left a phone with "Start: Run the live loop" and
    // nothing to say what a chapter was.
    const line = screen.getByText(/short chapters show tripl at work/)
    expect(line).not.toHaveClass('hidden')
    expect(screen.getByRole('button', { name: /^Start: / })).toBeInTheDocument()
  })

  it('says tap on a touch screen', () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn((query: string) => ({
        matches: query === '(pointer: coarse)',
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    )
    renderPanel()

    expect(screen.getByText(/the guide points at every tap\.$/)).toBeInTheDocument()
  })
})

describe('DemoWelcomePanel — the tour button', () => {
  it('is named as the demo bar names the same dialog', () => {
    renderPanel()

    expect(screen.getByRole('button', { name: 'Tour & chapters' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Browse chapters/ })).toBeNull()
  })
})
