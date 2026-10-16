import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SystemSettings } from '@/types'
import { SystemCard } from './SystemCard'

const BASE: SystemSettings = {
  version: '9.8.7',
  edition: 'community',
  debug: false,
  database_url_configured: true,
  sync_database_url_configured: true,
  rabbitmq_url_configured: true,
  redis_url_configured: true,
  encryption_key_configured: true,
  openai_api_key_configured: true,
  alembic_revision: 'abc123def456',
  alembic_head_revision: 'abc123def456',
  alembic_up_to_date: true,
}

type Warehouse = NonNullable<SystemSettings['demo_warehouse']>

const POSTGRES: Warehouse = {
  mode: 'auto',
  engine: 'postgres',
  reachable: true,
  reader_configured: true,
  database: 'tripl_demo',
  written_through: '2026-10-16T09:35:00Z',
  oldest: '2026-09-16T00:00:00Z',
  database_bytes: 180 * 1024 * 1024,
  retention_days: 30,
  max_mb: 1024,
  last_run_at: '2026-10-16T09:30:00Z',
  last_error: null,
  reason: null,
  reader_limits_missing: [],
}

function tile(): HTMLElement {
  const element = screen.getByText('Demo warehouse').closest('div.flex-col')
  if (!element) throw new Error('no demo warehouse tile')
  return element as HTMLElement
}

describe('System card: demo warehouse', () => {
  it('shows no tile when the server does not report the warehouse', () => {
    render(<SystemCard system={BASE} />)
    expect(screen.queryByText('Demo warehouse')).toBeNull()
  })

  it('says how far the PostgreSQL warehouse is written and how full it is', () => {
    render(<SystemCard system={{ ...BASE, demo_warehouse: POSTGRES }} />)
    expect(tile()).toHaveTextContent('PostgreSQL · tripl_demo')
    expect(tile()).toHaveTextContent('written through 2026-10-16 09:35 UTC')
    expect(tile()).toHaveTextContent('180 of 1024 MB')
    expect(tile()).toHaveTextContent('30 days kept')
    expect(screen.queryByText(/needs attention/)).toBeNull()
  })

  it('explains why demos run on the in-memory engine, without raising an alarm', () => {
    render(
      <SystemCard
        system={{
          ...BASE,
          demo_warehouse: {
            ...POSTGRES,
            engine: 'memory',
            reachable: false,
            reason: 'The database role cannot create databases.',
          },
        }}
      />,
    )
    expect(tile()).toHaveTextContent('In-memory engine')
    expect(tile()).toHaveTextContent('The database role cannot create databases.')
    expect(screen.queryByText(/needs attention/)).toBeNull()
  })

  it('flags a failed write as needing attention', () => {
    render(
      <SystemCard
        system={{ ...BASE, demo_warehouse: { ...POSTGRES, last_error: 'disk full.' } }}
      />,
    )
    expect(tile()).toHaveTextContent('Write failed')
    expect(screen.getByText(/1 item needs attention/)).toHaveTextContent(
      'The demo warehouse writer failed',
    )
  })
})
