import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { at } from '@/test/at'
import type { ChartAnnotation } from '@/types'
import { AnnotationsCard } from './AnnotationsCard'
import type { useChartAnnotations } from './useChartAnnotations'

const BASE: ChartAnnotation = {
  id: 'a',
  project_id: 'p-1',
  scope_type: null,
  scope_ref: null,
  bucket: '2026-01-02T00:00:00Z',
  label: 'label',
  description: null,
  color: 'var(--info)',
  source: 'manual',
  url: null,
  created_by_user_id: null,
  created_at: '2026-01-01T00:00:00Z',
}

function renderCard(annotations: ChartAnnotation[]) {
  const query = {
    data: annotations,
    isError: false,
    error: null,
    refetch: () => Promise.resolve(),
  } as unknown as ReturnType<typeof useChartAnnotations>
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AnnotationsCard slug="demo" scope="event" scopeId="e-1" canWrite={false} query={query} />
    </QueryClientProvider>,
  )
}

describe('AnnotationsCard sources (#256)', () => {
  it('names release and API annotations and links their URL in a new tab', () => {
    renderCard([
      { ...BASE, id: 'm', label: 'Hotfix deploy' },
      { ...BASE, id: 'r', label: 'Release 1.4.0', source: 'release' },
      { ...BASE, id: 'x', label: 'Deploy #512', source: 'api', url: 'https://ci.example.com/runs/512' },
    ])

    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(3)
    const manualRow = at(items, 0)
    const releaseRow = at(items, 1)
    const apiRow = at(items, 2)
    expect(within(manualRow).queryByText('Release')).not.toBeInTheDocument()
    expect(within(manualRow).queryByRole('link')).not.toBeInTheDocument()
    expect(within(releaseRow).getByText('Release', { selector: '[data-slot="chip"]' })).toBeInTheDocument()
    expect(within(apiRow).getByText('API')).toBeInTheDocument()

    const link = within(apiRow).getByRole('link', { name: /Details for Deploy #512/ })
    expect(link).toHaveAttribute('href', 'https://ci.example.com/runs/512')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
  })

  it('never links a URL that is not http or https', () => {
    renderCard([{ ...BASE, id: 'x', label: 'Deploy', source: 'api', url: 'javascript:alert(1)' }])
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})
