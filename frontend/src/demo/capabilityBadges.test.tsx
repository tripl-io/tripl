import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DemoDataBadge, DemoSourceBadge, SyntheticSourceBadge } from './capabilityBadges'

describe('capability badges', () => {
  it('labels a synthetic source distinctly from a real connection', () => {
    render(<SyntheticSourceBadge />)

    const badge = screen.getByText('Synthetic')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveAttribute('title', expect.stringContaining('not a real connection'))
  })

  it('marks the demo project as local synthetic data', () => {
    render(<DemoDataBadge />)
    expect(screen.getByText('Local synthetic data')).toBeInTheDocument()
  })

  it('names the demo warehouse, and the engine serving it on hover', () => {
    render(<DemoSourceBadge source={{ db_type: 'demo', demo_engine: 'postgres' } as never} />)
    const badge = screen.getByText('Demo warehouse')
    expect(badge).toHaveAttribute('title', expect.stringContaining('read-only PostgreSQL'))
    expect(badge).toHaveAttribute('title', expect.stringContaining('not a connection anyone configured'))
  })

  it('says when the demo warehouse fell back to memory, and stays honest without an engine', () => {
    const { unmount } = render(<DemoSourceBadge source={{ db_type: 'demo', demo_engine: 'memory' } as never} />)
    expect(screen.getByText('Demo warehouse')).toHaveAttribute('title', expect.stringContaining('served in memory'))
    unmount()

    render(<DemoSourceBadge source={{ db_type: 'demo' }} />)
    expect(screen.getByText('Demo warehouse')).toHaveAttribute('title', expect.stringContaining('shared by every demo'))
  })

  it('keeps the Synthetic badge on an older demo’s source', () => {
    render(<DemoSourceBadge source={{ db_type: 'synthetic' }} />)
    expect(screen.getByText('Synthetic')).toBeInTheDocument()
    expect(screen.queryByText('Demo warehouse')).not.toBeInTheDocument()
  })
})
