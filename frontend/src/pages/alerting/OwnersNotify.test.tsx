import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { OwnersNotify } from './OwnersNotify'

const OWNERS = [
  { user_id: 'u-1', name: 'anna' },
  { user_id: 'u-2', name: 'oleg' },
]

function wrap(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

describe('OwnersNotify (F07, #260)', () => {
  it('renders nothing for an unowned scope', () => {
    wrap(<OwnersNotify owners={[]} canNotify notify={vi.fn()} target="checkout" />)
    expect(screen.queryByTestId('owners-notify')).toBeNull()
  })

  it('shows the owners to a viewer without the button', () => {
    wrap(<OwnersNotify owners={OWNERS} canNotify={false} notify={vi.fn()} target="checkout" />)
    expect(screen.getByText('Owners: @anna, @oleg')).toBeInTheDocument()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('emails the owners on click and says who got it', async () => {
    const notify = vi.fn().mockResolvedValue([
      { user_id: 'u-1', name: 'anna', email: 'anna@x.io', status: 'sent' },
      { user_id: 'u-2', name: 'oleg', email: 'oleg@x.io', status: 'skipped', error: 'no email' },
    ])
    wrap(<OwnersNotify owners={OWNERS} canNotify notify={notify} target="checkout" />)

    fireEvent.click(screen.getByRole('button', { name: 'Notify owners of checkout by email' }))

    expect(await screen.findByText('Emailed anna. Not sent to oleg (no email).')).toBeInTheDocument()
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('reports a refused request', async () => {
    const notify = vi.fn().mockRejectedValue(new Error('Forbidden'))
    wrap(<OwnersNotify owners={OWNERS} canNotify notify={notify} target="checkout" />)

    fireEvent.click(screen.getByRole('button', { name: 'Notify owners of checkout by email' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not notify owners: Forbidden')
  })
})
