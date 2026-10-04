import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { Field, InfoRow, Panel } from '@/components/settings/kit'
import { EvField } from '@/pages/events/eventFormLayout'
import { SField } from '@/pages/settings/EventTypesTab'
import { FormRow } from './form-row'

// jsdom applies no Tailwind, so these pin the STRUCTURE the stacking layout
// hangs on — every labelled form row goes through the one shared primitive,
// whose classes stack below 560px of row width — rather than the class strings
// themselves.
describe('FormRow', () => {
  it('renders a caption column and a control column, with the caption width once side by side', () => {
    const { container } = render(
      <FormRow labelWidth={200} caption={<label htmlFor="x">Name</label>}>
        <input id="x" />
      </FormRow>,
    )

    const row = container.querySelector('[data-slot="form-row"]') as HTMLElement
    expect(row.style.getPropertyValue('--form-row-label')).toBe('200px')
    expect(row.querySelector('[data-slot="form-row-caption"]')).toHaveTextContent('Name')
    expect(row.querySelector('[data-slot="form-row-control"]')).toContainElement(
      screen.getByLabelText('Name'),
    )
  })

  it('stacks on its own width, not the viewport', () => {
    const { container } = render(
      <FormRow caption={<span>Name</span>} className="px-4" role="group" aria-label="Name row">
        <input aria-label="Name" />
      </FormRow>,
    )
    const row = container.querySelector('[data-slot="form-row"]') as HTMLElement
    // The row is the query container and keeps the caller's ARIA; the caller's
    // layout classes land on the flex row inside it.
    expect(row).toHaveClass('@container')
    expect(row).toHaveAttribute('role', 'group')
    const flex = row.firstElementChild as HTMLElement
    expect(flex).toHaveClass('px-4', 'flex-col', '@min-[560px]:flex-row')
    expect(flex.className).not.toMatch(/(^|\s)sm:/)
  })
})

describe('forms that used a fixed-width caption now stack on phones', () => {
  const cases: [string, ReactNode][] = [
    ['event form row', <EvField label="Name" htmlFor="c"><input id="c" /></EvField>],
    ['event-type form row', <SField label="Name"><input aria-label="Name" /></SField>],
    ['settings kit row', <Field label="Name" htmlFor="c"><input id="c" /></Field>],
  ]

  it.each(cases)('the %s is a shared FormRow', (_name, element) => {
    render(<>{element}</>)
    expect(screen.getByLabelText('Name').closest('[data-slot="form-row"]')).not.toBeNull()
  })

  it('stacks the read-only InfoRow too, and titles a value that may truncate', () => {
    render(<InfoRow label="Scan" value="A very long scan name that truncates" mono={false} />)
    const value = screen.getByText('A very long scan name that truncates')
    expect(value.closest('[data-slot="form-row"]')).not.toBeNull()
    expect(value).toHaveAttribute('title', 'A very long scan name that truncates')
  })
})

describe('panel bodies scroll sideways instead of clipping a wide table', () => {
  const table = (
    <table aria-label="Wide">
      <tbody>
        <tr>
          <td>cell</td>
        </tr>
      </tbody>
    </table>
  )

  it.each([
    // The one Panel: the scan and event-type SurfPanel copies are gone.
    ['settings kit Panel', <Panel title="P">{table}</Panel>],
    ['settings kit Panel with no header', <Panel>{table}</Panel>],
  ])('the %s wraps its body in the scrolling panel body', (_name, element) => {
    render(<>{element}</>)
    expect(screen.getByRole('table', { name: 'Wide' }).closest('[data-slot="panel-body"]')).not.toBeNull()
  })
})
