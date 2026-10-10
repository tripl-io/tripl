import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { ActiveProjectContext } from '@/components/active-project-context'
import type { DataSource, Project } from '@/types'
import { filterProjectDataSources, useProjectDataSources } from './useProjectDataSources'

vi.mock('@/api/dataSources', () => ({
  dataSourcesApi: { list: vi.fn() },
}))

import { dataSourcesApi } from '@/api/dataSources'

const SOURCES = [
  { id: 'demo', project_id: 'p-demo' },
  { id: 'shared', project_id: null },
  { id: 'own', project_id: 'p-1' },
] as unknown as DataSource[]

function wrapperFor(project: Project | undefined) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ActiveProjectContext.Provider value={project}>{children}</ActiveProjectContext.Provider>
    </QueryClientProvider>
  )
}

describe('filterProjectDataSources', () => {
  it("keeps workspace-wide sources and this project's own, and drops another project's", () => {
    expect(filterProjectDataSources(SOURCES, 'p-1').map(source => source.id)).toEqual(['shared', 'own'])
  })

  it('leaves the list whole outside a project', () => {
    expect(filterProjectDataSources(SOURCES, undefined)).toEqual(SOURCES)
  })
})

describe('useProjectDataSources', () => {
  it('is loading with no data until the list answers, then scopes it to the active project', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue(SOURCES)
    const { result } = renderHook(() => useProjectDataSources(), {
      wrapper: wrapperFor({ id: 'p-1' } as Project),
    })

    expect(result.current.isLoading).toBe(true)
    expect(result.current.data).toBeUndefined()
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.isLoading).toBe(false)
    expect(result.current.data?.map(source => source.id)).toEqual(['shared', 'own'])
  })

  it('hands a failure back to the caller', async () => {
    const failure = new Error('list down')
    vi.mocked(dataSourcesApi.list).mockRejectedValue(failure)
    const { result } = renderHook(() => useProjectDataSources(), {
      wrapper: wrapperFor({ id: 'p-1' } as Project),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toBe(failure)
    expect(result.current.data).toBeUndefined()
  })
})
