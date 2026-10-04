import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import NotFoundPage from './NotFoundPage'

/**
 * `seedProjects` stands in for the shell: Layout fetches `['projects']` and
 * holds every child until it settles, so under a project route the page finds
 * the list already in cache. Only slug/name are read here.
 */
function renderNotFound(path: string, { seedProjects }: { seedProjects: boolean }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (seedProjects) {
    queryClient.setQueryData(['projects'], [{ slug: 'demo', name: 'Demo Project' }])
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/p/:slug/*" element={<NotFoundPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('NotFoundPage exits', () => {
  it('offers the project it is standing in, not only the portfolio', () => {
    // A mistyped sub-path keeps the project's sidebar and breadcrumb, so
    // "Back to all projects" as the only way out cost two navigations to get
    // back where the reader already was.
    renderNotFound('/p/demo/this-route-does-not-exist', { seedProjects: true })

    expect(screen.getByRole('link', { name: 'Back to Demo Project' })).toHaveAttribute(
      'href',
      '/p/demo/overview',
    )
    expect(screen.getByRole('link', { name: 'All projects' })).toHaveAttribute(
      'href',
      '/workspace',
    )
  })

  it('never invents the project from the slug when the list does not know it', () => {
    // OverviewPage renders this page directly when the project endpoint 404s,
    // so an unknown slug must not become a button pointing at a project that
    // does not exist.
    renderNotFound('/p/does-not-exist/overview', { seedProjects: false })

    expect(screen.getByRole('link', { name: 'Back to all projects' })).toHaveAttribute(
      'href',
      '/workspace',
    )
    expect(screen.queryByRole('link', { name: /back to does-not-exist/i })).toBeNull()
  })

  it('keeps the single portfolio exit on a path with no project in scope', () => {
    renderNotFound('/totally-unknown', { seedProjects: true })

    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.getByRole('link', { name: 'Back to all projects' })).toBeInTheDocument()
  })

  it('reads the list the shell already holds instead of asking for it again', () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(() => Promise.reject(new Error('not-found page must not fetch')))

    renderNotFound('/p/demo/this-route-does-not-exist', { seedProjects: true })

    expect(screen.getByRole('link', { name: 'Back to Demo Project' })).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
