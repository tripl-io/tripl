import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuthContextValue } from '@/components/auth-context'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import { eventTypesApi } from '@/api/eventTypes'
import { relationsApi } from '@/api/relations'
import type { EventType, EventTypeRelation } from '@/types'
import { RelationsTab } from './RelationsTab'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

vi.mock('@/api/relations', () => ({
  relationsApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), del: vi.fn() },
}))
vi.mock('@/api/eventTypes', () => ({
  eventTypesApi: { list: vi.fn() },
}))

const TYPES = [
  { id: 'et-1', name: 'purchase', display_name: 'Purchase', field_definitions: [{ id: 'f-1', name: 'user_id' }] },
  { id: 'et-2', name: 'signup', display_name: 'Signup', field_definitions: [{ id: 'f-2', name: 'user_id' }] },
] as unknown as EventType[]

const RELATION = {
  id: 'rel-1',
  source_event_type_id: 'et-1',
  target_event_type_id: 'et-2',
  source_field_id: 'f-1',
  target_field_id: 'f-2',
  relation_type: 'shared_field',
} as unknown as EventTypeRelation

function renderTab(auth: AuthContextValue | null, { seed = true }: { seed?: boolean } = {}) {
  if (seed) vi.mocked(relationsApi.list).mockResolvedValue([RELATION])
  vi.mocked(eventTypesApi.list).mockResolvedValue(TYPES)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <RelationsTab slug="demo" />
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('RelationsTab', () => {
  it('offers an editor Add and Delete', async () => {
    renderTab(authAs('member'))

    expect(
      await screen.findByRole('button', { name: 'Delete relation between purchase.user_id and signup.user_id' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New relation/ })).toBeInTheDocument()
  })

  it('offers a viewer neither, and says why once', async () => {
    renderTab(personaAuth('viewer'))

    expect(await screen.findByText('purchase.user_id')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('button', { name: /New relation/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete relation/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Edit relation/ })).not.toBeInTheDocument()
  })

  it('names the joined fields in each row and in the delete confirm', async () => {
    renderTab(authAs('member'))

    expect(await screen.findByText('purchase.user_id')).toBeInTheDocument()
    expect(screen.getByText('signup.user_id')).toBeInTheDocument()

    fireEvent.click(
      screen.getByRole('button', { name: 'Delete relation between purchase.user_id and signup.user_id' }),
    )
    const confirm = await screen.findByRole('alertdialog')
    expect(within(confirm).getByText('Remove the relation purchase.user_id → signup.user_id?')).toBeInTheDocument()
  })

  it('says a failed delete failed instead of leaving the row in silence', async () => {
    vi.mocked(relationsApi.del).mockRejectedValue(new Error('Relation is in use'))
    renderTab(authAs('member'))

    fireEvent.click(
      await screen.findByRole('button', { name: 'Delete relation between purchase.user_id and signup.user_id' }),
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Relation is in use')
  })

  it('shows a skeleton, not "No relations", while the list loads', async () => {
    vi.mocked(relationsApi.list).mockReturnValue(new Promise(() => {}))
    renderTab(authAs('member'), { seed: false })

    expect(await screen.findByLabelText('Loading relations')).toBeInTheDocument()
    expect(screen.queryByText('No relations')).not.toBeInTheDocument()
  })

  it('shows a failed load as an error with a retry, not as an empty list', async () => {
    vi.mocked(relationsApi.list).mockRejectedValue(new Error('boom'))
    renderTab(authAs('member'), { seed: false })

    expect(await screen.findByText("Couldn't load relations")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText('No relations')).not.toBeInTheDocument()
  })

  it('shows the relation type in words and the join in one cell', async () => {
    renderTab(authAs('member'))

    const source = await screen.findByText('purchase.user_id')
    expect(screen.getByText('shared field')).toBeInTheDocument()
    expect(source.closest('td')).toHaveTextContent('purchase.user_id→ to signup.user_id')
  })

  it('groups each end of a new relation and previews the join', async () => {
    renderTab(authAs('member'))
    await screen.findByText('purchase.user_id')

    fireEvent.click(screen.getByRole('button', { name: /New relation/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New relation' })

    // A field select waits for its type.
    expect(within(dialog).getByRole('combobox', { name: 'From field' })).toBeDisabled()
    expect(within(dialog).getByText('Pick a field on each side to preview the join.')).toBeInTheDocument()

    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From event type' }), { target: { value: 'et-1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From field' }), { target: { value: 'f-1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To event type' }), { target: { value: 'et-2' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To field' }), { target: { value: 'f-2' } })

    expect(within(dialog).getByText(/^Joins/)).toHaveTextContent('Joins purchase.user_id → signup.user_id')
    expect(within(dialog).getByRole('button', { name: 'Create' })).toBeEnabled()
  })

  it('edits a relation in place with the same From/To dialog', async () => {
    vi.mocked(relationsApi.update).mockResolvedValue(RELATION)
    renderTab(authAs('member'))

    fireEvent.click(
      await screen.findByRole('button', { name: 'Edit relation between purchase.user_id and signup.user_id' }),
    )
    const dialog = await screen.findByRole('dialog', { name: 'Edit relation' })

    // Seeded with the relation's ends; Save waits for a change.
    expect(within(dialog).getByRole('combobox', { name: 'From event type' })).toHaveValue('et-1')
    expect(within(dialog).getByRole('combobox', { name: 'To field' })).toHaveValue('f-2')
    const save = within(dialog).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    // Swap the ends.
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From event type' }), { target: { value: 'et-2' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From field' }), { target: { value: 'f-2' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To event type' }), { target: { value: 'et-1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To field' }), { target: { value: 'f-1' } })
    fireEvent.click(save)

    await waitFor(() => expect(relationsApi.update).toHaveBeenCalledWith(
      'demo',
      'rel-1',
      {
        source_event_type_id: 'et-2',
        target_event_type_id: 'et-1',
        source_field_id: 'f-2',
        target_field_id: 'f-1',
      },
      null,
    ))
    expect(relationsApi.create).not.toHaveBeenCalled()
  })
})
