import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuditExportCard } from './AuditExportCard'

/**
 * Organization › Audit log › Export (F20): a download link to the streamed
 * export, live only while the range is one the server accepts.
 */

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-09-28T12:00:00Z'))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('AuditExportCard', () => {
  it('links to a CSV of the last 30 days by default, today included', () => {
    render(<AuditExportCard org="acme" />)

    expect(screen.getByLabelText('To')).toHaveValue('2026-09-28')
    const link = screen.getByRole('link', { name: /Export/ })
    // `to` is exclusive on the server: the day after today, so today's entries are in.
    expect(link).toHaveAttribute('href', '/api/v1/orgs/acme/audit/export?format=csv&from=2026-08-29&to=2026-09-29')
    expect(link).toHaveAttribute('download')
  })

  it('follows the format and dates picked', () => {
    render(<AuditExportCard org="acme" />)

    fireEvent.change(screen.getByLabelText('Format'), { target: { value: 'json' } })
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-01-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-03-01' } })

    expect(screen.getByRole('link', { name: /Export/ })).toHaveAttribute(
      'href',
      '/api/v1/orgs/acme/audit/export?format=json&from=2026-01-01&to=2026-03-02',
    )
  })

  it('offers no link for a backwards range, and says why', () => {
    render(<AuditExportCard org="acme" />)

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-28' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-09-01' } })

    expect(screen.queryByRole('link', { name: /Export/ })).toBeNull()
    expect(screen.getByRole('button', { name: /Export/ })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(/not be before the start date/)
  })

  it('exports a single day as that whole day', () => {
    render(<AuditExportCard org="acme" />)

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-28' } })

    expect(screen.getByRole('link', { name: /Export/ })).toHaveAttribute(
      'href',
      '/api/v1/orgs/acme/audit/export?format=csv&from=2026-09-28&to=2026-09-29',
    )
  })

  it('refuses more than 366 days in one file', () => {
    render(<AuditExportCard org="acme" />)

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2024-01-01' } })

    expect(screen.queryByRole('link', { name: /Export/ })).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent(/at most 366 days/)
  })
})
