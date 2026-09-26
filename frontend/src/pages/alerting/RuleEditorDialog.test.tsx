import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import type { AlertDestination } from '@/types'

import { defaultRuleForm, type RuleFormState } from './constants'
import { RuleEditorDialog } from './RuleEditorDialog'

const COOLDOWN_LABEL = /re-alert the same scope for/

function makeDestination(overrides: Partial<AlertDestination> = {}): AlertDestination {
  return {
    id: 'dest-1',
    project_id: 'proj-1',
    type: 'telegram',
    name: 'TG',
    held_count: 0,
    enabled: true,
    webhook_set: false,
    bot_token_set: true,
    chat_id: '-100',
    target_url_set: false,
    webhook_header_name: null,
    email_recipients: null,
    email_from_address: null,
    email_subject_template: null,
    jira_base_url: null,
    jira_auth_email: null,
    jira_api_token_set: false,
    jira_project_key: null,
    jira_issue_type: null,
    linear_api_key_set: false,
    linear_team_id: null,
    linear_state_id: null,
    linear_label_ids: null,
    is_local: false,
    delivery_count: 0,
    incident_count: 0,
    rules: [],
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

/**
 * The dialog with its form state held the way MonitorsSection holds it, so
 * what is asserted is what the section would send.
 */
function Harness({
  initial = { ...defaultRuleForm(), name: 'Checkout drops' },
  onSubmit,
  error = null,
  destinationId: initialDestinationId = 'dest-1',
  destinations = [makeDestination()],
  guidedStep = false,
}: {
  initial?: RuleFormState
  onSubmit: (form: RuleFormState) => void
  error?: unknown
  destinationId?: string
  destinations?: AlertDestination[]
  guidedStep?: boolean
}) {
  const [ruleForm, setRuleForm] = useState(initial)
  const [destinationId, setDestinationId] = useState(initialDestinationId)
  return (
    <RuleEditorDialog
      open
      onClose={() => {}}
      slug="demo"
      destinations={destinations}
      destinationId={destinationId}
      onDestinationIdChange={setDestinationId}
      guidedStep={guidedStep}
      isEditing={false}
      ruleForm={ruleForm}
      setRuleForm={setRuleForm}
      eventTypes={[]}
      scans={[]}
      onSubmit={() => onSubmit(ruleForm)}
      isPending={false}
      isError={error !== null}
      error={error}
    />
  )
}

function renderDialog(props: Parameters<typeof Harness>[0]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Harness {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('RuleEditorDialog — numbers can be cleared and are checked (ALR-16)', () => {
  it('lets the cooldown be emptied instead of snapping it to 0, and refuses to save it', () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit })

    const cooldown = screen.getByLabelText(COOLDOWN_LABEL)
    fireEvent.change(cooldown, { target: { value: '' } })
    expect(cooldown).toHaveValue(null)

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(cooldown).toHaveAttribute('aria-invalid', 'true')
    expect(cooldown).toHaveAccessibleDescription(/Enter a number/)
  })

  it('submits what was typed once it is valid', () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit })

    fireEvent.change(screen.getByLabelText(COOLDOWN_LABEL), { target: { value: '30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(onSubmit).toHaveBeenCalledTimes(1)
    // The default cooldown is a day, shown as "1 days"; 30 of that unit is
    // 30 days in minutes (AL-6).
    expect(onSubmit.mock.calls[0]![0]).toMatchObject({ cooldown_minutes: String(30 * 1440) })
  })
})

describe('RuleEditorDialog — cooldown in human units (AL-6)', () => {
  it('shows 1440 minutes as 1 day, not as a number to divide', () => {
    renderDialog({ onSubmit: vi.fn() })

    expect(screen.getByLabelText(COOLDOWN_LABEL)).toHaveValue(1)
    expect(screen.getByRole('combobox', { name: 'Cooldown unit' })).toHaveTextContent('days')
  })

  it('keeps an odd number of minutes in minutes', () => {
    renderDialog({ onSubmit: vi.fn(), initial: { ...defaultRuleForm(), name: 'x', cooldown_minutes: '45' } })

    expect(screen.getByLabelText(COOLDOWN_LABEL)).toHaveValue(45)
    expect(screen.getByRole('combobox', { name: 'Cooldown unit' })).toHaveTextContent('minutes')
  })
})

describe('RuleEditorDialog — thresholds say what they mean (AL-2)', () => {
  it('starts a new rule at 30%, so a partial drop can alert', () => {
    renderDialog({ onSubmit: vi.fn() })

    expect(screen.getByLabelText('Alert when the change is at least')).toHaveValue(30)
    expect(screen.queryByText(/Drops will only alert when volume falls to zero/)).toBeNull()
  })

  it('warns, without blocking, when a drop rule could only fire at zero volume', () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit })

    fireEvent.change(screen.getByLabelText('Alert when the change is at least'), { target: { value: '100' } })

    expect(screen.getByText(/Drops will only alert when volume falls to zero/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })
})

describe('RuleEditorDialog — destination (AL-3)', () => {
  it('keeps Create enabled without a destination and names the missing field on submit', async () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit, destinationId: '' })

    const create = screen.getByRole('button', { name: 'Create' })
    expect(create).toBeEnabled()
    fireEvent.click(create)

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText('Pick a destination.')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Destination' })).toHaveAttribute('aria-invalid', 'true')
  })
})

describe('RuleEditorDialog — one validation timing (AL-5)', () => {
  it('names an emptied name once the field is left, before any submit', () => {
    renderDialog({ onSubmit: vi.fn(), initial: defaultRuleForm() })

    const name = screen.getByLabelText('Name')
    expect(name).not.toHaveAttribute('aria-invalid')
    fireEvent.blur(name)

    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(name).toHaveAccessibleDescription('Required')
  })

  it('names an emptied cooldown once the field is left', () => {
    renderDialog({ onSubmit: vi.fn() })

    const cooldown = screen.getByLabelText(COOLDOWN_LABEL)
    fireEvent.change(cooldown, { target: { value: '' } })
    fireEvent.blur(cooldown)

    expect(cooldown).toHaveAccessibleDescription(/Enter a number/)
  })
})

describe('RuleEditorDialog — what / when / where (AL-1)', () => {
  it('orders the steps so filters sit with the signals, and hides the templates', () => {
    renderDialog({ onSubmit: vi.fn() })

    const headings = screen.getAllByRole('heading', { level: 3 }).map(heading => heading.textContent)
    expect(headings.slice(0, 3)).toEqual(['1What to watch', '2When', '3Where'])
    // Filters live under "What to watch".
    const what = screen.getByRole('region', { name: /What to watch/ })
    expect(within(what).getByRole('button', { name: /Add filter/ })).toBeInTheDocument()
    // The two template editors are collapsed until asked for.
    expect(screen.queryByRole('combobox', { name: 'Message template' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Customize message/ }))
    expect(screen.getByRole('combobox', { name: 'Message template' })).toBeInTheDocument()
  })

  it('says in one sentence what Create will set up', () => {
    renderDialog({ onSubmit: vi.fn() })

    expect(screen.getByText(/^Sends to TG when .* spikes or drops by at least 30%, then waits 1 day/))
      .toBeInTheDocument()
  })

  it('does not ask for a scan on a rule that only watches metrics (JR-15)', () => {
    renderDialog({
      onSubmit: vi.fn(),
      initial: {
        ...defaultRuleForm(),
        name: 'Revenue',
        include_project_total: false,
        include_event_types: false,
        include_events: false,
        include_metrics: true,
      },
    })

    expect(screen.queryByLabelText('Scan')).toBeNull()
    expect(screen.getByText(/not tied to a scan/)).toBeInTheDocument()
  })

  it('says it is the last guided step when opened from guided setup (AL-34)', () => {
    renderDialog({ onSubmit: vi.fn(), guidedStep: true })

    expect(screen.getByText('Step 3 of 3')).toBeInTheDocument()
  })
})

describe('RuleEditorDialog — inline validation instead of browser bubbles (AL-28)', () => {
  it('names an empty rule name under its field and does not submit', async () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit, initial: defaultRuleForm() })

    const name = screen.getByLabelText('Name')
    expect(name).not.toHaveAttribute('required')
    expect(name.closest('form')).toHaveAttribute('novalidate')

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(name).toHaveAccessibleDescription('Required')
    // The refused submit moves focus to the first highlighted field.
    await waitFor(() => expect(name).toHaveFocus())
  })

  it('titles the dialog in sentence case', () => {
    renderDialog({ onSubmit: vi.fn() })
    expect(screen.getByRole('dialog', { name: 'New alert rule' })).toBeInTheDocument()
  })
})

describe('RuleEditorDialog — direction (ALR-14)', () => {
  it('names the two boxes for what they are, under one legend', () => {
    renderDialog({ onSubmit: vi.fn() })

    const group = screen.getByRole('group', { name: 'Notify on' })
    expect(within(group).getByLabelText('Spikes (up)')).toBeChecked()
    expect(within(group).getByLabelText('Drops (down)')).toBeChecked()
    expect(screen.queryByText('Up only')).toBeNull()
  })

  it('says so as soon as both are unticked, and does not save', () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit })

    fireEvent.click(screen.getByLabelText('Spikes (up)'))
    fireEvent.click(screen.getByLabelText('Drops (down)'))

    expect(screen.getByRole('group', { name: 'Notify on' }))
      .toHaveAccessibleDescription(/at least one direction/)
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(onSubmit).not.toHaveBeenCalled()
  })
})

describe('RuleEditorDialog — scopes (ALR-15)', () => {
  it('refuses a rule that watches no signal at all', () => {
    const onSubmit = vi.fn()
    renderDialog({
      onSubmit,
      initial: {
        ...defaultRuleForm(),
        name: 'Nothing',
        include_project_total: false,
        include_event_types: false,
        include_events: false,
      },
    })

    expect(screen.getByRole('group', { name: 'Signals' })).toHaveAccessibleDescription(/signal kind/)
    // Two groups, in words a PM knows (AL-38).
    expect(screen.getByText('Volume changes in')).toBeInTheDocument()
    expect(screen.getByText('Also alert on')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(onSubmit).not.toHaveBeenCalled()
  })
})

describe('RuleEditorDialog — an empty filter row is not dropped (ALR-5)', () => {
  it('names the row and refuses to save, instead of saving a broader rule', () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit })

    fireEvent.click(screen.getByRole('button', { name: /Add filter/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText(/Pick at least one value, or remove this filter/)).toBeInTheDocument()
  })
})

describe('RuleEditorDialog — server errors sit beside their fields (ALR-8)', () => {
  it('attaches a field error to its input, without Pydantic\'s prefix', () => {
    const error = new ApiError('cooldown_minutes: Value error, too short', 422)
    error.fields = [{ loc: ['body', 'cooldown_minutes'], msg: 'Value error, too short', type: 'value_error' }]
    renderDialog({ onSubmit: vi.fn(), error })

    const cooldown = screen.getByLabelText(COOLDOWN_LABEL)
    expect(cooldown).toHaveAttribute('aria-invalid', 'true')
    expect(cooldown).toHaveAccessibleDescription('too short')
    expect(screen.queryByText(/Value error/)).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('Check the highlighted fields.')
  })

  it('prints anything with no field as one plain sentence', () => {
    renderDialog({ onSubmit: vi.fn(), error: new Error('Value error, Rule limit reached') })

    expect(screen.getByRole('alert')).toHaveTextContent('Rule limit reached')
    expect(screen.queryByText(/Value error/)).toBeNull()
  })
})

describe('RuleEditorDialog — Source freshness toggle (F16, #269)', () => {
  it('offers the toggle beside the other scopes, off by default, and submits it', () => {
    const onSubmit = vi.fn()
    renderDialog({ onSubmit })

    const toggle = screen.getByLabelText('Source freshness')
    expect(toggle).not.toBeChecked()
    fireEvent.click(toggle)
    expect(toggle).toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ include_source_freshness: true }))
  })
  it('warns, without blocking, when freshness is on but Drops is off', () => {
    const onSubmit = vi.fn()
    renderDialog({
      onSubmit,
      initial: { ...defaultRuleForm(), name: 'x', include_source_freshness: true, notify_on_spike: true, notify_on_drop: false },
    })

    expect(screen.getByText(/Source freshness only alerts on rules that notify on drops/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Drops (down)' }))
    expect(screen.queryByText(/Source freshness only alerts on rules that notify on drops/)).toBeNull()
  })
})
