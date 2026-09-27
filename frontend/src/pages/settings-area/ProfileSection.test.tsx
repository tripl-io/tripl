import type { ReactElement } from 'react'
import { fireEvent, render as rtlRender, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ProfileSection from './ProfileSection'

const { getPrefs, updatePrefs } = vi.hoisted(() => ({
  getPrefs: vi.fn(),
  updatePrefs: vi.fn(),
}))

vi.mock('@/api/notifications', () => ({
  notificationsApi: { getPrefs, updatePrefs },
}))

function render(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return rtlRender(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  getPrefs.mockReset().mockResolvedValue({ email_mode: 'daily', mentions_email: true, email_available: true })
  updatePrefs.mockReset().mockImplementation(async (patch: object) => ({
    email_mode: 'daily',
    mentions_email: true,
    email_available: true,
    ...patch,
  }))
})

vi.mock('@/components/auth-context', () => ({
  useAuth: () => ({
    user: {
      id: 'u1',
      email: 'ada@example.com',
      name: 'Ada Lovelace',
      role: 'owner',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: vi.fn(),
    refresh: vi.fn(),
  }),
}))

describe('Account · Profile', () => {
  it('says once that nothing here is editable (#237 ST-17)', () => {
    render(<ProfileSection />)

    expect(screen.getByRole('note')).toHaveTextContent(/can't be changed here yet/)
  })

  it('shows the account’s real details', () => {
    render(<ProfileSection />)

    expect(screen.getByText('Ada Lovelace')).toBeInTheDocument()
    expect(screen.getByText('ada@example.com')).toBeInTheDocument()
    // The shared role chip, the one Members shows too (ST-16).
    expect(screen.getByText('Owner')).toHaveAttribute('data-slot', 'chip')
    // Plain text, not a form of read-only fields (ST-23).
    const details = screen.getByRole('region', { name: 'Your details' })
    expect(within(details).queryAllByRole('group')).toHaveLength(0)
  })

  /**
   * The card says timestamps follow the browser's timezone, and the value beside
   * it used to read a hardcoded "Europe/Berlin" (tripl-hmlx).
   */
  it('shows the browser timezone, not a hardcoded city', () => {
    render(<ProfileSection />)

    expect(
      screen.getByText(Intl.DateTimeFormat().resolvedOptions().timeZone),
    ).toBeInTheDocument()
  })

  /**
   * WS-37: the unbuilt preferences were first live controls that persisted
   * nowhere (tripl-z9ot), then the same controls disabled. Now they are one
   * "Coming later" card with nothing to click.
   */
  it('names what is not built in one card without a single control', () => {
    render(<ProfileSection />)

    const later = screen.getByRole('region', { name: 'Coming later' })
    expect(within(later).getByText('Display preferences')).toBeInTheDocument()
    expect(within(later).queryAllByRole('button')).toHaveLength(0)
    expect(within(later).queryAllByRole('switch')).toHaveLength(0)
    expect(within(later).queryAllByRole('combobox')).toHaveLength(0)
    expect(within(later).queryAllByRole('textbox')).toHaveLength(0)
    expect(screen.queryByText(/saved on this device/i)).toBeNull()
  })

  /** #259: personal notifications are built now — email frequency and mention emails. */
  it('shows and saves the email frequency and the mention email switch', async () => {
    render(<ProfileSection />)

    const card = screen.getByRole('region', { name: 'Notifications' })
    const daily = await within(card).findByRole('radio', { name: /Daily digest/ })
    expect(daily).toHaveAttribute('aria-checked', 'true')

    fireEvent.click(within(card).getByRole('radio', { name: /Weekly digest/ }))
    await waitFor(() => expect(updatePrefs).toHaveBeenCalledWith({ email_mode: 'weekly' }))

    await waitFor(() => expect(within(card).getByRole('switch')).toBeEnabled())
    fireEvent.click(within(card).getByRole('switch'))
    await waitFor(() => expect(updatePrefs).toHaveBeenCalledWith({ mentions_email: false }))
  })
})
