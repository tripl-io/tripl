import type { ReactNode } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { variablesApi } from '@/api/variables'
import { variableDriftsApi } from '@/api/variableDrifts'
import { variableOverridesApi } from '@/api/variableOverrides'
import { eventsApi } from '@/api/events'
import type { Variable } from '@/types'
import { VariablesEditDialog } from './VariablesEditDialog'
import { VariablesCreateDialog } from './VariablesCreateDialog'

vi.mock('@/api/variables', () => ({
  variablesApi: { create: vi.fn(), update: vi.fn(), values: vi.fn(), clearValues: vi.fn() },
}))
vi.mock('@/api/variableDrifts', () => ({ variableDriftsApi: { list: vi.fn(), action: vi.fn() } }))
vi.mock('@/api/variableOverrides', () => ({
  variableOverridesApi: { list: vi.fn(), upsert: vi.fn(), clearValues: vi.fn(), del: vi.fn() },
}))
vi.mock('@/api/events', () => ({ eventsApi: { list: vi.fn() } }))

const VARIABLE: Variable = {
  id: 'var-1',
  project_id: 'project-1',
  name: 'variant',
  source_name: null,
  variable_type: 'string',
  allowed_values: [],
  bindings: [],
  description: '',
} as Variable

const EXAMPLE = { binding: 'page_data.extra.variant', name: 'variant', fromProject: false }

function Location() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}</output>
}

function renderAt(dialog: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/p/demo/variables']}>
        <Routes>
          <Route path="/p/demo/variables" element={dialog} />
          <Route path="*" element={null} />
        </Routes>
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(variablesApi.values).mockResolvedValue([])
  vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [], total: 0 })
  vi.mocked(variableOverridesApi.list).mockResolvedValue([])
  vi.mocked(eventsApi.list).mockResolvedValue({ items: [] as never, total: 0 })
})

// The property's own page guarded this draft; the quick editor opened from
// every Properties row did not, and one click outside threw it away.
describe('VariablesEditDialog — unsaved definition (prelaunch)', () => {
  it('asks before Escape drops an edited definition', async () => {
    const onClose = vi.fn()
    renderAt(
      <VariablesEditDialog slug="demo" branchId={null} variable={VARIABLE} canWrite example={EXAMPLE} onClose={onClose} />,
    )
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Which arm of the test' } })
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Leave without saving?')
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(onClose).not.toHaveBeenCalled()
  })

  it('asks before "Open property page" leaves an edited definition, and goes once agreed', async () => {
    renderAt(
      <VariablesEditDialog slug="demo" branchId={null} variable={VARIABLE} canWrite example={EXAMPLE} onClose={() => {}} />,
    )
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Which arm of the test' } })
    fireEvent.click(screen.getByRole('link', { name: /Open property page/ }))
    expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/variables')
    expect(screen.getByTestId('location')).not.toHaveTextContent('var-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/\/variables\/var-1$/))
  })

  it('opens the property page at once when nothing was edited', () => {
    renderAt(
      <VariablesEditDialog slug="demo" branchId={null} variable={VARIABLE} canWrite example={EXAMPLE} onClose={() => {}} />,
    )
    fireEvent.click(screen.getByRole('link', { name: /Open property page/ }))
    expect(screen.getByTestId('location')).toHaveTextContent(/\/variables\/var-1$/)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})

describe('VariablesCreateDialog — unsaved new property (prelaunch)', () => {
  it('closes an untouched form at once and asks before Cancel drops a typed one', async () => {
    const onClose = vi.fn()
    const { unmount } = renderAt(<VariablesCreateDialog slug="demo" branchId={null} example={EXAMPLE} onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    unmount()

    renderAt(<VariablesCreateDialog slug="demo" branchId={null} example={EXAMPLE} onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'spot_id' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2))
    expect(variablesApi.create).not.toHaveBeenCalled()
  })
})
