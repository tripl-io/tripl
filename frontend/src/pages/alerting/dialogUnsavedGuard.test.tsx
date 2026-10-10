import type { ReactNode } from 'react'
import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import type { AlertDestination } from '@/types'

import { defaultRuleForm } from './constants'
import { DestinationDialog } from './DestinationDialog'
import { RuleEditorDialog } from './RuleEditorDialog'

/**
 * The rule and destination dialogs hand their unsaved state to <Dialog dirty>
 * instead of wiring a guard of their own: Cancel, Escape and the X all go
 * through the one prompt, and a clean form still closes at once.
 */

function withProviders(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

const DESTINATION = {
  id: 'dest-1',
  project_id: 'proj-1',
  type: 'telegram',
  name: 'TG',
  held_count: 0,
  enabled: true,
  webhook_set: false,
  bot_token_set: true,
  chat_id: '-100',
  rules: [],
  delivery_count: 0,
  incident_count: 0,
  is_local: false,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
} as unknown as AlertDestination

function RuleHarness({ onClose }: { onClose: () => void }) {
  const [ruleForm, setRuleForm] = useState({ ...defaultRuleForm(), name: 'Checkout drops' })
  const [destinationId, setDestinationId] = useState('dest-1')
  return (
    <RuleEditorDialog
      open
      onClose={onClose}
      slug="demo"
      destinations={[DESTINATION]}
      destinationId={destinationId}
      onDestinationIdChange={setDestinationId}
      guidedStep={false}
      isEditing={false}
      ruleForm={ruleForm}
      setRuleForm={setRuleForm}
      eventTypes={[]}
      scans={[]}
      onSubmit={() => {}}
      isPending={false}
      isError={false}
      error={null}
    />
  )
}

describe('RuleEditorDialog — unsaved rule', () => {
  it('closes an untouched form at once', () => {
    const onClose = vi.fn()
    withProviders(<RuleHarness onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('asks before Cancel drops an edited rule, and closes once agreed', async () => {
    const onClose = vi.fn()
    withProviders(<RuleHarness onClose={onClose} />)

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Checkout drops, EU' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Leave without saving?')
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })
})

describe('DestinationDialog — unsaved destination', () => {
  function renderDestination(onClose: () => void) {
    return withProviders(
      <DestinationDialog
        slug="demo"
        target={{ mode: 'create', type: 'slack', handOffToRule: false }}
        project={{ timezone: 'UTC' }}
        isDemo={false}
        onClose={onClose}
        onCreated={vi.fn()}
      />,
    )
  }

  it('closes an untouched form at once', () => {
    const onClose = vi.fn()
    renderDestination(onClose)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('asks before Cancel drops a typed-in destination', async () => {
    const onClose = vi.fn()
    renderDestination(onClose)

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Ops Slack' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Leave without saving?')
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })
})
