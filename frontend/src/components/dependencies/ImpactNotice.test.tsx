import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, type ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConfirm, type ConfirmOptions } from '@/hooks/useConfirm'
import type { DependencyEdge, ImpactResponse } from '@/types'
import { ConfirmImpactMessage, ImpactNotice } from './ImpactNotice'

vi.mock('@/api/dependencies', () => ({
  dependenciesApi: { get: vi.fn(), impact: vi.fn(), branchImpact: vi.fn() },
}))

import { dependenciesApi } from '@/api/dependencies'

function edge(overrides: Partial<DependencyEdge>): DependencyEdge {
  return {
    kind: 'metric',
    id: 'm-1',
    name: 'Checkout rate',
    relation: 'metric uses event in its composition',
    certainty: 'direct',
    url_hint: null,
    ...overrides,
  }
}

function wrap(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Mounts the hook and hands its `confirm` to the test on every render. */
function Harness({ onConfirm }: { onConfirm: (confirm: (o: ConfirmOptions) => Promise<boolean>) => void }) {
  const { confirm, dialog } = useConfirm()
  useEffect(() => {
    onConfirm(confirm)
  })
  return <>{dialog}</>
}

const DELETE_E1 = [{ kind: 'event' as const, id: 'e-1', change: 'delete' as const }]

describe('ImpactNotice (#257)', () => {
  beforeEach(() => {
    vi.mocked(dependenciesApi.impact).mockReset()
  })

  it("names the dependents with the server's sentence", async () => {
    const answer: ImpactResponse = {
      items: [
        {
          change: DELETE_E1[0]!,
          affected: [edge({}), edge({ id: 'm-2', name: 'Revenue' }), edge({ kind: 'alert_rule', id: 'a-1', name: 'Drop' })],
          summary: '2 metrics and 1 alert rule',
        },
      ],
    }
    vi.mocked(dependenciesApi.impact).mockResolvedValue(answer)
    wrap(<ImpactNotice slug="demo" branchId={null} changes={DELETE_E1} />)

    const notice = await screen.findByTestId('impact-notice')
    expect(notice).toHaveTextContent('This affects 2 metrics and 1 alert rule.')
    expect(notice).toHaveTextContent('They are not changed and may stop working as expected.')
    expect(within(notice).getByText('Revenue')).toBeInTheDocument()
    // Only the one-line summary is a live region, not the whole list.
    expect(notice).not.toHaveAttribute('role')
    const live = within(notice).getByRole('status')
    expect(live).toHaveTextContent('This affects 2 metrics and 1 alert rule.')
    expect(live).not.toHaveTextContent('Revenue')
    // Compact rows drop a direct edge's relation sentence.
    expect(within(notice).queryByText('metric uses event in its composition')).toBeNull()
    // No links inside a dialog: following one would strand it.
    expect(within(notice).queryByRole('link')).toBeNull()
    expect(dependenciesApi.impact).toHaveBeenCalledWith('demo', DELETE_E1, null, expect.anything())
  })

  it('counts a dependent shared by several changes once', async () => {
    const shared = edge({ kind: 'alert_rule', id: 'a-1', name: 'Drop' })
    vi.mocked(dependenciesApi.impact).mockResolvedValue({
      items: [
        { change: { kind: 'event', id: 'e-1', change: 'delete' }, affected: [shared], summary: '1 alert rule' },
        { change: { kind: 'event', id: 'e-2', change: 'delete' }, affected: [shared, edge({})], summary: '1 metric and 1 alert rule' },
      ],
    })
    wrap(
      <ImpactNotice
        slug="demo"
        branchId={null}
        changes={[...DELETE_E1, { kind: 'event', id: 'e-2', change: 'delete' }]}
      />,
    )
    expect(await screen.findByTestId('impact-notice')).toHaveTextContent('This affects 1 metric and 1 alert rule.')
  })

  it('keeps the relation sentence of a possible edge in compact rows', async () => {
    vi.mocked(dependenciesApi.impact).mockResolvedValue({
      items: [
        {
          change: DELETE_E1[0]!,
          affected: [edge({ certainty: 'possible', relation: 'fact table SQL names the column' })],
          summary: '1 possible metric',
        },
      ],
    })
    wrap(<ImpactNotice slug="demo" branchId={null} changes={DELETE_E1} />)
    const notice = await screen.findByTestId('impact-notice')
    expect(within(notice).getByText('fact table SQL names the column')).toBeInTheDocument()
    expect(
      within(notice).getByRole('button', { name: /Possible: matched by name, not by a stored reference/ }),
    ).toBeInTheDocument()
  })

  it('does not promise the change goes through when dependents block it', async () => {
    const change = { kind: 'fact_table' as const, id: 'ft-1', change: 'delete' as const }
    vi.mocked(dependenciesApi.impact).mockResolvedValue({
      items: [{ change, affected: [edge({})], summary: '1 metric' }],
    })
    wrap(<ImpactNotice slug="demo" branchId={null} changes={[change]} mode="blocks" />)
    const notice = await screen.findByTestId('impact-notice')
    expect(notice).toHaveTextContent('This affects 1 metric. While they read it, the delete is refused')
    expect(notice).not.toHaveTextContent(/not changed|not blocked/)
  })

  it('says the events go with a deleted event type', async () => {
    const change = { kind: 'event_type' as const, id: 'et-1', change: 'delete' as const }
    vi.mocked(dependenciesApi.impact).mockResolvedValue({
      items: [{ change, affected: [edge({})], summary: '1 metric' }],
    })
    wrap(<ImpactNotice slug="demo" branchId={null} changes={[change]} mode="cascades" />)
    const notice = await screen.findByTestId('impact-notice')
    expect(notice).toHaveTextContent('Events of this type are deleted with it')
    expect(notice).not.toHaveTextContent(/not changed/)
  })

  it('does not say a blocked delete goes through when the check fails', async () => {
    const change = { kind: 'fact_table' as const, id: 'ft-1', change: 'delete' as const }
    vi.mocked(dependenciesApi.impact).mockRejectedValue(new Error('boom'))
    wrap(<ImpactNotice slug="demo" branchId={null} changes={[change]} mode="blocks" />)
    const note = await screen.findByText(/Could not check what depends on this/)
    expect(note).not.toHaveTextContent(/not blocked/)
  })

  it('says nothing depends on it when the answer is empty', async () => {
    vi.mocked(dependenciesApi.impact).mockResolvedValue({ items: [{ change: DELETE_E1[0]!, affected: [], summary: '' }] })
    wrap(<ImpactNotice slug="demo" branchId={null} changes={DELETE_E1} />)
    expect(await screen.findByText(/Nothing else in the project depends on this/)).toBeInTheDocument()
  })

  it('never blocks the confirm: loading and failure leave it armed', async () => {
    let fail!: (e: Error) => void
    vi.mocked(dependenciesApi.impact).mockReturnValue(new Promise((_, reject) => { fail = reject }))

    const seen: ((o: ConfirmOptions) => Promise<boolean>)[] = []
    wrap(<Harness onConfirm={(c) => seen.push(c)} />)

    let answer!: Promise<boolean>
    act(() => {
      answer = seen[seen.length - 1]!({
        title: 'Delete selected events',
        message: <ConfirmImpactMessage message="Delete 1 selected events?" slug="demo" branchId={null} changes={DELETE_E1} />,
        confirmLabel: 'Delete',
        variant: 'danger',
      })
    })
    expect(await screen.findByText('Checking what depends on this…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()

    await act(async () => {
      fail(new Error('boom'))
    })
    expect(await screen.findByText(/Could not check what depends on this/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await expect(answer).resolves.toBe(true)
  })
})
