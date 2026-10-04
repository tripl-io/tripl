import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import PlanRulesSection from './PlanRulesSection'

/**
 * Nothing on Plan rules is wired to anything. The page first rendered every
 * control live and pre-set to a governed state, then as dozens of
 * disabled controls set to "off" (WS-37). Neither was a settings page: it is
 * now one "Coming later" card that describes the rules and offers no control
 * that could be read as a setting.
 */
describe('Project · Plan rules states that none of it is built', () => {
  it('says so in the header', () => {
    render(<PlanRulesSection />)

    expect(screen.getByRole('heading', { name: 'Plan rules' })).toBeInTheDocument()
    expect(screen.getByText('Not built yet')).toBeInTheDocument()
    expect(screen.getByText(/None of them run today/i)).toBeInTheDocument()
  })

  it('renders no control at all, enabled or disabled', () => {
    render(<PlanRulesSection />)

    expect(screen.queryAllByRole('switch')).toHaveLength(0)
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    // A Save with nothing behind it argues the page holds settings.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('describes each planned group of rules in one "Coming later" card', () => {
    render(<PlanRulesSection />)

    expect(screen.getByText('Coming later')).toBeInTheDocument()
    for (const group of ['Naming conventions', 'Governance', 'PII & compliance']) {
      expect(screen.getByRole('region', { name: group })).toBeInTheDocument()
    }
    // The approval gate that exists today is the merge policy, not a plan
    // rule; the planned list no longer duplicates it (PL-26).
    expect(screen.queryByText(/Require an approval/)).not.toBeInTheDocument()
    // Written as what the rule would do, never as a policy in force.
    expect(screen.queryByDisplayValue('1 approval')).not.toBeInTheDocument()
    expect(screen.queryByDisplayValue('90 days')).not.toBeInTheDocument()
  })
})

describe('Project · Plan rules points at what exists today (PL-26)', () => {
  it('links the merge policy for the project', () => {
    render(
      <MemoryRouter>
        <PlanRulesSection slug="demo" />
      </MemoryRouter>,
    )

    expect(screen.getByRole('link', { name: 'Plan branches › Merge policy' })).toHaveAttribute(
      'href',
      '/p/demo/branches',
    )
  })
})
