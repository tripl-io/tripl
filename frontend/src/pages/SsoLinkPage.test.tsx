import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { ssoApi } from '@/api/sso'
import { authAs } from '@/test/auth'
import SsoLinkPage from './SsoLinkPage'

/** `/sso/link?ticket=` (F20): an existing account is linked only once confirmed. */

afterEach(() => {
  vi.restoreAllMocks()
})

function renderPage(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/sso/link" element={<SsoLinkPage />} />
          <Route path="/auth" element={<p>sign-in page</p>} />
          <Route path="/o/:org" element={<p>organization home</p>} />
          <Route path="/" element={<p>app home</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SsoLinkPage', () => {
  it('asks before linking, then links and goes to the organization', async () => {
    vi.spyOn(ssoApi, 'linkPreview').mockResolvedValue({
      email: 'jane@example.com',
      org_slug: 'acme',
      org_name: 'Acme',
    })
    const confirmLink = vi.spyOn(ssoApi, 'confirmLink').mockResolvedValue({ next: '/', user: authAs('member').user ?? undefined })
    renderPage('/sso/link?ticket=tk-1')

    expect(await screen.findByText('jane@example.com')).toBeInTheDocument()
    expect(screen.getByText('Acme')).toBeInTheDocument()
    expect(confirmLink).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByText('organization home')).toBeInTheDocument()
    expect(confirmLink).toHaveBeenCalledWith('tk-1')
  })

  it('asks in general words when the server offers no preview', async () => {
    vi.spyOn(ssoApi, 'linkPreview').mockRejectedValue(new ApiError('Not Found', 405))
    const confirmLink = vi.spyOn(ssoApi, 'confirmLink').mockResolvedValue({ next: '/', user: authAs('member').user ?? undefined })
    renderPage('/sso/link?ticket=tk-1')

    expect(await screen.findByText(/Link your existing tripl account/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByText('app home')).toBeInTheDocument()
    expect(confirmLink).toHaveBeenCalledWith('tk-1')
  })

  it('goes where the sign-in was headed, when that is a path on this origin', async () => {
    vi.spyOn(ssoApi, 'linkPreview').mockResolvedValue({
      email: 'jane@example.com',
      org_slug: 'acme',
      org_name: 'Acme',
    })
    vi.spyOn(ssoApi, 'confirmLink').mockResolvedValue({ next: '//evil.example.com/' })
    renderPage('/sso/link?ticket=tk-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }))
    expect(await screen.findByText('organization home')).toBeInTheDocument()
  })

  it('cancelling links nothing', async () => {
    vi.spyOn(ssoApi, 'linkPreview').mockResolvedValue({
      email: 'jane@example.com',
      org_slug: 'acme',
      org_name: 'Acme',
    })
    const confirmLink = vi.spyOn(ssoApi, 'confirmLink')
    renderPage('/sso/link?ticket=tk-1')

    fireEvent.click(await screen.findByRole('link', { name: 'Cancel' }))
    expect(await screen.findByText('sign-in page')).toBeInTheDocument()
    expect(confirmLink).not.toHaveBeenCalled()
  })

  it('says an expired or used ticket does not work', async () => {
    vi.spyOn(ssoApi, 'linkPreview').mockResolvedValue({
      email: 'jane@example.com',
      org_slug: 'acme',
      org_name: 'Acme',
    })
    vi.spyOn(ssoApi, 'confirmLink').mockRejectedValue(new ApiError('Invalid or expired link ticket', 400))
    renderPage('/sso/link?ticket=tk-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }))
    expect(
      await screen.findByRole('heading', { name: 'This link request does not work' }),
    ).toBeInTheDocument()
  })

  it('sends the person to sign in to the account first when the preview asks for it', async () => {
    vi.spyOn(ssoApi, 'linkPreview').mockResolvedValue({
      email: 'jane@example.com',
      org_slug: 'acme',
      org_name: 'Acme',
      sign_in_required: true,
    })
    const confirmLink = vi.spyOn(ssoApi, 'confirmLink')
    renderPage('/sso/link?ticket=tk-1')

    expect(await screen.findByText(/To confirm, first sign in to/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'Sign in to confirm' }))
    expect(await screen.findByText('sign-in page')).toBeInTheDocument()
    expect(confirmLink).not.toHaveBeenCalled()
  })

  it('asks for a sign-in when the confirm answers 401', async () => {
    vi.spyOn(ssoApi, 'linkPreview').mockResolvedValue({
      email: 'jane@example.com',
      org_slug: 'acme',
      org_name: 'Acme',
      sign_in_required: false,
    })
    vi.spyOn(ssoApi, 'confirmLink').mockRejectedValue(
      new ApiError('Sign in to this account first, then confirm the link.', 401),
    )
    renderPage('/sso/link?ticket=tk-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('link', { name: 'Sign in to confirm' })).toBeInTheDocument()
  })

  it('needs a ticket', async () => {
    const preview = vi.spyOn(ssoApi, 'linkPreview')
    renderPage('/sso/link')

    expect(await screen.findByText('This link is missing its ticket.')).toBeInTheDocument()
    await waitFor(() => expect(preview).not.toHaveBeenCalled())
  })
})
