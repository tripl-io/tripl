import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { OffsetPager } from './offset-pager'

function renderPager(paging: { hasPrev: boolean; hasNext: boolean; isPaging: boolean }) {
  const onPrev = vi.fn()
  const onNext = vi.fn()
  render(
    <OffsetPager
      label="Audit log pages"
      paging={paging}
      caption="Showing 51–100 of 254 entries."
      prevLabel="Newer"
      nextLabel="Older"
      onPrev={onPrev}
      onNext={onNext}
    />,
  )
  return { onPrev, onNext }
}

describe('OffsetPager', () => {
  it('names itself, says the range and steps either way', () => {
    const { onPrev, onNext } = renderPager({ hasPrev: true, hasNext: true, isPaging: false })

    expect(screen.getByRole('navigation', { name: 'Audit log pages' })).toHaveTextContent(
      'Showing 51–100 of 254 entries.',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Newer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Older' }))
    expect(onPrev).toHaveBeenCalledTimes(1)
    expect(onNext).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Updating…')).toBeNull()
  })

  it('shuts the way past either end', () => {
    renderPager({ hasPrev: false, hasNext: true, isPaging: false })
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Older' })).toBeEnabled()
  })

  // A second click while a page is in flight moved the query key again and
  // the page in flight was never shown.
  it('holds both buttons shut and says so while a page is in flight', () => {
    renderPager({ hasPrev: true, hasNext: true, isPaging: true })
    expect(screen.getByText('Updating…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Older' })).toBeDisabled()
  })
})
