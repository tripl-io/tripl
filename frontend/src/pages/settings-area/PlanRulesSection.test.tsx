import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import PlanRulesSection from './PlanRulesSection'

/**
 * Plan rules lists the gates a plan change passes in the project, all of which
 * run today and are configured elsewhere, and says where the organization's
 * own rules live. A Community build has no such page, so it names the edition
 * that has them; it never offers a control that does nothing.
 */
describe('Project · Plan rules', () => {
  const renderPage = (slug?: string) =>
    render(
      <MemoryRouter>
        <PlanRulesSection slug={slug} />
      </MemoryRouter>,
    )

  it('lists the gates that run in the project', () => {
    renderPage()

    expect(screen.getByRole('heading', { name: 'Plan rules' })).toBeInTheDocument()
    expect(screen.getByText('In this project')).toBeInTheDocument()
    expect(screen.getByText(/needs one of its owners’ approval/)).toBeInTheDocument()
    expect(screen.getByText(/Required fields and contracts/)).toBeInTheDocument()
    expect(screen.queryByText('Not built yet')).not.toBeInTheDocument()
  })

  it('renders no control at all, enabled or disabled', () => {
    renderPage()

    expect(screen.queryAllByRole('switch')).toHaveLength(0)
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('names the edition that has organization rules, in a Community build', () => {
    renderPage()

    expect(screen.getByText('Across the organization')).toBeInTheDocument()
    expect(screen.getByText('Organization rules are part of Tripl Enterprise.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Compare editions/ })).toHaveAttribute(
      'href',
      'https://docs.tripl.io/editions',
    )
  })

  it('links the merge policy for the project', () => {
    renderPage('demo')

    expect(screen.getByRole('link', { name: 'Plan branches › Merge policy' })).toHaveAttribute(
      'href',
      '/p/demo/branches',
    )
  })
})
