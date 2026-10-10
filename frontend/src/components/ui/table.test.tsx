import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from './table'

describe('Table container', () => {
  it('carries the horizontal-overflow affordance on the element that scrolls', () => {
    const { container } = render(
      <Table>
        <TableBody>
          <TableRow>
            <TableCell>cell</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )

    // The wide events catalog scrolls inside this container, and its horizontal
    // scrollbar is far below the header row — `.tripl-scroll-x` paints the edge
    // fade that says "there is more to the right".
    const scroller = container.querySelector('[data-slot="table-container"]')
    expect(scroller).not.toBeNull()
    expect(scroller!.classList.contains('tripl-scroll-x')).toBe(true)
    expect(scroller!.classList.contains('overflow-x-auto')).toBe(true)
  })

  it('leaves the scrolling to an outer region when asked', () => {
    const { container } = render(
      <Table scroll={false}>
        <TableBody>
          <TableRow>
            <TableCell>cell</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )

    // A table inside its own focusable scroll region (the alert replay) would
    // otherwise nest a second scroller the keyboard cannot reach.
    const wrapper = container.querySelector('[data-slot="table-container"]')
    expect(wrapper!.classList.contains('tripl-scroll-x')).toBe(false)
    expect(wrapper!.classList.contains('overflow-x-auto')).toBe(false)
  })
})

describe('Table density', () => {
  it('sizes rows and cell gutters from the density tokens', () => {
    const { container } = render(
      <Table>
        <TableBody>
          <TableRow>
            <TableCell>cell</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )
    expect(container.querySelector('[data-slot="table-row"]')).toHaveClass('h-(--row-h)')
    expect(container.querySelector('[data-slot="table-cell"]')).toHaveClass('px-(--cell-px)')
  })
})

describe('Table header', () => {
  it('passes its uppercase caption on to a sort button inside it', () => {
    const { container } = render(
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>
              <button type="button">Health</button>
            </TableHead>
          </TableRow>
        </TableHeader>
      </Table>,
    )
    // The browser's stylesheet resets text-transform on a <button>; without
    // this a sortable column read "Health" among "VOLUME" and "OWNER".
    expect(container.querySelector('[data-slot="table-head"]')).toHaveClass(
      'micro-label',
      '[&_button]:[text-transform:inherit]',
    )
  })
})
