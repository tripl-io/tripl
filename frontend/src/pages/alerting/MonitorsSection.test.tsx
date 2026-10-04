import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

/** The toaster, stubbed: a failed row write says why in one. */
const { toastError, toastSuccess } = vi.hoisted(() => ({ toastError: vi.fn(), toastSuccess: vi.fn() }))
vi.mock('sonner', () => ({
  toast: { success: toastSuccess, error: toastError },
  Toaster: () => null,
}))

import { alertingApi, MAX_ALERT_RULE_NAME_LENGTH } from '@/api/alerting'
import { anomalySettingsApi } from '@/api/anomalySettings'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import type { ProjectAnomalySettings } from '@/types'
import { ApiError } from '@/api/client'
import { INDEFINITE_MUTE, muteChoiceName } from '@/lib/mutePresets'
import type { AlertDestination, AlertRule, MonitorsSummaryResponse, ScanConfig } from '@/types'

import { MonitorsSection, type RuleWithDestination } from './MonitorsSection'
import { at } from '@/test/at'

function makeRule(overrides: Partial<RuleWithDestination> = {}): RuleWithDestination {
  return {
    id: 'rule-1',
    destination_id: 'dest-1',
    destination_name: 'TG',
    scan_config_id: null,
    name: 'Prod drops',
    enabled: true,
    include_project_total: true,
    include_event_types: false,
    include_events: false,
    include_schema_drifts: false,
    include_distribution_drifts: false,
    include_release_regressions: false,
    include_variable_value_drifts: false,
    include_metrics: false,
    include_source_freshness: false,
    notify_on_spike: false,
    notify_on_drop: true,
    ai_explanation_enabled: false,
    min_percent_delta: 100,
    min_absolute_delta: 0,
    min_expected_count: 0,
    cooldown_minutes: 360,
    message_template: null,
    items_template: null,
    message_format: 'plain',
    filters: [],
    muted: false,
    muted_until: null,
    total_deliveries: 115,
    incident_count: 57,
    last_delivery_at: '2026-08-12T09:00:00Z',
    last_delivery_status: 'sent',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function makeDestination(overrides: Partial<AlertDestination> = {}): AlertDestination {
  return {
    id: 'dest-1',
    project_id: 'proj-1',
    type: 'telegram',
    name: 'TG',
    // Zero, not omitted: the API always sends this field, and a double that can
    // leave it out is a claim that a destination with no held count is normal.
    held_count: 0,
    enabled: true,
    webhook_set: false,
    bot_token_set: true,
    chat_id: '-100223344',
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
    delivery_count: 115,
    incident_count: 57,
    rules: [makeRule() as AlertRule],
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function makeSummary(overrides: Partial<MonitorsSummaryResponse> = {}): MonitorsSummaryResponse {
  return {
    monitors: [
      {
        rule_id: 'rule-1',
        rule_name: 'Prod drops',
        destination_id: 'dest-1',
        destination_name: 'TG',
        destination_type: 'telegram',
        enabled: true,
        status: 'firing',
        active_scope_count: 2,
        firing_scope_count: 1,
        last_anomaly_at: '2026-08-12T10:00:00Z',
        last_notified_at: null,
        notify_on_spike: false,
        notify_on_drop: true,
        min_percent_delta: 100,
        min_expected_count: 0,
        cooldown_minutes: 360,
        muted: false,
        muted_until: null,
      },
    ],
    firing_count: 1,
    warning_count: 0,
    healthy_count: 0,
    total: 1,
    // Both scopes fed, so every assertion below sees the ordinary editor. The
    // inert case is its own describe block at the bottom.
    scope_readiness: { variable_value_drift: true, distribution_drift: true },
    ...overrides,
  }
}

interface RenderOptions {
  rules?: RuleWithDestination[]
  destinations?: AlertDestination[]
  scans?: { id: string; name: string }[]
  scansLoaded?: boolean
  scansFailed?: boolean
  canWrite?: boolean
  autoOpenRuleForDestinationId?: string | null
  onAutoOpenRuleConsumed?: () => void
  onGoToDestinations?: () => void
  /** The URL the section mounts under, for `?new=rule`. */
  entry?: string
}

/** The router's current query string, so a test can see params being stripped. */
function LocationProbe() {
  const location = useLocation()
  return <div hidden data-testid="location-search">{location.search}</div>
}

function renderSection(options: RenderOptions = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[options.entry ?? '/']}>
        <MonitorsSection
          slug="acme-ios"
          destinations={options.destinations ?? [makeDestination()]}
          rules={options.rules ?? [makeRule()]}
          eventTypes={[]}
          scans={(options.scans ?? []) as ScanConfig[]}
          scansLoaded={options.scansLoaded ?? true}
          scansFailed={options.scansFailed ?? false}
          canWrite={options.canWrite ?? true}
          autoOpenRuleForDestinationId={options.autoOpenRuleForDestinationId ?? null}
          onAutoOpenRuleConsumed={options.onAutoOpenRuleConsumed ?? (() => {})}
          onGoToDestinations={options.onGoToDestinations ?? (() => {})}
        />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Detection is on unless a test says otherwise; the section reads it. */
function mockDetection(enabled: boolean) {
  return vi.spyOn(anomalySettingsApi, 'get').mockResolvedValue({
    anomaly_detection_enabled: enabled,
  } as ProjectAnomalySettings)
}

beforeEach(() => {
  mockDetection(true)
})

afterEach(() => {
  vi.restoreAllMocks()
  toastError.mockClear()
  toastSuccess.mockClear()
})

/** Replay, Mute and Delete live behind the row's "…" menu. */
async function openRowMenu(ruleName = 'Prod drops') {
  const trigger = await screen.findByRole('button', { name: `More actions for ${ruleName}` })
  fireEvent.keyDown(trigger, { key: 'Enter' })
  return screen.findByRole('menu')
}

describe('MonitorsSection live state', () => {
  it('shows the firing state that only the standalone page used to carry', async () => {
    // This is the whole point of the merge: the rule and the state it is in are
    // one row now, so nothing has to be read on a second nav item.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    // Scoped to the table: the rollup above it also carries a "Firing" label,
    // and the assertion is about the ROW knowing its own state.
    // Awaited on the CHIP, not on the table: the table renders straight from
    // props, so it is on screen before the state request has answered — which
    // is the behaviour the next test pins down.
    const table = await screen.findByRole('table', { name: 'Alert rules' })
    expect(await within(table).findByText('Firing')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Prod drops' })).toHaveAttribute(
      'href',
      '/p/acme-ios/monitors/rule-1',
    )
  })

  it('leaves the Healthy count neutral; only Firing and Warning take colour', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary({ healthy_count: 3 }))
    const { container } = renderSection()

    // A toggle once the summary has answered, so the figure is read
    // off the button rather than a definition list.
    const healthy = await screen.findByRole('button', { name: /^Healthy 3/ })
    const figure = healthy.querySelector('[data-slot="mini-stat-value"]')
    expect(figure).toHaveTextContent('3')
    expect(figure).not.toHaveAttribute('data-tone')
    expect(container.querySelectorAll('[data-slot="mini-stat-value"][data-tone="success"]')).toHaveLength(0)
  })

  it('renders the rule before the state request answers, rather than blanking the list', () => {
    // A rule is not less real for its status being in flight, and emptying the
    // table on every refetch would hide the rows mid-incident.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockReturnValue(new Promise(() => {}))
    renderSection()

    expect(screen.getByRole('link', { name: 'Prod drops' })).toBeInTheDocument()
  })

  it('keeps the delivery health that moved off the destination card', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    // `Never delivered` and `last sent 3h ago` are opposite facts, and the card
    // stated neither before the merge fix — the merge must not lose it again.
    expect(
      await screen.findByText(/115 deliveries · 57 incidents · last .* · sent/),
    ).toBeInTheDocument()
  })

  it('says plainly when a rule has never delivered', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({
      rules: [makeRule({ total_deliveries: 0, incident_count: 0, last_delivery_at: null, last_delivery_status: null })],
    })

    expect(await screen.findByText('Never delivered')).toBeInTheDocument()
  })
})

describe('MonitorsSection rule settings', () => {
  it('keeps every setting behind one labelled expansion instead of dropping them', async () => {
    // The destination card printed these on every rule. Moving the rules must
    // not lose the scan binding or the filters, which the rule detail page does
    // not show at all.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    const toggle = await screen.findByRole('button', { name: 'Show settings for Prod drops' })
    expect(screen.queryByText('Scan')).toBeNull()

    fireEvent.click(toggle)

    expect(screen.getByText('Scan')).toBeInTheDocument()
    expect(screen.getByText('all scans')).toBeInTheDocument()
    expect(screen.getByText('Cooldown')).toBeInTheDocument()
    expect(screen.getByText('6h')).toBeInTheDocument()
    expect(screen.getByText('Min expected')).toBeInTheDocument()
  })
})

/**
 * Every mute button name below is written out as a literal, and stays that way.
 *
 * Since the shared mute-name helpers, this row's component does not compose those sentences: it
 * calls `muteName`, `muteChoiceName` and `unmuteName` from `@/lib/mutePresets`,
 * exactly as `MonitorDetailPage` and `AlertingInbox` do. Deriving the
 * expectations here from those same functions would make these assertions move
 * whenever the wording moved, so they would prove only that the component maps
 * the list it maps. Left literal they are one of the three independent
 * witnesses that the shared sentence is still the sentence every surface's
 * authors read — `mutePresets.test.ts` is the fourth, and is the module's own
 * oracle.
 *
 * The one deliberate exception is the open-ended NEGATIVE below, which is built
 * from the shared module on purpose; the reason is stated where it is asserted.
 */
describe('MonitorsSection mute', () => {
  it('mutes from the row, on the same screen that shows the rule is muted', async () => {
    // Mute lived on a separate page while the mute STATE was rendered here, and
    // that split is exactly how the two disagreed.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const mute = vi.spyOn(alertingApi, 'muteMonitor').mockResolvedValue({} as never)
    renderSection()

    await openRowMenu()
    // The duration is on the control, so no mute is silent — the same labels
    // the Inbox uses, in a menu rather than an inline reveal that pushed the
    // row's columns sideways.
    fireEvent.click(screen.getByRole('menuitem', { name: 'Mute Prod drops for 24h' }))

    await waitFor(() => expect(mute).toHaveBeenCalled())
    expect(at(mute.mock.calls, 0)[0]).toBe('acme-ios')
    expect(at(mute.mock.calls, 0)[1]).toBe('rule-1')
  })

  it('offers Unmute — not a second Mute — once a rule is muted', async () => {
    // "Mute works, unmute does not" was the reported symptom that started this:
    // the only way back was a control labelled something else entirely.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const unmute = vi.spyOn(alertingApi, 'unmuteMonitor').mockResolvedValue({} as never)
    renderSection({ rules: [makeRule({ muted: true, muted_until: '2026-08-19T00:00:00Z' })] })

    expect(await screen.findByText(/^muted until /)).toBeInTheDocument()
    await openRowMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unmute Prod drops' }))

    await waitFor(() => expect(unmute).toHaveBeenCalledWith('acme-ios', 'rule-1'))
  })

  it('always names the instant a muted rule comes back', async () => {
    // READ THIS BEFORE TRUSTING IT: this test does NOT discriminate the guard
    // an earlier fix changed, and no honest test can. The only input that separates
    // `rule.muted && rule.muted_until` from the old `rule.muted` plus a
    // `: 'muted'` else-branch is `{muted: true, muted_until: null}`, which
    // `is_rule_muted()` cannot produce — so a fixture asserting it would enshrine
    // a state the product does not have and teach the next reader that rules can
    // be muted indefinitely, which is the very misconception b82m removed. The
    // value of that change is its comment; this test pins the rendered CONTRACT
    // instead, so a later refactor toward a `muted_until`-only guard, or back to
    // an end-date-less chip, has something to break against.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ rules: [makeRule({ muted: true, muted_until: '2026-08-19T00:00:00Z' })] })

    expect(await screen.findByText(/^muted until /)).toBeInTheDocument()
    // Exact text, so it cannot match the "muted until …" chip above. The row
    // does render plenty of other text — the rule link, the condition, the
    // destination, the status chip, the delivery-health line — but none of it is
    // the single word "muted", so a hit here is an end-date-less mute chip and
    // nothing else.
    expect(screen.queryByText('muted')).toBeNull()
  })

  it('shows no mute chip for a rule whose mute has already lapsed', async () => {
    // A stale `muted_until` in the past with `muted: false` is a NORMAL server
    // response, not a corrupt one: the timestamp is the raw column and stays put
    // after the mute lifts. The row says nothing because it leads on the
    // EFFECTIVE flag — it does no date arithmetic of its own, and must not start
    // doing any, since `is_rule_muted()` is the one place that comparison lives.
    // Get that ordering wrong and the row prints "muted until
    // <a date that has passed>", which is the lapsed-mute defect the Inbox card
    // fixed earlier.
    //
    // Like its neighbour above, this pins the contract rather than the guard:
    // `muted: false` short-circuits in the old and new code alike.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ rules: [makeRule({ muted: false, muted_until: '2026-08-01T00:00:00Z' })] })

    await screen.findByRole('link', { name: 'Prod drops' })
    expect(screen.queryByText(/muted until/)).toBeNull()
    expect(screen.queryByText('muted')).toBeNull()
    // And the menu offers Mute, not Unmute — the row agrees with itself
    // about which state the rule is in.
    await openRowMenu()
    expect(screen.getByRole('menuitem', { name: 'Mute Prod drops for 24h' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Unmute Prod drops' })).toBeNull()
  })

  it('can only mute a rule for a fixed time — no open-ended choice here', async () => {
    // The scope boundary of the indefinite mute. On an INCIDENT a NULL
    // `muted_until` means "silenced until somebody unmutes it"; on a RULE
    // `is_rule_muted()` returns FALSE for a NULL `muted_until`, so the very same
    // button would UN-mute the rule it promises to silence forever. The
    // permanent lever on a rule is its enable/disable switch.
    //
    // This fails the moment the open-ended option leaks in — whether by growing
    // MUTE_PRESETS (which all three surfaces map) or by copy-pasting the Inbox
    // control onto this row.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    await openRowMenu()

    for (const label of ['1h', '24h', '7d']) {
      expect(screen.getByRole('menuitem', { name: `Mute Prod drops for ${label}` }))
        .toBeInTheDocument()
    }
    // The COUNT as well, per surface. Three surfaces map `MUTE_PRESETS` and each
    // one can also grow a button of its own, so "only these three" is a property
    // of this row — not something one assertion in another file can hold for it.
    // A predicate, not an interpolated RegExp: the target is a rule name a user
    // typed, and a `(` in it would quietly match something else.
    const presetPrefix = 'Mute Prod drops for '
    expect(
      screen.getAllByRole('menuitem', { name: (name: string) => name.startsWith(presetPrefix) }),
    ).toHaveLength(3)
    expect(screen.queryByRole('menuitem', { name: /until unmuted/i })).toBeNull()
    expect(screen.queryByText(/Until I unmute/i)).toBeNull()
    // …and the same two negatives built from the shared module, which is what
    // keeps this guard alive through a rename. A frozen negative matches
    // nothing by construction the day the open-ended phrasing is reworded, and
    // then passes forever while guarding nothing — a silently dead test, unlike
    // a positive assertion, which fails loudly when it goes stale.
    //
    // It also replaces a canary this row gave up when it moved to the shared helpers. The component
    // used to write `Mute ${ruleName} for ${preset.label}` as a literal, so it
    // was LEXICALLY incapable of the open-ended phrasing: a leak would have
    // rendered the visibly broken "Mute Prod drops for Until I unmute". It now
    // calls `muteChoiceName`, which contains that branch, so the same leak
    // would read as grammatical English. This assertion is the replacement.
    expect(
      screen.queryByRole('menuitem', { name: muteChoiceName('Prod drops', INDEFINITE_MUTE) }),
    ).toBeNull()
    expect(screen.queryByText(INDEFINITE_MUTE.label)).toBeNull()
  })
})

describe('MonitorsSection rule writes', () => {
  it('states what the cascade destroys instead of asking Delete "Prod drops"?', async () => {
    // `AlertDelivery.rule_id` is ON DELETE CASCADE and the inbox INNER JOINs
    // through it, so this one button takes 115 deliveries and 57 incidents —
    // with their notes and mutes.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const remove = vi.spyOn(alertingApi, 'deleteRule').mockResolvedValue(undefined)
    renderSection()

    await openRowMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete rule Prod drops' }))

    expect(
      await screen.findByText(
        /Delete "Prod drops"\? This also deletes 115 deliveries and 57 incidents/,
      ),
    ).toBeInTheDocument()
    expect(remove).not.toHaveBeenCalled()
  })

  it('routes the enable switch through a mutation so a failure is not silent', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const update = vi.spyOn(alertingApi, 'updateRule').mockResolvedValue(makeRule({ enabled: false }))
    renderSection()

    fireEvent.click(await screen.findByRole('switch', { name: 'Toggle Prod drops' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme-ios', 'dest-1', 'rule-1', { enabled: false }),
    )
  })

  it('leaves the switch showing the server value when the write is rejected', async () => {
    // `checked` is bound to the rule the server sent, so a 403 leaves it exactly
    // where it was rather than flipping and silently reverting.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    vi.spyOn(alertingApi, 'updateRule').mockRejectedValue(new Error('Editor role required'))
    renderSection()

    const toggle = await screen.findByRole('switch', { name: 'Toggle Prod drops' })
    fireEvent.click(toggle)

    await waitFor(() => expect(toggle).toBeChecked())
  })
})

describe('MonitorsSection guided-setup handoff', () => {
  it('opens the rule form prefilled for the destination just created', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ autoOpenRuleForDestinationId: 'dest-1' })

    expect(await screen.findByText('New alert rule')).toBeInTheDocument()
    // Named after its destination and marked as the last step.
    expect(screen.getByLabelText('Name')).toHaveValue('Alerts to TG')
    expect(screen.getByText('Step 3 of 3')).toBeInTheDocument()
  })

  it('stays closed on every ordinary visit', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    await screen.findByRole('link', { name: 'Prod drops' })
    expect(screen.queryByText('New alert rule')).toBeNull()
  })

  it('reports the instruction spent, and does not re-open on the next render', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const consumed = vi.fn()
    renderSection({ autoOpenRuleForDestinationId: 'dest-1', onAutoOpenRuleConsumed: consumed })

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(screen.queryByText('New alert rule')).toBeNull()
    // The page clears its own state off this callback: the prop is still set
    // here, so a section that never reported back would re-open the dialog on
    // the next mount.
    expect(consumed).toHaveBeenCalled()
  })
})

describe('MonitorsSection ?new=rule', () => {
  it('opens a blank rule form on arrival', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ entry: '/p/acme-ios/alerting?section=monitors&new=rule' })

    expect(await screen.findByText('New alert rule')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('')
  })

  it('does not open the form for a viewer', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({
      canWrite: false,
      entry: '/p/acme-ios/alerting?section=monitors&new=rule',
    })

    await screen.findByRole('link', { name: 'Prod drops' })
    expect(screen.queryByText('New alert rule')).toBeNull()
  })

  it('scopes the new rule to the metric it came from, then drops both params', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    // The filter row names its metric from the catalog rather than printing the id.
    vi.spyOn(metricsCatalogApi, 'get').mockResolvedValue({
      id: 'met-1',
      name: 'checkout_rate',
      display_name: 'Checkout rate',
    } as Awaited<ReturnType<typeof metricsCatalogApi.get>>)
    renderSection({ entry: '/p/acme-ios/alerting?section=monitors&new=rule&metric=met-1' })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('New alert rule')).toBeInTheDocument()
    // Metrics on, the volume scopes off: the rule watches that one metric.
    const scopeBox = (label: string) =>
      within(within(dialog).getByText(label).closest('label')!).getByRole('checkbox')
    expect(scopeBox('Metrics')).toBeChecked()
    expect(scopeBox('Project total')).not.toBeChecked()
    // One filter row: Metric · is one of · the metric from the URL.
    expect(within(dialog).getByRole('combobox', { name: 'Filter field' })).toHaveTextContent('Metric')
    expect(within(dialog).getByRole('combobox', { name: 'Filter operator' })).toHaveTextContent('is one of')
    expect(await within(dialog).findByText('Checkout rate')).toBeInTheDocument()
    expect(metricsCatalogApi.get).toHaveBeenCalledWith('acme-ios', 'met-1')

    // Spent: Back or a refresh must not reopen the form, and nothing else in
    // the query string is touched.
    await waitFor(() => {
      const search = new URLSearchParams(screen.getByTestId('location-search').textContent ?? '')
      expect(search.has('new')).toBe(false)
      expect(search.has('metric')).toBe(false)
      expect(search.get('section')).toBe('monitors')
    })
    // Stripping the params does not close the form that they opened.
    expect(within(screen.getByRole('dialog')).getByText('New alert rule')).toBeInTheDocument()
  })
})

describe('MonitorsSection rule editor', () => {
  it('limits rule names to the API and database width', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: /Add rule/ }))
    expect(screen.getByLabelText('Name')).toHaveAttribute(
      'maxLength',
      String(MAX_ALERT_RULE_NAME_LENGTH),
    )
  })

  it('asks which destination a new rule routes to, now that no card answers it', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({
      destinations: [makeDestination(), makeDestination({ id: 'dest-2', name: 'Slack', type: 'slack' })],
    })

    fireEvent.click(await screen.findByRole('button', { name: /Add rule/ }))

    expect(screen.getByText('New alert rule')).toBeInTheDocument()
    expect(screen.getByLabelText('Destination')).toBeInTheDocument()
    // Nothing routes anywhere until one is named — the API addresses the rule
    // through its destination. Create says so on submit instead of sitting
    // disabled with no reason.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New rule' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(screen.getByText('Pick a destination.')).toBeInTheDocument()
  })

  it('preselects the only enabled destination', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({
      destinations: [
        makeDestination(),
        makeDestination({ id: 'dest-2', name: 'Old Slack', type: 'slack', enabled: false }),
      ],
    })

    fireEvent.click(await screen.findByRole('button', { name: /Add rule/ }))

    expect(screen.getByRole('combobox', { name: 'Destination' })).toHaveTextContent('TG')
  })

  it('says a rule was created, like a destination does', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    vi.spyOn(alertingApi, 'createRule').mockResolvedValue(
      makeRule({ id: 'rule-9', name: 'Checkout drops' }) as AlertRule,
    )
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: /Add rule/ }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Checkout drops' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Rule "Checkout drops" created'))
  })
})

describe('MonitorsSection panel subtitle', () => {
  it('counts the rules with the noun attached, not a bare "2 routing"', async () => {
    // The subtitle was `${rules.length} routing` — a count whose noun never got
    // written — sitting directly above a table of numbers, where an unfinished
    // template is exactly the thing a reader cannot un-see.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ rules: [makeRule(), makeRule({ id: 'rule-2', name: 'Weekly health' })] })

    expect(await screen.findByText('2 alert rules')).toBeInTheDocument()
  })

  it('agrees with a single rule', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    expect(await screen.findByText('1 alert rule')).toBeInTheDocument()
  })
})

describe('MonitorsSection empty states', () => {
  it('sends a project with no channels to Destinations, not to a rule form', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(
      makeSummary({ monitors: [], firing_count: 0, total: 0 }),
    )
    const goToDestinations = vi.fn()
    renderSection({ rules: [], destinations: [], onGoToDestinations: goToDestinations })

    fireEvent.click(await screen.findByRole('button', { name: 'Add a destination' }))

    expect(goToDestinations).toHaveBeenCalled()
  })

  it('hides the all-zero rollup when there is nothing to roll up', () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(
      makeSummary({ monitors: [], firing_count: 0, total: 0 }),
    )
    renderSection({ rules: [] })

    expect(screen.queryByText('Firing')).toBeNull()
    expect(screen.getByText('No rules yet')).toBeInTheDocument()
  })
})

describe('MonitorsSection viewer gating', () => {
  // Mute and Delete sit behind "More actions"; the menu itself is the
  // write control a viewer must not get.
  const WRITE_CONTROLS = ['More actions for Prod drops', 'Edit rule Prod drops']

  it('offers an editor every write control', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    for (const name of WRITE_CONTROLS) {
      expect(await screen.findByRole('button', { name })).toBeEnabled()
    }
    expect(screen.getByRole('switch', { name: 'Toggle Prod drops' })).toBeEnabled()
  })

  it('offers a viewer none of them, and says so once', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ canWrite: false })

    await screen.findByRole('link', { name: 'Prod drops' })
    for (const name of WRITE_CONTROLS) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.queryByRole('switch', { name: 'Toggle Prod drops' })).toBeNull()
  })

  it('keeps replay, which the API does not gate because it saves nothing', async () => {
    // The one control here with no EditorUserDep on its route: asking "would a
    // stricter threshold have cut this noise" changes nothing.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ canWrite: false })

    expect(await screen.findByRole('button', { name: 'Replay Prod drops' })).toBeEnabled()
  })
})

/**
 * A scope switched on that nothing can feed.
 *
 * Production had both drift scopes enabled on its only monitor while no scan
 * watched a column and no variable documented a value list, so the editor
 * showed two switches wired to nothing. The negative cases matter as much as
 * the positive one: a warning painted over a healthy project, or over a
 * response that has not arrived yet, is worse than the silence it replaces.
 */
describe('MonitorsSection inert scope notice', () => {
  const DISTRIBUTION_SENTENCE = /no scan in this project watches a column for it/

  async function openEditorFor(rule: RuleWithDestination, summary: MonitorsSummaryResponse) {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(summary)
    renderSection({ rules: [rule] })
    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule Prod drops' }))
    return screen.findByText('Edit alert rule')
  }

  it('names the missing scan setting, and links to the screen that supplies it', async () => {
    await openEditorFor(
      makeRule({ include_distribution_drifts: true }),
      makeSummary({ scope_readiness: { variable_value_drift: true, distribution_drift: false } }),
    )

    expect(await screen.findByText(DISTRIBUTION_SENTENCE)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Scan settings' })).toHaveAttribute(
      'href',
      '/p/acme-ios/scans',
    )
  })

  it('keeps that link project-level even for a rule narrowed to one scan', async () => {
    // The tripwire for the scan-bound link. The monitor detail now aims this same link
    // at the scan its rule is bound to, because there the binding is SAVED. Here
    // it is a draft the picker two fields up can change at any keystroke, while
    // `scopeReadiness` is the project's answer fetched once — so narrowing the
    // link would promise a per-scan verdict this dialog does not have, and the
    // editor and the detail would disagree about one rule. If this goes red, the
    // per-scan destination has leaked into the editor.
    await openEditorFor(
      makeRule({ include_distribution_drifts: true, scan_config_id: 'scan-7' }),
      makeSummary({ scope_readiness: { variable_value_drift: true, distribution_drift: false } }),
    )

    expect(await screen.findByText(DISTRIBUTION_SENTENCE)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Scan settings' })).toHaveAttribute(
      'href',
      '/p/acme-ios/scans',
    )
  })

  it('opens that link in a new tab, so the half-built rule survives the click', async () => {
    // The dialog is modal and its draft is component state — Escape, Cancel and
    // a same-tab navigation all discard it. This link is the only navigation
    // inside the form, and it exists to be followed.
    await openEditorFor(
      makeRule({ include_distribution_drifts: true }),
      makeSummary({ scope_readiness: { variable_value_drift: true, distribution_drift: false } }),
    )

    await screen.findByText(DISTRIBUTION_SENTENCE)
    expect(screen.getByRole('link', { name: 'Scan settings' })).toHaveAttribute('target', '_blank')
    expect(screen.getByText('Edit alert rule')).toBeInTheDocument()
  })

  it('leaves the toggle usable, because the precondition can be met later', async () => {
    // Disabling it would make the problem unfixable from the one screen that
    // reports it: the reader would be told what is wrong and denied the switch.
    await openEditorFor(
      makeRule({ include_distribution_drifts: true }),
      makeSummary({ scope_readiness: { variable_value_drift: true, distribution_drift: false } }),
    )

    await screen.findByText(DISTRIBUTION_SENTENCE)
    const box = within(screen.getByText('Distribution drift').closest('label')!).getByRole('checkbox')
    expect(box).not.toBeDisabled()
    expect(box).toBeChecked()
  })

  it('says nothing about a scope that has source data', async () => {
    await openEditorFor(
      makeRule({ include_distribution_drifts: true, include_variable_value_drifts: true }),
      makeSummary(),
    )

    expect(screen.queryByText(DISTRIBUTION_SENTENCE)).toBeNull()
    expect(screen.queryByText(/documents an allowed-values list/)).toBeNull()
  })

  it('says nothing about a scope the rule has not switched on', async () => {
    // The project being unable to feed a scope is only worth saying to someone
    // who asked for that scope.
    await openEditorFor(
      makeRule({ include_distribution_drifts: false }),
      makeSummary({ scope_readiness: { variable_value_drift: false, distribution_drift: false } }),
    )

    expect(screen.queryByText(DISTRIBUTION_SENTENCE)).toBeNull()
  })

  it('stays silent while the readiness request is still in flight', async () => {
    // `scopeReadiness` is undefined until monitors-summary answers. A form must
    // not accuse a project on the strength of a value it does not have.
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockReturnValue(new Promise(() => {}))
    renderSection({ rules: [makeRule({ include_distribution_drifts: true })] })

    fireEvent.click(screen.getByRole('button', { name: 'Edit rule Prod drops' }))

    expect(await screen.findByText('Edit alert rule')).toBeInTheDocument()
    expect(screen.queryByText(DISTRIBUTION_SENTENCE)).toBeNull()
  })
})

describe('MonitorsSection row writes that fail say why', () => {
  it('toasts a refused enable switch, prefix stripped', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    vi.spyOn(alertingApi, 'updateRule').mockRejectedValue(new Error('Value error, Editor role required'))
    renderSection()

    fireEvent.click(await screen.findByRole('switch', { name: 'Toggle Prod drops' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Editor role required', expect.anything()))
  })

  it('toasts a refused delete, and does not send a second one while the first is in flight', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    let reject: (error: Error) => void = () => {}
    const remove = vi.spyOn(alertingApi, 'deleteRule').mockImplementation(
      () => new Promise((_, fail) => { reject = fail }),
    )
    renderSection()

    await openRowMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete rule Prod drops' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1))
    await openRowMenu()
    expect(screen.getByRole('menuitem', { name: 'Delete rule Prod drops' }))
      .toHaveAttribute('aria-disabled', 'true')

    reject(new Error('Rule not found'))
    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Rule not found', expect.anything()))
  })

  it('toasts a refused mute', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    vi.spyOn(alertingApi, 'muteMonitor').mockRejectedValue(new Error('Forbidden'))
    renderSection()

    await openRowMenu()
    fireEvent.click(screen.getAllByRole('menuitem', { name: /^Mute Prod drops for / })[0]!)

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Forbidden', expect.anything()))
  })

  // Silencing the global backstop to strip the prefix must not also drop what
  // the backstop did: the support reference, and quiet on a 401.
  it('quotes the request id of a failed row write for support', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    vi.spyOn(alertingApi, 'updateRule').mockRejectedValue(
      new ApiError('Internal server error', 500, 'req-123'),
    )
    renderSection()

    fireEvent.click(await screen.findByRole('switch', { name: 'Toggle Prod drops' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'Internal server error\nReference: req-123',
        expect.objectContaining({ id: expect.any(String) }),
      ),
    )
  })

  it('leaves a 401 to the re-auth flow instead of toasting it', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const update = vi.spyOn(alertingApi, 'updateRule').mockRejectedValue(
      new ApiError('Not authenticated', 401),
    )
    renderSection()

    fireEvent.click(await screen.findByRole('switch', { name: 'Toggle Prod drops' }))

    await waitFor(() => expect(update).toHaveBeenCalled())
    // Let the rejection settle before asserting the absence of a toast.
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Toggle Prod drops' })).toBeEnabled())
    expect(toastError).not.toHaveBeenCalled()
  })
})

describe('MonitorsSection rule row details', () => {
  it('describes the delete cascade with the shared, pluralised sentence', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({ rules: [makeRule({ total_deliveries: 1, incident_count: 1 })] })

    // Written on the menu item itself, under its label — visible to keyboard
    // and touch alike, where a tooltip or `title` was not.
    await openRowMenu()
    expect(screen.getByRole('menuitem', { name: 'Delete rule Prod drops' }))
      .toHaveTextContent('1 delivery and 1 incident')
  })

  it('links the settings toggle to the row it opens, which spans the table', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    const toggle = await screen.findByRole('button', { name: 'Show settings for Prod drops' })
    expect(toggle).not.toHaveAttribute('aria-controls')
    fireEvent.click(toggle)

    const controls = screen.getByRole('button', { name: 'Hide settings for Prod drops' })
      .getAttribute('aria-controls')
    expect(controls).toBeTruthy()
    const row = document.getElementById(controls!)
    expect(row).toHaveAttribute('role', 'row')
    expect(within(row!).getByRole('cell')).toHaveAttribute('aria-colspan', '6')
  })

  it('says the scan is loading, not unknown, until the scan list answers', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const { unmount } = renderSection({
      rules: [makeRule({ scan_config_id: 'scan-1' })],
      scansLoaded: false,
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Show settings for Prod drops' }))
    expect(screen.getByText('…')).toBeInTheDocument()
    expect(screen.queryByText('unknown scan')).toBeNull()
    unmount()

    renderSection({ rules: [makeRule({ scan_config_id: 'scan-1' })], scansLoaded: true })
    fireEvent.click(await screen.findByRole('button', { name: 'Show settings for Prod drops' }))
    expect(screen.getByText('unknown scan')).toBeInTheDocument()
  })

  it('says the scan is unavailable, not loading, when the scan list failed', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({
      rules: [makeRule({ scan_config_id: 'scan-1' })],
      scansLoaded: false,
      scansFailed: true,
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Show settings for Prod drops' }))

    expect(screen.getByText('scan unavailable')).toBeInTheDocument()
    expect(screen.queryByText('…')).toBeNull()
  })
})

describe('MonitorsSection rule form — what is saved is what is shown', () => {
  // Radix Select drives selection through pointer capture, which jsdom omits.
  beforeAll(() => {
    if (!Element.prototype.hasPointerCapture) {
      Element.prototype.hasPointerCapture = () => false
    }
    if (!Element.prototype.releasePointerCapture) {
      Element.prototype.releasePointerCapture = () => {}
    }
  })

  it('drops a format the new destination does not support when the destination changes', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const create = vi.spyOn(alertingApi, 'createRule').mockResolvedValue(makeRule() as AlertRule)
    renderSection({
      destinations: [makeDestination(), makeDestination({ id: 'dest-2', name: 'Slack', type: 'slack' })],
    })

    fireEvent.click(await screen.findByRole('button', { name: /Add rule/ }))
    // No format is offered before a destination is: the choices are per channel.
    expect(screen.queryByRole('combobox', { name: 'Message format' })).toBeNull()

    fireEvent.click(screen.getByRole('combobox', { name: 'Destination' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Slack · Slack' }))
    // The templates are collapsed by default.
    fireEvent.click(screen.getByRole('button', { name: /Customize message/ }))
    fireEvent.click(screen.getByRole('combobox', { name: 'Message format' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Slack mrkdwn' }))
    expect(screen.getByRole('combobox', { name: 'Message format' })).toHaveTextContent('Slack mrkdwn')

    fireEvent.click(screen.getByRole('combobox', { name: 'Destination' }))
    fireEvent.click(await screen.findByRole('option', { name: 'TG · Telegram' }))

    expect(screen.getByRole('combobox', { name: 'Message format' })).toHaveTextContent('Plain text')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New rule' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(create.mock.calls[0]![1]).toBe('dest-1')
    expect(create.mock.calls[0]![2]).toMatchObject({ message_format: 'plain', message_template: null })
  })

  it('reopens without the previous attempt\'s error', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    vi.spyOn(alertingApi, 'updateRule').mockRejectedValue(new Error('Rule limit reached'))
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule Prod drops' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Rule limit reached')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule Prod drops' }))

    expect(await screen.findByText('Edit alert rule')).toBeInTheDocument()
    expect(screen.queryByText('Rule limit reached')).toBeNull()
  })

  it('offers a replay of the saved rule from inside its editor', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    // The replay runs on open; held in flight, it is not what this asserts.
    vi.spyOn(alertingApi, 'simulateRule').mockReturnValue(new Promise(() => {}))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule Prod drops' }))
    fireEvent.click(screen.getByRole('button', { name: 'Replay saved rule' }))

    expect(await screen.findByText(/Replay rule “Prod drops”/)).toBeInTheDocument()
  })

  it('replays the edits on the form, unsaved, once there are any', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    const update = vi.spyOn(alertingApi, 'updateRule')
    const simulate = vi.spyOn(alertingApi, 'simulateRule').mockResolvedValue({
      rule_id: 'rule-1',
      rule_name: 'Prod drops',
      days: 7,
      window_from: '2026-07-12T10:00:00Z',
      window_to: '2026-07-19T10:00:00Z',
      anomalies_considered: 0,
      matched_before_cooldown: 0,
      noisy: false,
      cooldown_minutes_used: 45,
      cooldown_minutes_saved: 60,
      min_percent_delta_used: 0,
      min_percent_delta_saved: 0,
      min_expected_count_used: 0,
      min_expected_count_saved: 0,
      sigma_threshold_used: null,
      sigma_threshold_saved: null,
      rendered_message: null,
      firings: [],
    })
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule Prod drops' }))
    // No edits yet: only the saved rule can be replayed.
    expect(screen.queryByRole('button', { name: 'Replay with these edits' })).toBeNull()

    // The saved 360 minutes shows as 6 hours; 45 of them is 2700 minutes.
    fireEvent.change(screen.getByLabelText(/re-alert the same scope for/), { target: { value: '45' } })
    fireEvent.click(screen.getByRole('button', { name: 'Replay with these edits' }))

    expect(
      await screen.findByRole('heading', { name: /Replay rule “Prod drops” with your unsaved edits/ }),
    ).toBeInTheDocument()
    // It runs on open; no second click needed.
    await waitFor(() => expect(simulate).toHaveBeenCalled())
    expect(simulate.mock.calls[0]?.[5]).toMatchObject({ cooldown_minutes: 45 * 60 })
    expect(update).not.toHaveBeenCalled()
  })
})

describe('MonitorsSection — detection switched off', () => {
  it('says above the rules that none of them can fire, and links to the switch', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    mockDetection(false)
    renderSection()

    expect(await screen.findByText(/Detection is off for this project/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Detection settings' })).toHaveAttribute(
      'href',
      '/p/acme-ios/settings/monitoring',
    )
  })

  it('says nothing while detection is on', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    await screen.findByRole('link', { name: 'Prod drops' })
    await waitFor(() => expect(anomalySettingsApi.get).toHaveBeenCalled())
    expect(screen.queryByText(/Detection is off for this project/)).toBeNull()
  })
})

describe('MonitorsSection — rows read as sentences', () => {
  it('writes the condition in words and names what a metrics-only rule watches', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection({
      rules: [makeRule({ include_project_total: false, include_metrics: true, notify_on_spike: true })],
    })

    const table = await screen.findByRole('table', { name: 'Alert rules' })
    expect(within(table).getByText('Spikes & drops ≥ 100% · 6h cooldown')).toBeInTheDocument()
    expect(within(table).getByText('Metrics')).toBeInTheDocument()
    // No raw channel type chip.
    expect(within(table).queryByText('telegram')).toBeNull()
  })

  it('keeps the row actions to a switch, Edit and one labelled menu', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(makeSummary())
    renderSection()

    await screen.findByRole('link', { name: 'Prod drops' })
    // No bell-with-slash on an unmuted row: mute is an action in the menu.
    expect(screen.queryByRole('button', { name: 'Mute Prod drops' })).toBeNull()
    const menu = await openRowMenu()
    expect(within(menu).getByRole('menuitem', { name: 'Replay Prod drops' })).toHaveTextContent('Replay')
  })
})

// "Firing 1" is exactly the filter a reader wants, and the strip used to be a
// row of numbers nothing could be done with.
describe('MonitorsSection state tiles filter the rules', () => {
  function twoRuleSummary() {
    const firing = makeSummary().monitors[0]!
    return makeSummary({
      monitors: [
        firing,
        { ...firing, rule_id: 'rule-2', rule_name: 'Quiet spikes', status: 'healthy', firing_scope_count: 0 },
      ],
      firing_count: 1,
      healthy_count: 1,
      total: 2,
    })
  }
  const twoRules = () => [makeRule(), makeRule({ id: 'rule-2', name: 'Quiet spikes' })]

  it('shows only the rules in the pressed state, and all of them again on a second press', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(twoRuleSummary())
    renderSection({ rules: twoRules() })

    const firing = await screen.findByRole('button', { name: /^Firing 1/ })
    expect(firing).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(firing)

    expect(firing).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('link', { name: 'Prod drops' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Quiet spikes' })).toBeNull()
    expect(screen.getByText('1 of 2 alert rules · firing')).toBeInTheDocument()

    fireEvent.click(firing)
    expect(firing).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('link', { name: 'Quiet spikes' })).toBeInTheDocument()
  })

  it('says when no rule is in the pressed state, with a way back to all of them', async () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockResolvedValue(twoRuleSummary())
    renderSection({ rules: twoRules() })

    fireEvent.click(await screen.findByRole('button', { name: /^Warning 0/ }))

    expect(screen.getByText(/No rules are warning right now/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show all rules' }))
    expect(screen.getByRole('link', { name: 'Prod drops' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Quiet spikes' })).toBeInTheDocument()
  })

  it('is not a toggle until the summary has answered', () => {
    vi.spyOn(alertingApi, 'getMonitorsSummary').mockReturnValue(new Promise(() => {}))
    renderSection({ rules: twoRules() })

    expect(screen.queryByRole('button', { name: /^Firing/ })).toBeNull()
  })
})
