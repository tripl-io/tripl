// @vitest-environment jsdom
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { HealthBreakdown, HealthPopover } from './health-breakdown'
import { RENORMALIZED_HEALTH, healthComponent } from './healthFixtures'
import type { EventHealth } from '@/types/health'

function rowFor(key: string): HTMLElement {
  const row = document.querySelector(`tr[data-component="${key}"]`)
  if (!(row instanceof HTMLElement)) throw new Error(`no row for ${key}`)
  return row
}

describe('HealthBreakdown', () => {
  it('lists every component in a labelled table, applicable ones first', () => {
    render(<HealthBreakdown health={RENORMALIZED_HEALTH} />)
    const table = screen.getByRole('table', { name: /Health score breakdown for checkout_completed/ })
    const rowHeaders = within(table).getAllByRole('rowheader').map((cell) => cell.textContent)
    expect(rowHeaders).toEqual([
      'Implemented & seen',
      'Contract',
      'Documentation',
      'Drifts',
      'Signals',
      'Freshness',
    ])
  })

  it('shows the fixed weight, the rescaled weight, the value, the points and the reason', () => {
    render(<HealthBreakdown health={RENORMALIZED_HEALTH} />)
    const contract = rowFor('contract')
    expect(contract).toHaveTextContent('20%')
    expect(contract).toHaveTextContent('33.3%')
    expect(contract).toHaveTextContent('rescaled to')
    expect(contract).toHaveTextContent('78%')
    expect(contract).toHaveTextContent('25.9')
    expect(contract).toHaveTextContent('2 of 9 contract rules failing: amount (range), plan (enum)')
  })

  it('greys out an excluded component with its reason', () => {
    render(<HealthBreakdown health={RENORMALIZED_HEALTH} />)
    const drifts = rowFor('drifts')
    expect(drifts).toHaveAttribute('data-excluded', 'true')
    expect(drifts).toHaveTextContent('Not covered by any scan')
    expect(drifts).toHaveTextContent('(excluded)')
    expect(rowFor('freshness')).toHaveTextContent('No scheduled source')
    expect(rowFor('signals')).toHaveTextContent('Anomaly detection is off for its scans')
  })

  it('says the weights were renormalized over the applicable components', () => {
    render(<HealthBreakdown health={RENORMALIZED_HEALTH} />)
    expect(screen.getByTestId('health-renormalized')).toHaveTextContent(
      'Weights renormalized over 3 applicable components; 3 excluded.',
    )
  })

  it('has no footnote when every component applies', () => {
    const full: EventHealth = {
      ...RENORMALIZED_HEALTH,
      renormalized: false,
      excluded: [],
      components: RENORMALIZED_HEALTH.components.map((c) =>
        healthComponent({ key: c.key, label: c.label, weight: c.weight, effective_weight: c.weight }),
      ),
    }
    render(<HealthBreakdown health={full} />)
    expect(screen.queryByTestId('health-renormalized')).not.toBeInTheDocument()
    // The weight is not repeated as "25% → 25%".
    expect(rowFor('implemented_seen')).not.toHaveTextContent('rescaled to')
  })

  it('shows the score and grade on top unless the host hides them', () => {
    const { rerender } = render(<HealthBreakdown health={RENORMALIZED_HEALTH} />)
    expect(screen.getByText('Needs attention')).toBeInTheDocument()
    rerender(<HealthBreakdown health={RENORMALIZED_HEALTH} hideSummary />)
    expect(screen.queryByText('Needs attention')).not.toBeInTheDocument()
  })
})

describe('HealthPopover', () => {
  it('opens the breakdown from a keyboard-reachable button and closes on Escape', async () => {
    render(<HealthPopover health={RENORMALIZED_HEALTH} />)
    const trigger = screen.getByRole('button', { name: 'Health 71 of 100. Show breakdown' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(trigger).toHaveTextContent('71')

    trigger.focus()
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    const dialog = await screen.findByRole('dialog', { name: 'Health breakdown for checkout_completed' })
    expect(within(dialog).getByRole('table')).toBeInTheDocument()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })
})
