import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { Variable } from '@/types'
import { VariablesTableRow } from './VariablesTableRow'

// The list-row shape VariablesTab.test.tsx seeds: everything the row draws
// arrives on the list response, so a fixture is a plain Variable.
function makeVariable(overrides: Partial<Variable> & { id: string; name: string }): Variable {
  return {
    project_id: 'project-1',
    source_name: null,
    variable_type: 'string',
    allowed_values: [],
    bindings: [],
    description: '',
    ...overrides,
  }
}

/**
 * A variable whose OTHER em-dash-capable cells all speak.
 *
 * Three columns render a bare em-dash when they have nothing to say. Filling the
 * events and documented-values columns leaves the Observed values cell as the
 * only one that can, so a single `getByText('—')` — which throws on a second
 * match — is a claim about that cell and no other.
 */
function makeSpeakingVariable(overrides: Partial<Variable>): Variable {
  return makeVariable({
    id: 'var-1',
    name: 'variant',
    event_names: ['checkout_completed'],
    event_count: 1,
    allowed_values: ['documented'],
    ...overrides,
  })
}

function renderRow(variable: Variable) {
  // memo() and <td>s both: the row must be mounted in a real table body or the
  // cells are invalid DOM and React warns.
  return render(
    <table>
      <tbody>
        <VariablesTableRow
          variable={variable}
          typeLabel="string"
          selected={false}
          focused={false}
          onToggleSelect={() => {}}
          onEdit={() => {}}
          onExclude={() => {}}
          onDelete={() => {}}
        />
      </tbody>
    </table>,
  )
}

// two unrelated silences used to print the same em-dash — nothing
// references the variable at all, versus every context that does came back
// empty. Only the second is a fact about the scan, and only the second is worth
// an operator's attention.
describe('VariablesTableRow observed values cell', () => {
  it('renders a chip per observed value', () => {
    renderRow(makeSpeakingVariable({ sample_values: ['/checkout', '/cart'], context_count: 2 }))

    expect(screen.getByText('/checkout')).toBeInTheDocument()
    expect(screen.getByText('/cart')).toBeInTheDocument()
    expect(screen.queryByText('No values stored')).not.toBeInTheDocument()
  })

  it('names the silence when the property has contexts but no stored values', () => {
    renderRow(makeSpeakingVariable({ sample_values: [], context_count: 2 }))

    expect(screen.getByText('No values stored')).toHaveAttribute(
      'title',
      '2 value contexts, none holding a value',
    )
    expect(screen.queryByText('—')).not.toBeInTheDocument()
  })

  it('counts a lone value context in the singular', () => {
    renderRow(makeSpeakingVariable({ sample_values: [], context_count: 1 }))

    expect(screen.getByText('No values stored')).toHaveAttribute(
      'title',
      '1 value context, none holding a value',
    )
  })

  it('keeps the em-dash when no context references the property at all', () => {
    renderRow(makeSpeakingVariable({ sample_values: [], context_count: 0 }))

    expect(screen.queryByText('No values stored')).not.toBeInTheDocument()
    // The other two dash-capable cells are populated, so this dash is the
    // Observed values cell's.
    expect(screen.getByText('documented')).toBeInTheDocument()
    expect(screen.getByText('checkout_completed')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

describe('VariablesTableRow observed-in events', () => {
  it('links each event name to its event when ids and a route are given', () => {
    render(
      <MemoryRouter>
        <table>
          <tbody>
            <VariablesTableRow
              variable={makeSpeakingVariable({
                event_refs: [{ id: 'event-1', name: 'checkout_completed' }],
              })}
              typeLabel="string"
              selected={false}
              focused={false}
              onToggleSelect={() => {}}
              onEdit={() => {}}
              onExclude={() => {}}
              onDelete={() => {}}
              eventHref={id => `/p/demo/events/all/${id}`}
            />
          </tbody>
        </table>
      </MemoryRouter>,
    )

    expect(screen.getByRole('link', { name: 'checkout_completed' })).toHaveAttribute(
      'href',
      '/p/demo/events/all/event-1',
    )
  })

  it('keeps plain names without a route', () => {
    renderRow(makeSpeakingVariable({ event_refs: [{ id: 'event-1', name: 'checkout_completed' }] }))

    expect(screen.getByText('checkout_completed')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'checkout_completed' })).toBeNull()
  })
})

describe('VariablesTableRow property lists (F23)', () => {
  it('says how many events list the property, linked to its Events tab', () => {
    render(
      <MemoryRouter>
        <table>
          <tbody>
            <VariablesTableRow
              variable={makeSpeakingVariable({ listed_event_count: 3, required_event_count: 1 })}
              typeLabel="string"
              selected={false}
              focused={false}
              onToggleSelect={() => {}}
              onEdit={() => {}}
              onExclude={() => {}}
              onDelete={() => {}}
              listedEventsHref={(id) => `/variables/${id}?tab=events`}
            />
          </tbody>
        </table>
      </MemoryRouter>,
    )
    const link = screen.getByRole('link', { name: 'On 3 events · 1 required' })
    expect(link).toHaveAttribute('href', '/variables/var-1?tab=events')
  })

  it('says nothing when no event lists it', () => {
    renderRow(makeSpeakingVariable({ listed_event_count: 0 }))
    expect(screen.queryByText(/required$/)).toBeNull()
  })
})

// A phone hides Observed in, Description and both values columns (the table
// head carries `hidden md:table-cell`), so the name cell says them instead of
// a desktop table scrolled under the pinned actions.
describe('VariablesTableRow on a phone', () => {
  it('folds the description and what scans saw under the name', () => {
    renderRow(
      makeSpeakingVariable({
        description: 'Client platform the event was sent from.',
        sample_values: ['ios', 'android', 'web', 'tv'],
      }),
    )

    const phoneLine = screen.getByText('Seen in 1 event · values: ios, android, web +1')
    expect(phoneLine).toHaveClass('md:hidden')
    // The description shows once per layout: the phone line and the wide column.
    const descriptions = screen.getAllByText('Client platform the event was sent from.')
    expect(descriptions).toHaveLength(2)
    expect(descriptions.map((node) => node.tagName)).toEqual(['P', 'TD'])
    expect(descriptions[0]).toHaveClass('md:hidden')
    expect(descriptions[1]).toHaveClass('hidden', 'md:table-cell')
  })

  it('names the values alone when no event is known, and says nothing when scans saw nothing', () => {
    const { unmount } = renderRow(makeVariable({ id: 'var-2', name: 'build', sample_values: ['a', 'b'] }))
    expect(screen.getByText('Values: a, b')).toBeInTheDocument()
    unmount()

    renderRow(makeVariable({ id: 'var-3', name: 'unused' }))
    expect(screen.queryByText(/^(Seen in|Values:)/)).toBeNull()
  })

  it('keeps the pinned actions on the first line, with an edge over what they cover', () => {
    renderRow(makeSpeakingVariable({}))
    const actions = screen.getByRole('button', { name: 'Edit property variant' }).closest('td')
    expect(actions).toHaveClass('sticky', 'right-0', 'align-top')
    expect(actions?.className).toMatch(/shadow-\[/)
  })
})
