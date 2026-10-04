import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ColumnsMenu } from './ColumnsMenu'

function renderMenu(hiddenColumns: Set<string>, offscreenColumnCount = 0) {
  return render(
    <ColumnsMenu
      open={false}
      onOpenChange={() => {}}
      tagsHidden={false}
      lastSeenHidden={false}
      fieldColumns={[]}
      metaFields={[]}
      hiddenColumns={hiddenColumns}
      offscreenColumnCount={offscreenColumnCount}
      onToggle={() => {}}
    />,
  )
}

describe('ColumnsMenu hidden-count badge', () => {
  it('labels the count clearly instead of an ambiguous "−N"', () => {
    renderMenu(new Set(['status', 'reviewed']))

    expect(screen.getByText('2 hidden')).toBeInTheDocument()
    expect(screen.queryByText('−2')).not.toBeInTheDocument()
  })

  it('omits the badge entirely when no columns are hidden', () => {
    renderMenu(new Set())

    expect(screen.queryByText(/hidden/)).not.toBeInTheDocument()
  })

  // the chip said "3 hidden" while 11 of 17 columns were actually
  // unreadable — the other 8 were merely scrolled past the right edge, and this
  // chip is the only signal that the table continues.
  it('reports columns scrolled out of the viewport alongside the toggled-off ones', () => {
    renderMenu(new Set(['status', 'reviewed']), 8)

    expect(screen.getByText('2 hidden · 8 off-screen')).toBeInTheDocument()
  })

  it('reports off-screen columns even when nothing is toggled off', () => {
    renderMenu(new Set(), 8)

    expect(screen.getByText('8 off-screen')).toBeInTheDocument()
  })
})
