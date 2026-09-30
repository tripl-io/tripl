import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { orgSettingsApi, type OrgTrackerDefaults } from '@/api/orgSettings'
import { ActiveOrgContext } from '@/components/active-org-context'
import { at } from '@/test/at'
import { orgTrackerDefaultsFixture } from '@/test/orgTrackerDefaults'
import OrgTrackersSection from './OrgTrackersSection'

/**
 * Organization › Trackers (F20 PR12): the Jira/Linear defaults its projects
 * inherit, against `/orgs/{org}/settings/trackers`, secrets write-only.
 */

function renderSection(defaults: OrgTrackerDefaults = orgTrackerDefaultsFixture()) {
  const get = vi.spyOn(orgSettingsApi, 'getTrackers').mockResolvedValue(defaults)
  const update = vi.spyOn(orgSettingsApi, 'updateTrackers').mockResolvedValue(defaults)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
        <MemoryRouter>
          <OrgTrackersSection />
        </MemoryRouter>
      </ActiveOrgContext.Provider>
    </QueryClientProvider>,
  )
  return { get, update }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Trackers', () => {
  it("reads the active organization's defaults, badged, with secrets write-only", async () => {
    const { get } = renderSection()

    expect(await screen.findByDisplayValue('https://acme.atlassian.net')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('acme')
    expect(screen.getByLabelText('API token')).toHaveValue('')
    expect(screen.getByLabelText('API token')).toHaveAttribute('placeholder', 'Configured — leave blank to keep')
    // Five of the six fields are the organization's; the project key is unset.
    expect(screen.getAllByText('Organization', { selector: '[title]' })).toHaveLength(5)
  })

  it('saves only the edited fields', async () => {
    const { update } = renderSection()
    await screen.findByDisplayValue('ENG')

    fireEvent.change(screen.getByLabelText('Default project key'), { target: { value: 'OPS' } })
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'lin_api_new' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', {
        jira: { project_key: 'OPS' },
        linear: { api_key: 'lin_api_new' },
      }),
    )
  })

  it("removes a stored token and warns that the Jira group is now incomplete", async () => {
    const { update } = renderSection()
    await screen.findByDisplayValue('https://acme.atlassian.net')

    fireEvent.click(at(screen.getAllByRole('button', { name: 'Remove it' }), 0))
    expect(screen.getByText(/go together/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() => expect(update).toHaveBeenCalledWith('acme', { jira: { api_token: null } }))
  })

  it('blocks Save on a site that is not https', async () => {
    const { update } = renderSection()
    await screen.findByDisplayValue('https://acme.atlassian.net')

    fireEvent.change(screen.getByLabelText('Site URL'), { target: { value: 'http://jira.internal' } })

    expect(screen.getByText(/Enter an https URL/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeDisabled()
    expect(update).not.toHaveBeenCalled()
  })

  it('shows the server refusal of a private site', async () => {
    const { update } = renderSection()
    update.mockRejectedValueOnce(new Error('jira_base_url resolves to a private address'))
    await screen.findByDisplayValue('https://acme.atlassian.net')

    fireEvent.change(screen.getByLabelText('Site URL'), { target: { value: 'https://jira.acme.example' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    expect(await screen.findByText(/private address/)).toBeInTheDocument()
  })
})
