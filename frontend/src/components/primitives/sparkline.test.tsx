import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Sparkline, SparklineSkeleton } from './sparkline'

function linePoints(container: HTMLElement): Array<[number, number]> {
  // The stroked path is the last <path>; the first one is the area fill.
  const paths = container.querySelectorAll('path')
  const d = paths[paths.length - 1]?.getAttribute('d') ?? ''
  return Array.from(d.matchAll(/[ML](-?[\d.]+),(-?[\d.]+)/g), (match) => [
    Number(match[1]),
    Number(match[2]),
  ])
}

function barHeights(container: HTMLElement): number[] {
  return Array.from(container.querySelectorAll('rect'), (rect) => Number(rect.getAttribute('height')))
}

// the range was clamped to at least 1 unit, so a percent metric stored as
// a fraction (0.05 → 0.09) drew under a pixel of travel — a flat line.
describe('Sparkline scaling', () => {
  it('uses the full height for a fractional series', () => {
    const { container } = render(<Sparkline data={[0.05, 0.07, 0.09]} height={22} />)
    const ys = linePoints(container).map(([, y]) => y)
    expect(ys).toHaveLength(3)
    // 2px padding top and bottom: the lowest value sits at 20, the highest at 2.
    expect(Math.max(...ys) - Math.min(...ys)).toBeCloseTo(18)
  })

  it('draws a flat series flat without dividing by zero', () => {
    const { container } = render(<Sparkline data={[0.3, 0.3, 0.3]} height={22} />)
    const ys = linePoints(container).map(([, y]) => y)
    expect(ys.every((y) => Number.isFinite(y))).toBe(true)
    expect(new Set(ys).size).toBe(1)
  })

  it('stands bars on zero so the smallest is not a 1px stub', () => {
    const { container } = render(
      <Sparkline data={[100, 200]} variant="bar" width={40} height={22} />,
    )
    const [small, large] = barHeights(container)
    // 100 of 200 is half the drawable height (20px), not the 1px minimum.
    expect(small).toBeCloseTo(10)
    expect(large).toBeCloseTo(20)
  })

  it('scales fractional bars too', () => {
    const { container } = render(
      <Sparkline data={[0.02, 0.04]} variant="bar" width={40} height={22} />,
    )
    const [small, large] = barHeights(container)
    expect(large).toBeCloseTo(20)
    expect(small).toBeCloseTo(10)
  })
})

// a single series is drawn in the fixed series hue, not the accent, so
// "volume over time" is one colour on every page and under every accent.
describe('Sparkline colour', () => {
  it('defaults to the first series colour rather than the accent', () => {
    const { container } = render(<Sparkline data={[1, 3, 2]} />)
    const paths = container.querySelectorAll('path')
    const stroke = paths[paths.length - 1]?.getAttribute('stroke') ?? ''
    expect(stroke).toContain('--series-1')
    expect(stroke).not.toContain('--accent')
  })

  it('still takes an explicit colour for a series that means something else', () => {
    const { container } = render(<Sparkline data={[1, 3, 2]} color="var(--danger)" />)
    const paths = container.querySelectorAll('path')
    expect(paths[paths.length - 1]?.getAttribute('stroke')).toBe('var(--danger)')
  })
})

// a loading row drew the same "—" as a loaded empty one.
describe('SparklineSkeleton', () => {
  it('holds the sparkline\'s size and stays out of the accessibility tree', () => {
    const { getByTestId } = render(<SparklineSkeleton width={40} height={12} />)
    const block = getByTestId('sparkline-skeleton')
    expect(block).toHaveAttribute('aria-hidden', 'true')
    expect(block.style.width).toBe('40px')
    expect(block.style.height).toBe('12px')
  })
})
