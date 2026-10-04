import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { EventGroupRulesEditor } from './EventGroupRulesEditor'
import type { UiEventGroupRule } from './scanFormTypes'

function rule(index: number): UiEventGroupRule {
  return {
    _uid: `rule-${index}`,
    name: `Group ${index}`,
    condition_logic: 'all',
    conditions: [{ _uid: `cond-${index}`, field: 'event_name', pattern: `^Screen ${index}$` }],
  }
}

function Harness({ initial }: { initial: UiEventGroupRule[] }) {
  const [rules, setRules] = useState(initial)
  return <EventGroupRulesEditor rules={rules} onChange={setRules} />
}

describe('EventGroupRulesEditor — one line per rule (#247)', () => {
  it('lists saved rules closed, with their conditions on the line, and a count', () => {
    render(<Harness initial={[rule(1), rule(2)]} />)

    expect(screen.getByText('· 2')).toBeInTheDocument()
    expect(screen.getByText('event_name ~ ^Screen 1$')).toBeInTheDocument()
    // Closed: no edit inputs until the rule is opened.
    expect(screen.queryByLabelText('Group name')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Edit group rule "Group 1"' }))
    expect(screen.getByLabelText('Group name')).toHaveValue('Group 1')
  })

  it('opens a rule added here, ready to fill in', () => {
    render(<Harness initial={[rule(1)]} />)

    fireEvent.click(screen.getByRole('button', { name: 'Add group rule' }))
    expect(screen.getByPlaceholderText('e.g. button events')).toBeInTheDocument()
  })

  it('offers a filter once there are more than eight rules', () => {
    const many = Array.from({ length: 9 }, (_, index) => rule(index + 1))
    render(<Harness initial={many} />)

    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter group rules' }), {
      target: { value: 'Screen 9' },
    })
    expect(screen.getByText('Group 9')).toBeInTheDocument()
    expect(screen.queryByText('Group 1')).toBeNull()
  })

  it('keeps the rule being edited on screen once it stops matching the filter', () => {
    const many = Array.from({ length: 9 }, (_, index) => rule(index + 1))
    render(<Harness initial={many} />)

    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter group rules' }), {
      target: { value: 'Group 9' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Edit group rule "Group 9"' }))
    const name = screen.getByLabelText('Group name')
    fireEvent.change(name, { target: { value: 'Checkout' } })

    expect(screen.getByLabelText('Group name')).toHaveValue('Checkout')
    expect(screen.queryByText(/No group rule matches/)).toBeNull()
  })

  it('marks a rule the save would refuse on its closed line, filter or not', () => {
    const many = Array.from({ length: 9 }, (_, index) => rule(index + 1))
    many[0] = { ...rule(1), name: '  ' }
    render(<Harness initial={many} />)

    expect(screen.getByText('Needs a group name')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter group rules' }), {
      target: { value: 'Group 9' },
    })
    expect(screen.getByText('Needs a group name')).toBeInTheDocument()
  })

  it('has no filter for a short list', () => {
    render(<Harness initial={[rule(1)]} />)
    expect(screen.queryByRole('searchbox')).toBeNull()
  })
})
