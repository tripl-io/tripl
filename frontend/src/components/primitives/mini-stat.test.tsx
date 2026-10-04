import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MiniStat, MiniStatStrip } from './mini-stat'

// `tone` with no delta used to render nothing, while the call sites
// (Overview's Implemented / Needs review / Coverage, the Coverage page) read as
// if their figures were coloured.
describe('MiniStat tone', () => {
  it('tints the figure when there is no delta to carry the tone', () => {
    render(<MiniStat label="Coverage" value="92%" tone="success" />)
    expect(screen.getByText('92%')).toHaveAttribute('data-tone', 'success')
  })

  it('keeps the figure plain and tones the delta when there is one', () => {
    render(<MiniStat label="Active scopes" value="4" delta="1 firing" tone="danger" />)
    expect(screen.getByText('4')).not.toHaveAttribute('data-tone')
    expect(screen.getByText('1 firing')).toHaveStyle({ color: 'var(--danger)' })
  })

  it('lets valueTone colour the figure beside a delta', () => {
    render(<MiniStat label="Spikes" value="3" delta="+1" tone="neutral" valueTone="danger" />)
    expect(screen.getByText('3')).toHaveAttribute('data-tone', 'danger')
  })

  it('never tints a neutral figure', () => {
    render(<MiniStat label="Events" value="12" tone="neutral" />)
    expect(screen.getByText('12')).not.toHaveAttribute('data-tone')
  })
})

// the divider was a sibling element, so a wrapped row ended with one.
describe('MiniStatStrip', () => {
  it('gives every stat but the first its own divider', () => {
    const { container } = render(
      <MiniStatStrip>
        <MiniStat label="A" value="1" />
        {false}
        <MiniStat label="B" value="2" />
        {null}
        <MiniStat label="C" value="3" />
      </MiniStatStrip>,
    )
    // Three stats, two dividers; skipped children leave no stray divider.
    expect(screen.getAllByRole('term')).toHaveLength(3)
    expect(container.querySelectorAll('[data-slot="mini-stat-divider"]')).toHaveLength(2)
    for (const divider of container.querySelectorAll('[data-slot="mini-stat-divider"]')) {
      expect(divider).toHaveAttribute('aria-hidden', 'true')
    }
  })

  it('applies the caller box styling to the outer element', () => {
    const { container } = render(
      <MiniStatStrip className="rounded-lg border" style={{ background: 'var(--bg-sunken)' }}>
        <MiniStat label="A" value="1" />
      </MiniStatStrip>,
    )
    expect(container.firstElementChild).toHaveStyle({ background: 'var(--bg-sunken)' })
    expect(container.querySelector('[data-slot="mini-stat-divider"]')).toBeNull()
  })

  // A vertical clip cut the top and bottom of a filter toggle's focus ring.
  it('clips the row-start dividers sideways only, never vertically', () => {
    const { container } = render(
      <MiniStatStrip>
        <MiniStat label="A" value="1" />
        <MiniStat label="B" value="2" />
      </MiniStatStrip>,
    )
    const clip = container.querySelector<HTMLElement>('[data-slot="mini-stat-clip"]')
    expect(clip).not.toBeNull()
    expect(clip?.style.overflowX).toBe('clip')
    expect(clip?.style.overflowY).toBe('visible')
    expect(clip?.className).not.toMatch(/overflow-(hidden|clip|auto|scroll)|overflow-y-/)
  })
})

// an info icon placed beside the whole stat sat far from its caption.
describe('MiniStat labelAddon', () => {
  it('renders the addon inside the caption', () => {
    render(
      <MiniStat label="Chart signals" value="3" labelAddon={<button type="button" aria-label="About chart signals" />} />,
    )
    const caption = screen.getByRole('term')
    expect(caption).toHaveTextContent('Chart signals')
    expect(caption).toContainElement(screen.getByRole('button', { name: 'About chart signals' }))
  })
})

// a KPI figure is a number, not code.
describe('MiniStat figure', () => {
  it('sets the value in sans with tabular digits, never mono', () => {
    render(<MiniStat label="Last run" value="1h ago" />)
    const value = screen.getByText('1h ago')
    expect(value).toHaveClass('tnum')
    expect(value).not.toHaveClass('mono')
    expect(value).not.toHaveClass('font-mono')
  })
})

// one page-KPI container instead of each page spelling it out.
describe('MiniStatStrip boxed', () => {
  it('draws the sunken card box when boxed', () => {
    const { container } = render(
      <MiniStatStrip boxed>
        <MiniStat label="A" value="1" />
      </MiniStatStrip>,
    )
    const strip = container.querySelector('[data-slot="mini-stat-strip"]')
    expect(strip).toHaveClass('rounded-card', 'border', 'bg-bg-sunken', 'px-4', 'py-3')
  })

  it('draws no box by default', () => {
    const { container } = render(
      <MiniStatStrip>
        <MiniStat label="A" value="1" />
      </MiniStatStrip>,
    )
    const strip = container.querySelector('[data-slot="mini-stat-strip"]')
    expect(strip).not.toHaveClass('rounded-card')
    expect(strip).not.toHaveClass('bg-bg-sunken')
  })
})

// Four stats as a 2x2 grid on a phone, not a row that orphans
// the last one with its own divider.
describe('MiniStatStrip phoneGrid', () => {
  it('lays the row out as a two-column grid below sm and hides the dividers there', () => {
    const { container } = render(
      <MiniStatStrip phoneGrid>
        <MiniStat label="A" value="1" />
        <MiniStat label="B" value="2" />
      </MiniStatStrip>,
    )
    const row = container.querySelector('[data-phone-grid]')
    expect(row).toHaveClass('grid', 'grid-cols-2', 'sm:flex')
    const divider = container.querySelector('[data-slot="mini-stat-divider"]')
    expect(divider).toHaveClass('max-sm:hidden')
  })

  // The pressable pair sat 8px right of the plain pair on the Metrics catalog
  // at 375px: its hover padding pushed the caption off the grid line.
  // A class guard only: jsdom lays nothing out, so this pins the padding /
  // margin pair that cancels out, not the alignment, which is checked in a
  // browser at 375px.
  it('puts a pressable stat on the same grid line as a plain one', () => {
    const { container } = render(
      <MiniStatStrip phoneGrid>
        <MiniStat label="A" value="1" />
        <MiniStat label="B" value="2" />
        <MiniStat label="C" value="3" onPress={() => {}} />
        <MiniStat label="D" value="4" onPress={() => {}} />
      </MiniStatStrip>,
    )
    const toggles = container.querySelectorAll('[data-slot="mini-stat-pressable"]')
    expect(toggles).toHaveLength(2)
    for (const toggle of toggles) {
      // The horizontal padding is cancelled by an equal negative margin.
      expect(toggle).toHaveClass('px-2', '-mx-2')
    }
    // The clip box leaves room for that 8px overhang plus a 2px focus ring,
    // and still stops short of the dividers 12px out.
    expect(container.querySelector('[data-slot="mini-stat-clip"]')).toHaveClass('-m-2.5', 'p-2.5')
  })

  it('keeps the wrapping row by default', () => {
    const { container } = render(
      <MiniStatStrip>
        <MiniStat label="A" value="1" />
      </MiniStatStrip>,
    )
    expect(container.querySelector('[data-phone-grid]')).toBeNull()
  })
})

// F31: a stat that filters is a real toggle button, not a <dl> inside a
// role="button" div.
describe('MiniStat pressable', () => {
  it('renders a toggle named "<label> <value>" with no definition list inside', () => {
    const onPress = vi.fn()
    render(<MiniStat label="Firing" value="3" tone="danger" onPress={onPress} pressed={false} />)

    const toggle = screen.getByRole('button', { name: 'Firing 3' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    expect(toggle.querySelector('dl, dt, dd')).toBeNull()
    fireEvent.click(toggle)
    expect(onPress).toHaveBeenCalledTimes(1)
  })

  it('says when its filter is on', () => {
    render(<MiniStat label="Healthy" value="7" onPress={() => {}} pressed />)
    expect(screen.getByRole('button', { name: 'Healthy 7' })).toHaveAttribute('aria-pressed', 'true')
  })
})
