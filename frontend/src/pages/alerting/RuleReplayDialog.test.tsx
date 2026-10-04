import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { alertingApi } from '@/api/alerting'
import type { AlertRule, AlertRuleSimulateResponse } from '@/types'
import { RuleReplayDialog } from './RuleReplayDialog'

const LONG_SCOPE = 'a9ed99c1-ca7f-4704-a8b6-d836c44b66e2'
const LONG_MESSAGE = `[tripl] 3 alerts\nProject delivery via demo sink\n${'unbroken-preview-token'.repeat(24)}`

const RULE: AlertRule = {
  id: 'rule-1',
  destination_id: 'destination-1',
  scan_config_id: null,
  name: 'Test',
  enabled: true,
  include_project_total: true,
  include_event_types: true,
  include_events: true,
  include_schema_drifts: false,
  include_distribution_drifts: false,
  include_release_regressions: false,
  include_variable_value_drifts: false,
  include_metrics: false,
  include_source_freshness: false,
  notify_on_spike: true,
  notify_on_drop: true,
  ai_explanation_enabled: false,
  min_percent_delta: 20,
  min_absolute_delta: 10,
  min_expected_count: 100,
  cooldown_minutes: 1440,
  message_template: null,
  items_template: null,
  message_format: 'plain',
  filters: [],
  muted: false,
  muted_until: null,
  total_deliveries: 12,
  incident_count: 4,
  last_delivery_at: '2026-07-19T09:00:00Z',
  last_delivery_status: 'sent',
  created_at: '2026-07-19T00:00:00Z',
  updated_at: '2026-07-19T00:00:00Z',
}

const RESULT: AlertRuleSimulateResponse = {
  rule_id: RULE.id,
  rule_name: RULE.name,
  days: 7,
  window_from: '2026-07-12T10:00:00Z',
  window_to: '2026-07-19T10:00:00Z',
  anomalies_considered: 6,
  matched_before_cooldown: 3,
  noisy: false,
  cooldown_minutes_used: 1440,
  cooldown_minutes_saved: 1440,
  min_percent_delta_used: 20,
  min_percent_delta_saved: 20,
  min_expected_count_used: 100,
  min_expected_count_saved: 100,
  sigma_threshold_used: null,
  sigma_threshold_saved: null,
  rendered_message: LONG_MESSAGE,
  firings: [
    {
      anomaly_id: 'anomaly-1',
      scan_config_id: 'scan-1',
      scope_type: 'project_total',
      scope_ref: LONG_SCOPE,
      scope_name: LONG_SCOPE,
      event_type_id: null,
      event_id: null,
      drift_field: null,
      drift_type: null,
      sample_value: null,
      bucket: '2026-07-19T10:00:00Z',
      direction: 'spike',
      actual_count: 5780,
      expected_count: 3529,
      absolute_delta: 2251,
      percent_delta: 63.8,
      window_from: null,
      rendered_item: null,
    },
  ],
}

function renderDialog() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <RuleReplayDialog
        open
        onOpenChange={() => {}}
        slug="demo"
        destinationId="destination-1"
        rule={RULE}
        scans={[{ id: 'scan-1', name: 'iOS primary' }, { id: 'scan-2', name: 'iOS backup' }]}
      />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('RuleReplayDialog responsive results', () => {
  it('distinguishes firings of one scope from two scans', async () => {
    vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue({
      ...RESULT,
      firings: [
        RESULT.firings[0]!,
        { ...RESULT.firings[0]!, anomaly_id: 'anomaly-2', scan_config_id: 'scan-2' },
      ],
    })
    renderDialog()
    // Runs on open.
    expect(await screen.findByText('iOS primary')).toBeInTheDocument()
    expect(screen.getByText('iOS backup')).toBeInTheDocument()
  })

  it('confines wide replay data to the result viewport without widening the dialog', async () => {
    vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()

    const scope = await screen.findByText(LONG_SCOPE)
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveClass('w-[calc(100vw-2rem)]', 'max-w-4xl', 'overflow-hidden')

    const firingRegion = within(dialog).getByRole('region', { name: 'Replay firings' })
    expect(firingRegion).toHaveAttribute('tabindex', '0')

    const table = scope.closest('table')
    expect(table).not.toBeNull()
    expect(table).toHaveClass('md:min-w-[840px]', 'table-fixed')
    // The design-system table's wrapper sits between them, but the named region
    // stays the one element that scrolls.
    expect(table?.parentElement?.parentElement).toBe(firingRegion)
    expect(table?.parentElement).not.toHaveClass('overflow-x-auto')
    expect(firingRegion).toHaveClass('min-w-0', 'max-w-full', 'overflow-x-auto')
    // Short and in sans with tabular figures, so it no longer runs into
    // Scope.
    const when = within(table as HTMLTableElement).getByText(/^Jul 19, \d\d:\d\d$/)
    expect(when).toHaveClass('whitespace-nowrap', 'tnum')
    expect(when).not.toHaveClass('mono')

    // Both count columns are grouped by the same formatter, so one row cannot
    // read "5780" beside "3,529".
    expect(within(table as HTMLTableElement).getByText('5,780')).toBeInTheDocument()
    expect(within(table as HTMLTableElement).getByText('3,529')).toBeInTheDocument()

    const preview = dialog.querySelector('pre')
    expect(preview?.textContent).toContain(LONG_MESSAGE)
    expect(preview).toHaveClass('min-w-0', 'max-w-full', '[overflow-wrap:anywhere]')
    const footerClose = screen
      .getAllByRole('button', { name: 'Close' })
      .find((button) => button.getAttribute('data-slot') === 'button')
    expect(footerClose).toBeInTheDocument()
    expect(footerClose?.closest('[data-slot="dialog-footer"]')).toHaveClass('shrink-0')
  })
})

describe('RuleReplayDialog threshold overrides', () => {
  it('replays a stricter Min % against the saved run without writing it to the rule', async () => {
    const stricter: AlertRuleSimulateResponse = {
      ...RESULT,
      firings: [],
      min_percent_delta_used: 300,
      min_percent_delta_saved: 20,
    }
    const simulate = vi
      .spyOn(alertingApi, 'simulateRule')
      .mockImplementation((_slug, _destination, _rule, _days, overrides) =>
        Promise.resolve(overrides?.minPercentDelta === undefined ? RESULT : stricter),
      )
    renderDialog()
    await screen.findByText('Saved thresholds')

    fireEvent.change(screen.getByLabelText('Min change (%)'), {
      target: { value: '300' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Replay' }))

    // Two runs over the same window: `_used`/`_saved` name the thresholds one
    // run applied, but only the pair says how many firings the change removes.
    expect(await screen.findByText('With your overrides')).toBeInTheDocument()
    expect(simulate).toHaveBeenCalledWith('demo', 'destination-1', 'rule-1', 7)
    expect(simulate).toHaveBeenCalledWith('demo', 'destination-1', 'rule-1', 7, {
      minPercentDelta: 300,
    })
    expect(screen.getByText('Saved thresholds')).toBeInTheDocument()
    expect(screen.getByText(/saved 20/)).toBeInTheDocument()
    expect(
      screen.getByText(/Nothing here is saved to the rule/),
    ).toBeInTheDocument()
  })

  it.each([
    ['0', 'the route rejects it with gt=0'],
    ['11', 'the route caps it at the ratchet cap'],
  ])('refuses to replay sigma %s, because %s', async (value) => {
    // The input allowed 0 and had no upper bound while the route enforces
    // `gt=0, le=10`, so this was a UI state that always 422'd with nothing on
    // screen saying why (flagged on PR #108).
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()
    await screen.findByText('Saved thresholds')

    fireEvent.change(screen.getByLabelText('Sigma threshold'), {
      target: { value },
    })

    expect(screen.getByRole('alert')).toHaveTextContent(/Between 0 and 10/)
    expect(screen.getByRole('button', { name: 'Replay' })).toBeDisabled()
    // Only the saved-threshold run on open went out; never the bad sigma.
    expect(simulate.mock.calls.every(call => call[4] === undefined)).toBe(true)
  })

  it('replays a sigma inside the range', async () => {
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()
    await screen.findByText('Saved thresholds')

    fireEvent.change(screen.getByLabelText('Sigma threshold'), {
      target: { value: '4.5' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Replay' }))

    await waitFor(() =>
      expect(simulate).toHaveBeenCalledWith('demo', 'destination-1', 'rule-1', 7, {
        sigmaThreshold: 4.5,
      }),
    )
  })

  it('treats an empty box as "leave it alone" rather than as zero', async () => {
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()

    // `Number('')` is 0, so an unguarded parse would replay every blank field as
    // the most permissive threshold there is and call it the saved behaviour.
    expect(await screen.findByText('Saved thresholds')).toBeInTheDocument()
    expect(simulate).toHaveBeenCalledTimes(1)
    expect(simulate).toHaveBeenCalledWith('demo', 'destination-1', 'rule-1', 7)
    expect(screen.queryByText('With your overrides')).toBeNull()
  })

  it('captions the preview with the format it was rendered in', async () => {
    vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()

    // The caption used to read "as it would render to destination" or
    // "…to Slack/Telegram" depending on whether the FIRST firing happened to
    // carry a scope type — a field with nothing to say about rendering.
    expect(
      await screen.findByText(/the message this run would have sent, as plain text/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Slack\/Telegram/)).toBeNull()
  })

  it('keeps a sub-unit baseline instead of replaying it as "0 vs 0"', async () => {
    // A `%` catalog metric stores a fraction (0.08 == 8%) and metrics are a
    // first-class rule scope, so Math.round emptied both columns of a replay row
    // whose Δ% beside them was computed from the real values.
    vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue({
      ...RESULT,
      firings: [
        {
          ...RESULT.firings[0]!,
          scope_type: 'metric',
          actual_count: 0.04,
          expected_count: 0.12,
          absolute_delta: -0.08,
          percent_delta: -66.7,
        },
      ],
    })
    renderDialog()

    const table = (await screen.findByText(LONG_SCOPE)).closest('table') as HTMLTableElement
    expect(within(table).getByText('0.04')).toBeInTheDocument()
    expect(within(table).getByText('0.12')).toBeInTheDocument()
    expect(within(table).queryByText('0')).toBeNull()
  })
})

describe('RuleReplayDialog cooldown override bounds', () => {
  it.each(['1.5', '20000', '-5'])('refuses %s before sending it', async (value) => {
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()
    await screen.findByText('Saved thresholds')

    const input = screen.getByLabelText('Cooldown (minutes)')
    fireEvent.change(input, { target: { value } })

    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('alert')).toHaveTextContent(/Whole minutes from 0 to 10080/)
    expect(screen.getByRole('button', { name: 'Replay' })).toBeDisabled()
    expect(simulate.mock.calls.every(call => call[4] === undefined)).toBe(true)
  })

  it.each([
    ['Min change (%)'],
    ['Min expected count'],
  ])('refuses a negative %s instead of reading it as blank', async (label) => {
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()
    await screen.findByText('Saved thresholds')

    const input = screen.getByLabelText(label)
    fireEvent.change(input, { target: { value: '-5' } })

    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('alert')).toHaveTextContent(/0 or more/)
    expect(screen.getByRole('button', { name: 'Replay' })).toBeDisabled()
    expect(simulate.mock.calls.every(call => call[4] === undefined)).toBe(true)
  })

  it('accepts a whole number in range', async () => {
    vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()
    await screen.findByText('Saved thresholds')

    fireEvent.change(screen.getByLabelText('Cooldown (minutes)'), { target: { value: '60' } })

    expect(screen.getByRole('button', { name: 'Replay' })).toBeEnabled()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('RuleReplayDialog stale results', () => {
  it('says the result no longer matches once an override changes, until replayed again', async () => {
    vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()

    await screen.findByText(/Considered/)
    expect(screen.queryByText(/Settings changed since this replay/)).toBeNull()

    fireEvent.change(screen.getByLabelText('Min change (%)'), { target: { value: '300' } })
    expect(screen.getByText(/Settings changed since this replay/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Replay' }))
    await waitFor(() =>
      expect(screen.queryByText(/Settings changed since this replay/)).toBeNull(),
    )
  })
})


describe('RuleReplayDialog with unsaved edits', () => {
  it('sends the draft with every run and says it is replaying the edits', async () => {
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    const draft = { cooldown_minutes: 60, min_percent_delta: 50 }
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RuleReplayDialog
          open
          onOpenChange={() => {}}
          slug="demo"
          destinationId="destination-1"
          rule={RULE}
          scans={[]}
          draft={draft}
        />
      </QueryClientProvider>,
    )

    expect(screen.getByRole('heading', { name: /with your unsaved edits/ })).toBeInTheDocument()
    // The overrides are compared with the EDITED thresholds, named as helper
    // text rather than a placeholder that truncated.
    expect(screen.getByLabelText('Cooldown (minutes)')).toHaveAccessibleDescription('Edited: 1h')
    expect(screen.getByLabelText('Cooldown (minutes)')).not.toHaveAttribute('placeholder')

    // The edits are replayed on open…
    await screen.findByText('Your edits')
    fireEvent.change(screen.getByLabelText('Sigma threshold'), { target: { value: '4.5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Replay' }))

    // …and again with the override, beside a fresh run of the edits alone.
    await waitFor(() => expect(simulate).toHaveBeenCalledTimes(3))
    expect(simulate).toHaveBeenCalledWith('demo', 'destination-1', 'rule-1', 7, undefined, draft)
    expect(simulate).toHaveBeenCalledWith(
      'demo', 'destination-1', 'rule-1', 7, { sigmaThreshold: 4.5 }, draft,
    )
    expect(await screen.findByText('Your edits')).toBeInTheDocument()
  })
})

describe('RuleReplayDialog opens on an answer', () => {
  it('replays the saved rule on open and says what replay does', async () => {
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue(RESULT)
    renderDialog()

    expect(screen.getByText(/Nothing is sent or saved/)).toBeInTheDocument()
    await waitFor(() => expect(simulate).toHaveBeenCalledWith('demo', 'destination-1', 'rule-1', 7))
    expect(simulate).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('Saved thresholds')).toBeInTheDocument()
  })
})
