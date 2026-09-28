import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { trackerConfigApi } from '@/api/trackerConfig'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { ProjectTrackerConfig, Role } from '@/types'
import { TrackerConfigDialog } from './TrackerConfigDialog'
import { at } from '@/test/at'
import { type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

vi.mock('@/api/trackerConfig', () => ({
  trackerConfigApi: {
    get: vi.fn(),
    update: vi.fn(),
  },
}))

function makeConfig(overrides: Partial<ProjectTrackerConfig> = {}): ProjectTrackerConfig {
  return {
    id: 't-1',
    project_id: 'p-1',
    enabled: true,
    tracker_type: 'jira',
    base_url: 'https://acme.atlassian.net',
    project_key: 'ENG',
    auth_email: 'ops@acme.com',
    issue_type: 'Task',
    api_token_set: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function authValue(role: Role): AuthContextValue {
  return {
    user: {
      id: `user-${role}`,
      email: `${role}@example.com`,
      name: role,
      role,
      is_platform_admin: false,
      orgs: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}

function renderDialog(role: Persona = 'owner', onOpenChange: (open: boolean) => void = () => {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authValue(role === 'viewer' ? 'member' : role)}>
        <PersonaProject persona={role}>
          <TrackerConfigDialog slug="demo" open onOpenChange={onOpenChange} />
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('TrackerConfigDialog', () => {
  it('renders the section and loads an existing config with the stored-token hint', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig({ api_token_set: true }))

    renderDialog('owner')

    expect(await screen.findByText('Implementation tracker')).toBeInTheDocument()
    expect(
      await screen.findByText(/merging a branch opens one Jira ticket/i),
    ).toBeInTheDocument()

    // Existing values are seeded into the form.
    const baseUrl = await screen.findByLabelText('Base URL')
    expect(baseUrl).toHaveValue('https://acme.atlassian.net')
    expect(screen.getByLabelText('Project key')).toHaveValue('ENG')

    // The raw token is never returned, so the password field is empty and shows
    // the "leave blank to keep" hint.
    const token = screen.getByLabelText('API token')
    expect(token).toHaveValue('')
    expect(token).toHaveAttribute('placeholder', 'Token stored — leave blank to keep')
    expect(screen.getByText(/A token is stored\./i)).toBeInTheDocument()
  })

  it('saves a change via PATCH and omits api_token when the token field is blank', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig())
    vi.mocked(trackerConfigApi.update).mockResolvedValue(makeConfig({ project_key: 'PAY' }))
    const toastSuccess = vi.spyOn(toast, 'success')
    const onOpenChange = vi.fn()

    renderDialog('owner', onOpenChange)

    const projectKey = await screen.findByLabelText('Project key')
    fireEvent.change(projectKey, { target: { value: 'PAY' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // Only what changed: the backend validates every field it is sent, so the
    // untouched ones are left out rather than re-sent.
    await waitFor(() =>
      expect(trackerConfigApi.update).toHaveBeenCalledWith('demo', { project_key: 'PAY' }),
    )
    // The blank token field must not be part of the payload.
    const [, payload] = at(vi.mocked(trackerConfigApi.update).mock.calls, 0)
    expect(payload).not.toHaveProperty('api_token')
    // A save closes the dialog and says so in a toast, instead of a green line
    // over a still-offered Cancel (AU-38).
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(toastSuccess).toHaveBeenCalledWith('Jira tracker connected')
    toastSuccess.mockRestore()
  })

  it('includes api_token in the PATCH only when the user types one', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig({ api_token_set: false }))
    vi.mocked(trackerConfigApi.update).mockResolvedValue(makeConfig({ api_token_set: true }))

    renderDialog('owner')

    const token = await screen.findByLabelText('API token')
    fireEvent.change(token, { target: { value: 'super-secret-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(trackerConfigApi.update).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ api_token: 'super-secret-token' }),
      ),
    )
  })

  it('hides the Save control and disables inputs for non-owners', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig())

    renderDialog('member')

    // Editors can GET but not PATCH, so no Save affordance is offered.
    expect(await screen.findByLabelText('Project key')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(
      screen.getByText('Only project owners can edit the tracker connection.'),
    ).toBeInTheDocument()
  })

  it('surfaces a 403 from PATCH with a plain-language message', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig())
    vi.mocked(trackerConfigApi.update).mockRejectedValue(new ApiError('403 Forbidden', 403))

    renderDialog('owner')

    fireEvent.change(await screen.findByLabelText('Project key'), { target: { value: 'PAY' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(
      await screen.findByText('Only project owners can change the tracker connection.'),
    ).toBeInTheDocument()
  })

  it('shows a failed load with a retry, not "Loading tracker…" forever (PLAN-21)', async () => {
    vi.mocked(trackerConfigApi.get)
      .mockRejectedValueOnce(new Error('Network down'))
      .mockResolvedValue(makeConfig())

    renderDialog('owner')

    expect(await screen.findByText('Could not load the tracker connection')).toBeInTheDocument()
    expect(screen.getByText('Network down')).toBeInTheDocument()
    expect(screen.queryByText('Loading tracker…')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Try again/ }))
    expect(await screen.findByLabelText('Project key')).toHaveValue('ENG')
  })

  it('refuses what the backend would refuse, beside the field (PLAN-21)', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig())

    renderDialog('owner')

    const baseUrl = await screen.findByLabelText('Base URL')
    // The backend is https-only (`_validate_https_url`).
    fireEvent.change(baseUrl, { target: { value: 'http://acme.atlassian.net' } })
    fireEvent.change(screen.getByLabelText('Project key'), { target: { value: '1-bad' } })
    // A saved value cannot be blanked: the PATCH has no way to clear it.
    fireEvent.change(screen.getByLabelText('Auth email'), { target: { value: ' ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(
      await screen.findByText('Enter an https URL, such as https://acme.atlassian.net.'),
    ).toBeInTheDocument()
    expect(baseUrl).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('Project key')).toHaveAttribute('aria-invalid', 'true')
    expect(
      screen.getByText('A saved value cannot be cleared; enter a new one.'),
    ).toBeInTheDocument()
    expect(trackerConfigApi.update).not.toHaveBeenCalled()
  })

  it('requires the connection fields before a tracker can be enabled (PLAN-21)', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(
      makeConfig({ enabled: false, base_url: '', project_key: '', auth_email: '' }),
    )

    renderDialog('owner')

    fireEvent.click(await screen.findByLabelText('Enabled'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findAllByText('Required while the tracker is enabled.')).toHaveLength(3)
    expect(trackerConfigApi.update).not.toHaveBeenCalled()
  })

  describe("the organization's tracker defaults (F20 PR12)", () => {
    const inheriting = () =>
      makeConfig({
        enabled: false,
        base_url: '',
        project_key: '',
        auth_email: '',
        api_token_set: false,
        inherited_fields: ['api_token', 'auth_email', 'base_url', 'project_key'],
      })

    it('enables a tracker whose empty fields the organization fills in', async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(inheriting())
      vi.mocked(trackerConfigApi.update).mockResolvedValue(makeConfig())

      renderDialog('owner')

      fireEvent.click(await screen.findByLabelText('Enabled'))
      expect(screen.getAllByText('From the organization’s tracker defaults.')).toHaveLength(3)
      expect(screen.getByLabelText('API token')).toHaveAttribute(
        'placeholder',
        "The organization's API token — leave blank to use it",
      )
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      await waitFor(() => expect(trackerConfigApi.update).toHaveBeenCalledWith('demo', { enabled: true }))
      expect(screen.queryByText('Required while the tracker is enabled.')).toBeNull()
    })

    it("asks for the project's own account and token once it names its own site (critique #13)", async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(inheriting())

      renderDialog('owner')

      fireEvent.click(await screen.findByLabelText('Enabled'))
      fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://other.atlassian.net' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      // The project key still comes from the organization; the account and the
      // token do not follow a site the project names.
      expect(await screen.findAllByText('Required while the tracker is enabled.')).toHaveLength(1)
      expect(screen.getByText(/the organization's token is never sent there/)).toBeInTheDocument()
      expect(trackerConfigApi.update).not.toHaveBeenCalled()
    })
  })

  it('lets an owner park an incomplete connection while it is disabled', async () => {
    vi.mocked(trackerConfigApi.get).mockResolvedValue(
      makeConfig({ enabled: false, base_url: '', project_key: '', auth_email: '' }),
    )
    vi.mocked(trackerConfigApi.update).mockResolvedValue(makeConfig({ enabled: false }))

    renderDialog('owner')

    fireEvent.change(await screen.findByLabelText('Issue type'), { target: { value: 'Story' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // The blank fields are not sent: the backend would answer "Jira base_url
    // is required" for an empty string, whatever the tracker's state.
    await waitFor(() =>
      expect(trackerConfigApi.update).toHaveBeenCalledWith('demo', { issue_type: 'Story' }),
    )
  })

  describe('Linear (#258)', () => {
    it('switches to Linear: team id and a write-only API key replace the Jira fields', async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig({ api_token_set: true }))
      vi.mocked(trackerConfigApi.update).mockResolvedValue(
        makeConfig({ tracker_type: 'linear', team_id: 'team-1' }),
      )
      const toastSuccess = vi.spyOn(toast, 'success')

      renderDialog('owner')

      fireEvent.click(await screen.findByRole('button', { name: 'Linear' }))
      expect(screen.getByRole('button', { name: 'Linear' })).toHaveAttribute('aria-pressed', 'true')
      expect(screen.queryByLabelText('Base URL')).toBeNull()
      expect(screen.queryByLabelText('Issue type')).toBeNull()

      // The stored secret is Jira's, so the key field does not offer to keep it.
      const key = screen.getByLabelText('API key')
      expect(key).toHaveAttribute('type', 'password')
      expect(key).toHaveAttribute('placeholder', 'Paste your Linear API key')

      fireEvent.change(screen.getByLabelText('Team ID'), { target: { value: 'team-1' } })
      fireEvent.change(key, { target: { value: 'lin_api_secret' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      await waitFor(() =>
        expect(trackerConfigApi.update).toHaveBeenCalledWith('demo', {
          tracker_type: 'linear',
          team_id: 'team-1',
          api_token: 'lin_api_secret',
        }),
      )
      expect(toastSuccess).toHaveBeenCalledWith('Linear tracker connected')
      toastSuccess.mockRestore()
    })

    it('refuses to enable Linear without a team id or with the old tracker\'s secret', async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig({ api_token_set: true }))

      renderDialog('owner')

      fireEvent.click(await screen.findByRole('button', { name: 'Linear' }))
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      expect(await screen.findByText('Required while the tracker is enabled.')).toBeInTheDocument()
      expect(screen.getByLabelText('Team ID')).toHaveAttribute('aria-invalid', 'true')
      expect(screen.getByText(/Enter the Linear API key; the one stored is for Jira\./)).toBeInTheDocument()
      expect(trackerConfigApi.update).not.toHaveBeenCalled()
    })

    it('warns that saving a switch replaces the stored connection, and only while switched', async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(makeConfig({ api_token_set: true }))

      renderDialog('owner')

      await screen.findByRole('button', { name: 'Linear' })
      expect(screen.queryByTestId('tracker-switch-notice')).toBeNull()

      fireEvent.click(screen.getByRole('button', { name: 'Linear' }))
      const notice = screen.getByTestId('tracker-switch-notice')
      expect(notice).toHaveTextContent(
        'Saving replaces the stored Jira connection: its API token and project key are removed.',
      )
      expect(notice).toHaveTextContent('Enter the Linear API key below to save.')

      fireEvent.click(screen.getByRole('button', { name: 'Jira' }))
      expect(screen.queryByTestId('tracker-switch-notice')).toBeNull()
    })

    it('requires the new secret to save a switch even while the tracker is disabled', async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(
        makeConfig({ enabled: false, api_token_set: true }),
      )
      vi.mocked(trackerConfigApi.update).mockResolvedValue(
        makeConfig({ enabled: false, tracker_type: 'linear' }),
      )

      renderDialog('owner')

      fireEvent.click(await screen.findByRole('button', { name: 'Linear' }))
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      expect(await screen.findByText(/Enter the Linear API key; the one stored is for Jira\./))
        .toBeInTheDocument()
      expect(screen.getByLabelText('API key')).toHaveAttribute('aria-invalid', 'true')
      // A disabled switch may leave the team blank: only the secret is required.
      expect(screen.getByLabelText('Team ID')).not.toHaveAttribute('aria-invalid', 'true')
      expect(trackerConfigApi.update).not.toHaveBeenCalled()

      fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'lin_api_secret' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      await waitFor(() =>
        expect(trackerConfigApi.update).toHaveBeenCalledWith('demo', {
          tracker_type: 'linear',
          api_token: 'lin_api_secret',
        }),
      )
    })

    it('loads a saved Linear connection and keeps its stored key when left blank', async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(
        makeConfig({ tracker_type: 'linear', team_id: 'team-1', api_token_set: true }),
      )
      vi.mocked(trackerConfigApi.update).mockResolvedValue(
        makeConfig({ tracker_type: 'linear', team_id: 'team-2' }),
      )

      renderDialog('owner')

      const teamId = await screen.findByLabelText('Team ID')
      expect(teamId).toHaveValue('team-1')
      expect(screen.getByLabelText('API key')).toHaveAttribute(
        'placeholder',
        'Key stored — leave blank to keep',
      )

      fireEvent.change(teamId, { target: { value: 'team-2' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))

      await waitFor(() =>
        expect(trackerConfigApi.update).toHaveBeenCalledWith('demo', { team_id: 'team-2' }),
      )
    })

    it('shows the tracker as text, not a switch, to non-owners', async () => {
      vi.mocked(trackerConfigApi.get).mockResolvedValue(
        makeConfig({ tracker_type: 'linear', team_id: 'team-1' }),
      )

      renderDialog('member')

      expect(await screen.findByLabelText('Team ID')).toBeDisabled()
      expect(screen.queryByRole('button', { name: 'Linear' })).toBeNull()
      expect(screen.getByText('Linear')).toBeInTheDocument()
    })
  })
})
