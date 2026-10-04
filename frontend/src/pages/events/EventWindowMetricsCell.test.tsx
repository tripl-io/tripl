import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SERIES_COLORS } from '@/components/ui/chart-format'
import { TooltipProvider } from '@/components/ui/tooltip'
import { EventWindowMetricsCell } from './EventWindowMetricsCell'

const DATA = [3, 5, 4, 9].map((count, i) => ({
  bucket: `2026-09-2${i}T00:00:00Z`,
  count,
}))

function renderCell(props: Partial<Parameters<typeof EventWindowMetricsCell>[0]> = {}) {
  return render(
    <TooltipProvider>
      <EventWindowMetricsCell
        eventName="spot:open"
        totalCount={21}
        data={DATA as never}
        {...props}
      />
    </TooltipProvider>,
  )
}

function sparkLine(container: HTMLElement) {
  return container.querySelector('path[stroke]')
}

describe('EventWindowMetricsCell', () => {
  it('draws the line in the fixed single-series hue, not the accent', () => {
    const { container } = renderCell()
    expect(sparkLine(container)).toHaveAttribute('stroke', SERIES_COLORS[0])
  })

  it('keeps the event type colour when one is set', () => {
    const { container } = renderCell({ color: '#123456' })
    expect(sparkLine(container)).toHaveAttribute('stroke', '#123456')
  })

  it('marks a signal with the anomaly dot and the count, not a repainted line', () => {
    const { container } = renderCell({ signalTone: 'danger', anomalyIdx: 3 })
    expect(sparkLine(container)).toHaveAttribute('stroke', SERIES_COLORS[0])
    expect(container.querySelector('circle')).toHaveAttribute('fill', 'var(--danger)')
    expect(screen.getByRole('img', { name: /spot:open metrics/ })).toHaveStyle({
      color: 'var(--danger)',
    })
  })

  it('shows a loading placeholder, not the no-data dash, while pending', () => {
    const { container } = renderCell({ pending: true, totalCount: undefined, data: [] as never })
    expect(screen.getByRole('img', { name: 'spot:open metrics: loading' })).toBeInTheDocument()
    expect(container).not.toHaveTextContent('—')
  })
})
