import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import MonitorDetailPage from './MonitorDetailPage'
import { PageHeader } from '@/components/primitives/page-header'
import { INDEFINITE_MUTE, MUTE_PRESETS, muteChoiceName } from '@/lib/mutePresets'
import { formatCooldown } from './alerting/constants'
import { at } from '@/test/at'
import { alertDeliveriesAnyKey } from '@/lib/queryKeys'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const BASE_MONITOR = {
  rule_id: 'rule-1',
  rule_name: 'payment_failed spike',
  destination_id: 'dest-1',
  destination_name: 'Main Slack',
  destination_type: 'slack',
  enabled: true,
  status: 'firing',
  active_scope_count: 2,
  firing_scope_count: 1,
  last_anomaly_at: '2026-06-26T10:00:00Z',
  last_notified_at: '2026-06-26T10:01:00Z',
  notify_on_spike: true,
  notify_on_drop: false,
  min_percent_delta: 50,
  min_expected_count: 100,
  // 360 is the value from the unit-agreement fix: the monitors side used to print it raw
  // as "360m" while the alerting side already said "6h" for the same rule.
  cooldown_minutes: 360,
  muted: false,
  muted_until: null,
  rule_enabled: true,
  destination_enabled: true,
  // Project-wide, the default every rule is created with. The scan-bound case
  // is its own describe block.
  scan_config_id: null,
  scan_name: null,
  include_project_total: true,
  include_event_types: true,
  include_events: false,
  include_schema_drifts: false,
  include_distribution_drifts: false,
  include_release_regressions: false,
  total_deliveries: 3,
  last_delivery_at: '2026-06-26T10:01:00Z',
  last_delivery_status: 'sent',
  // Both drift scopes have source data, so the ordinary fixture renders plain
  // chips. The inert case is its own describe block.
  scope_readiness: { variable_value_drift: true, distribution_drift: true },
}

const HISTORY = {
  items: [
    {
      id: 'del-1',
      project_id: 'proj-1',
      scan_config_id: 'scan-1',
      scan_job_id: 'job-1',
      destination_id: 'dest-1',
      rule_id: 'rule-1',
      destination_name: 'Main Slack',
      rule_name: 'payment_failed spike',
      scan_name: 'hourly events scan',
      status: 'failed',
      channel: 'slack',
      matched_count: 4,
      payload_snapshot: null,
      error_message: 'Scan failed: could not connect to the data source.',
      created_at: '2026-06-26T10:01:00Z',
      updated_at: '2026-06-26T10:01:00Z',
      sent_at: null,
    },
  ],
  total: 1,
}

type MockOptions = {
  monitor?: Record<string, unknown>
  muteResponse?: Record<string, unknown>
}

/** One scope the failed delivery matched, as its detail route returns it. */
const DELIVERY_ITEM = {
  id: 'item-1',
  delivery_id: 'del-1',
  scope_type: 'event',
  scope_ref: 'evt-9',
  scope_name: 'checkout_failed',
  event_type_id: null,
  event_id: 'evt-9',
  bucket: '2026-06-26T10:00:00Z',
  direction: 'spike',
  actual_count: 380,
  expected_count: 200,
  absolute_delta: 180,
  percent_delta: 90,
  details_path: null,
  monitoring_path: null,
  drift_field: null,
  drift_type: null,
  sample_value: null,
  correlation_group_id: null,
}

function mockApi(options: MockOptions = {}) {
  const monitor = options.monitor ?? BASE_MONITOR
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    if (/\/alert-deliveries\/del-1(\?|$)/.test(url) && method === 'GET') {
      return jsonResponse({ ...HISTORY.items[0], items: [DELIVERY_ITEM] })
    }
    if (url.includes('/alert-deliveries')) return jsonResponse(HISTORY)
    if (url.includes('/monitors/rule-1/mute')) {
      return jsonResponse(options.muteResponse ?? { ...monitor, muted: true, muted_until: '2099-01-01T00:00:00Z' })
    }
    if (url.includes('/monitors/rule-1/unmute')) {
      return jsonResponse({ ...monitor, muted: false, muted_until: null })
    }
    if (url.includes('/monitors/rule-1') && method === 'GET') return jsonResponse(monitor)
    throw new Error(`Unhandled fetch: ${method} ${url}`)
  })
}

function renderDetail(
  auth: AuthContextValue | null = null,
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <MemoryRouter initialEntries={['/p/demo/monitors/rule-1']}>
            <Routes>
              <Route path="/p/:slug/monitors/:monitorId" element={<MonitorDetailPage />} />
            </Routes>
          </MemoryRouter>
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

const VIEWER: AuthContextValue = personaAuth('viewer')

afterEach(() => {
  vi.restoreAllMocks()
})

/** One hour, as the shared preset list defines it. */
const HOUR_MS = 3_600_000

/**
 * How every mute surface names a mute button, spelled out once here — as a
 * literal, independently of the code under test.
 *
 * The guard this comment used to ask for now exists. It said "the only real
 * guard against the three surfaces drifting would be a shared name builder next
 * to `MUTE_PRESETS`"; that builder now exists, and this page's component no longer
 * writes the sentence at all — it calls `muteChoiceName` and `unmuteName` from
 * `@/lib/mutePresets`, as `MonitorsSection` and `AlertingInbox` now do.
 *
 * These helpers therefore STAY LOCAL, and stay literal, on purpose. Rewiring
 * them to import the builders is the obvious next step and would be a mistake:
 * a test that names a button with the very function the component labels it
 * with asserts nothing about the wording, because both sides move together. Kept
 * literal they are strictly stronger after the change than before — the
 * component's name is now produced by shared code, so a reword inside
 * `mutePresets.ts` fails HERE, in a file that never imported it. Three surface
 * test files hold that line independently, and `mutePresets.test.ts` holds it a
 * fourth time as the module's own oracle.
 *
 * Named `expected…` rather than `mutePresetName` / `unmuteName` so nothing in
 * this file can be read as, or shadowed by, the production builders that now
 * export those exact names.
 *
 * What else stays literal in this file, and why, is called out where it is
 * asserted: the preset labels '1h' / '24h' / '7d' and the count 3. The shared
 * module now needs a witness that is not itself.
 */
const expectedMutePresetName = (ruleName: string, presetLabel: string) =>
  `Mute ${ruleName} for ${presetLabel}`
const expectedUnmuteName = (ruleName: string) => `Unmute ${ruleName}`

/** The durations sit behind the header's "Mute" menu (MO-35). */
async function openMuteMenu() {
  fireEvent.keyDown(await screen.findByRole('button', { name: 'Mute' }), { key: 'Enter' })
  return screen.findByRole('menu')
}

/** The rule name `BASE_MONITOR` carries, and therefore the target every button names. */
const RULE = BASE_MONITOR.rule_name

describe('MonitorDetailPage', () => {
  it('renders the monitor condition, destination, recency and fired history', async () => {
    mockApi()

    renderDetail()

    expect(await screen.findByRole('heading', { name: 'payment_failed spike' })).toBeInTheDocument()
    // Condition config: the direction in words beside a coloured arrow, not a
    // black text triangle (MO-37).
    expect(screen.getByText('Spike')).toBeInTheDocument()
    expect(screen.queryByText(/▲/)).toBeNull()
    expect(screen.getByText('≥ 50% change')).toBeInTheDocument()
    expect(screen.getByText('6h between alerts')).toBeInTheDocument()
    expect(screen.getByText('Project total')).toBeInTheDocument()
    // Routing destination + recency
    expect(screen.getByText('Main Slack')).toBeInTheDocument()
    expect(screen.getByText('Last fired')).toBeInTheDocument()
    // Fired-history timeline surfaces this rule's deliveries
    expect(await screen.findByText('hourly events scan')).toBeInTheDocument()
    expect(screen.getByText('failed')).toBeInTheDocument()
    expect(
      screen.getByText('Scan failed: could not connect to the data source.'),
    ).toBeInTheDocument()
    // The eyebrow names the nav group and the collection, in place of the
    // separate "Monitors" back link above the header (DS-2 / MO-40).
    expect(screen.getByText('Observe · Alert rule')).toHaveAttribute('data-slot', 'page-eyebrow')
    expect(screen.queryByRole('link', { name: 'Monitors' })).not.toBeInTheDocument()
  })

  it('offers a viewer no mute or retry, and says who can (MON-6)', async () => {
    mockApi()

    renderDetail(VIEWER)

    // The failed delivery is on screen; only its Retry is withheld.
    expect(await screen.findByText('hourly events scan')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mute' })).not.toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    // Nothing to edit from here either: the rule editor is an editor's.
    expect(screen.queryByRole('link', { name: /Edit rule/ })).not.toBeInTheDocument()
  })

  it('links an editor to the rule editor', async () => {
    mockApi()

    renderDetail()

    expect(await screen.findByRole('link', { name: /Edit rule/ })).toHaveAttribute(
      'href',
      '/p/demo/alerting?section=monitors',
    )
  })

  // The Condition panel described everything that narrows a rule except the
  // first thing that does. A reader told by the docs to go and check the bound
  // scan's own drift settings could not learn from this screen which scan that
  // was — the only scan it ever named was a delivery's.
  it('names the scan a rule is narrowed to', async () => {
    mockApi({
      monitor: { ...BASE_MONITOR, scan_config_id: 'scan-7', scan_name: 'ios hourly' },
    })

    renderDetail()

    await screen.findByRole('heading', { name: RULE })
    expect(screen.getByText('Scan')).toBeInTheDocument()
    expect(screen.getByText('ios hourly')).toBeInTheDocument()
    expect(screen.queryByText('All scans')).toBeNull()
  })

  it('spells the project-wide default out rather than leaving the row blank', async () => {
    // A null here means "every scan in the project", not "unknown". An empty
    // value would read as a missing fact and send the reader looking for one.
    mockApi()

    renderDetail()

    await screen.findByRole('heading', { name: RULE })
    expect(screen.getByText('All scans')).toBeInTheDocument()
  })

  // Pins the unit agreement fix: this page and the alerting
  // destinations card described one rule's cooldown two different ways ("360m"
  // vs "6h"). Asserting against `formatCooldown` itself — the helper the
  // alerting side already renders — means the two screens cannot drift apart
  // again without this failing.
  // 90 stays in minutes because it is not a whole number of hours; 1 is the
  // smallest cooldown the API accepts (`ge=1`), so 0 is unreachable here.
  it.each([
    [1, '1m'],
    [30, '30m'],
    [60, '1h'],
    [90, '90m'],
    [360, '6h'],
    [1440, '1d'],
  ])('renders a %i-minute cooldown as "%s between alerts"', async (minutes, expected) => {
    expect(formatCooldown(minutes)).toBe(expected)
    mockApi({ monitor: { ...BASE_MONITOR, cooldown_minutes: minutes } })

    renderDetail()

    await screen.findByRole('heading', { name: 'payment_failed spike' })
    expect(screen.getByText(`${expected} between alerts`)).toBeInTheDocument()
  })

  it('never prints a raw minute count once the formatter has a coarser unit', async () => {
    mockApi({ monitor: { ...BASE_MONITOR, cooldown_minutes: 360 } })

    renderDetail()

    await screen.findByRole('heading', { name: 'payment_failed spike' })
    expect(screen.queryByText('360m between alerts')).toBeNull()
  })

  it('mutes the monitor for a preset duration, at the exact instant that preset means', async () => {
    const fetchMock = mockApi()

    renderDetail()

    await screen.findByRole('heading', { name: 'payment_failed spike' })

    // The clock is pinned across the click only. This page used to own a
    // private `futureIso` beside a private copy of the preset list; the rewire
    // onto the shared `muteUntilIso` is only behaviour-preserving
    // if the instant is identical, so assert the instant rather than merely
    // "some time in the future" — which would also pass if the wrong number
    // were passed in, or the label, or an already-absolute timestamp.
    await openMuteMenu()
    const clickedAt = Date.parse('2026-08-14T10:00:00Z')
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(clickedAt)
    // Queried by the name a screen reader hears, not by the "For 1h" on its
    // face: the item is reachable by either, and picking the accessible one
    // means this test also breaks if the label ever stops naming its target.
    fireEvent.click(screen.getByRole('menuitem', { name: expectedMutePresetName(RULE, '1h') }))
    nowSpy.mockRestore()

    await waitFor(() => {
      const muteCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/mute'))
      if (!muteCall) throw new Error('no /mute request was sent')
      const body = JSON.parse(String((muteCall[1] as RequestInit).body)) as { muted_until: string }
      expect(body.muted_until).toBe(new Date(clickedAt + HOUR_MS).toISOString())
    })

    // After a successful mute the control flips to Unmute — which names the
    // monitor it will un-silence, exactly as the preset it replaced did.
    expect(await screen.findByRole('button', { name: expectedUnmuteName(RULE) })).toBeInTheDocument()
  })

  it('offers the shared presets, and only those', async () => {
    mockApi()

    renderDetail()

    await screen.findByRole('heading', { name: 'payment_failed spike' })
    await openMuteMenu()

    // The three the shared module defines, unrenamed and unreordered — this is
    // what stops the rewire from quietly dropping or relabelling one. The
    // literals stay literal on purpose: deriving them from `MUTE_PRESETS` would
    // make a rename in the shared module invisible here, which is the one thing
    // this test was written to catch. Only the surrounding sentence moved when
    // the buttons started naming their target.
    for (const label of ['1h', '24h', '7d']) {
      expect(
        screen.getByRole('menuitem', { name: expectedMutePresetName(RULE, label) }),
      ).toBeInTheDocument()
    }
    // ONLY those — the count, not just the presence of the three. Without it a
    // fourth duration appended to `MUTE_PRESETS` would satisfy every assertion
    // in this file, and "and only those" in the title would be a claim no
    // assertion made.
    //
    // Matched with a predicate rather than a RegExp built by interpolation: the
    // target is a rule NAME, which users write, and one carrying `(`, `+` or `?`
    // would either throw or quietly match something else — silently disarming
    // the one assertion in the suite that counts these buttons.
    const presetPrefix = `Mute ${RULE} for `
    expect(
      screen.getAllByRole('menuitem', { name: (name: string) => name.startsWith(presetPrefix) }),
    ).toHaveLength(3)
    // …and no open-ended mute. A rule with a NULL `muted_until` is NOT muted
    // (`is_rule_muted`), so that button would do the opposite of its label here;
    // the permanent lever on a rule is the enable/disable switch.
    expect(screen.queryByRole('menuitem', { name: /until unmuted/i })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /Until I unmute/i })).toBeNull()
    // The same two negatives again, built from the shared module rather than
    // frozen — and this pair matters MORE now that the shared builder exists, for two reasons.
    //
    // First, staleness. A frozen negative stops matching anything the day the
    // open-ended phrasing is reworded: it then passes forever while guarding
    // nothing, and unlike the positive count above it fails silently rather
    // than loudly. Built from `muteChoiceName` it renames with the thing it
    // excludes. (The literals above stay as the independent witness that the
    // wording is still what this page's authors read; the two forms are
    // complementary, not redundant.)
    //
    // Second, the canary this page traded away. Its component used to write the
    // duration sentence as a literal, so an open-ended choice leaking in here
    // would have rendered the visibly broken "Mute <rule> for Until I unmute".
    // It now calls the shared builder, which HAS the open-ended branch, so the
    // same leak would read as grammatical English and could ship unnoticed.
    // This assertion is what replaces the broken grammar.
    expect(
      screen.queryByRole('menuitem', { name: muteChoiceName(RULE, INDEFINITE_MUTE) }),
    ).toBeNull()
    expect(screen.queryByText(INDEFINITE_MUTE.label)).toBeNull()
  })

  it('shows an unmute control for an already-muted monitor', async () => {
    mockApi({ monitor: { ...BASE_MONITOR, muted: true, muted_until: '2099-01-01T00:00:00Z' } })

    renderDetail()

    expect(await screen.findByRole('button', { name: expectedUnmuteName(RULE) })).toBeInTheDocument()
    expect(screen.getByText(/Muted until/)).toBeInTheDocument()
  })

  // The gap this closed: every button in this control was named only by
  // its own face — "1h", "24h", "7d", "Unmute" — so the announcement said how
  // long, never whose alerts. The two assertions below are deliberately a pair:
  // the positive one pins the wording the other surfaces use, the negative one
  // pins that the bare duration is GONE as a button name, which is what makes
  // three of these controls on one page tellable apart.
  it('names the monitor in every mute preset button, not just the duration', async () => {
    mockApi()

    renderDetail()

    await screen.findByRole('heading', { name: RULE })
    await openMuteMenu()

    for (const preset of MUTE_PRESETS) {
      expect(
        screen.getByRole('menuitem', { name: expectedMutePresetName(RULE, preset.label) }),
      ).toBeInTheDocument()
      // The duration alone is no longer a whole accessible name. It is still
      // the visible text, and still a substring of the name above — WCAG 2.5.3,
      // so "click 1h" keeps working for speech input.
      expect(screen.queryByRole('menuitem', { name: preset.label })).toBeNull()
    }
  })

  // Proves the target is READ from the monitor rather than hardcoded: a
  // literal 'payment_failed spike' baked into the label would satisfy every
  // other test in this file, since that is the only rule name they load.
  it.each<[string, boolean]>([
    ['checkout_error drop', false],
    ['checkout_error drop', true],
  ])('names %s in its own mute control (muted=%s)', async (ruleName, muted) => {
    mockApi({
      monitor: {
        ...BASE_MONITOR,
        rule_name: ruleName,
        muted,
        muted_until: muted ? '2099-01-01T00:00:00Z' : null,
      },
    })

    renderDetail()

    await screen.findByRole('heading', { name: ruleName })

    if (muted) {
      expect(screen.getByRole('button', { name: expectedUnmuteName(ruleName) })).toBeInTheDocument()
      return
    }
    await openMuteMenu()
    expect(
      screen.getByRole('menuitem', { name: expectedMutePresetName(ruleName, at(MUTE_PRESETS, 0).label) }),
    ).toBeInTheDocument()
    // …and never the previous monitor's name, or the fixture's.
    expect(screen.queryByRole('menuitem', { name: expectedMutePresetName(RULE, '1h') })).toBeNull()
  })

  it('says "still firing" beside the last-fired time instead of contradicting it (LIVE-18)', async () => {
    mockApi()
    renderDetail()

    await screen.findByRole('heading', { name: 'payment_failed spike' })
    expect(screen.getByText('still firing')).toBeInTheDocument()
    expect(screen.queryByText('now')).not.toBeInTheDocument()
  })

  it('says how many of the watched scopes are firing, once (MON-42, MO-36)', async () => {
    mockApi()
    renderDetail()

    await screen.findByRole('heading', { name: 'payment_failed spike' })
    // "1 of 2", not "2 · 1 firing": the number is said once.
    expect(screen.getByText('Firing scopes')).toBeInTheDocument()
    expect(screen.getByText('1 of 2')).toBeInTheDocument()
    expect(screen.queryByText('1 firing')).toBeNull()
  })

  it('lists which scopes are firing now, each linking to its drilldown (MO-36)', async () => {
    mockApi({
      monitor: {
        ...BASE_MONITOR,
        firing_scope_count: 2,
        firing_scopes: [
          {
            scope_type: 'event',
            scope_ref: 'evt-7',
            scan_config_id: 'scan-1',
            scope_name: 'payment_declined',
            event_id: 'evt-7',
            direction: 'drop',
            last_anomaly_bucket: '2026-06-26T10:00:00Z',
            last_notified_at: '2026-06-26T10:01:00Z',
          },
          {
            // Not notified yet: a cooldown held the first message back, so
            // there is no delivery to take a name or a direction from.
            scope_type: 'event_type',
            scope_ref: 'et-3',
            scan_config_id: 'scan-1',
            scope_name: null,
            event_id: null,
            direction: null,
            last_anomaly_bucket: '2026-06-26T10:00:00Z',
            last_notified_at: null,
          },
        ],
      },
    })
    renderDetail()

    expect(await screen.findByRole('heading', { name: 'Firing now' })).toBeInTheDocument()
    expect(screen.getByText('2 scopes')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /payment_declined/ })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/evt-7',
    )
    expect(screen.getByRole('img', { name: 'Drop' })).toBeInTheDocument()
    expect(screen.getByText('not notified yet')).toBeInTheDocument()
  })

  it('shows no "Firing now" panel when nothing fires', async () => {
    mockApi({ monitor: { ...BASE_MONITOR, status: 'healthy', firing_scope_count: 0, firing_scopes: [] } })
    renderDetail()

    await screen.findByRole('heading', { name: 'payment_failed spike' })
    expect(screen.queryByRole('heading', { name: 'Firing now' })).toBeNull()
  })

  it('opens a delivery\'s matched scopes, each linking to its chart (MO-36)', async () => {
    mockApi()
    renderDetail()

    const matched = await screen.findByRole('button', { name: /4 matched/ })
    expect(matched).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(matched)

    expect(matched).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByRole('link', { name: /checkout_failed/ })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/evt-9',
    )
  })

  it('puts the state beside the title and mute among the header actions (MO-35)', async () => {
    mockApi()
    renderDetail()

    const heading = await screen.findByRole('heading', { level: 1, name: RULE })
    // The chip is the title's sibling, on the title's line.
    expect(heading.parentElement).toHaveTextContent('Firing')
    expect(screen.getByRole('button', { name: 'Mute' })).toBeInTheDocument()
  })

  it('refreshes the Monitors list summary after a mute (MON-31)', async () => {
    mockApi()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderDetail(null, queryClient)

    await screen.findByRole('heading', { name: 'payment_failed spike' })
    await openMuteMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: expectedMutePresetName(RULE, '1h') }))

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['monitors-summary', 'demo'] }),
    )
    // The destination card's rule reads the same muted_until, and the Inbox
    // and delivery log are refreshed by the same alerting helper.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['alertDestinations', 'demo'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['alertInbox', 'demo'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['alertDeliveries', 'demo'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: alertDeliveriesAnyKey('demo') })
  })

  it('refreshes the Inbox and delivery probes after a successful retry (MON-31)', async () => {
    mockApi()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderDetail(null, queryClient)

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['alertInbox', 'demo'] }),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: alertDeliveriesAnyKey('demo') })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['alertDeliveries', 'demo'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['monitors-summary', 'demo'] })
  })

  it('says a failed retry failed, on the row, instead of silently re-arming the button (MON-31)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.includes('/alert-deliveries/del-1/retry')) {
        return jsonResponse({ detail: 'Slack rejected the webhook' }, 409)
      }
      if (url.includes('/alert-deliveries')) return jsonResponse(HISTORY)
      if (url.includes('/monitors/rule-1') && method === 'GET') return jsonResponse(BASE_MONITOR)
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/^Retry failed:/)
    // The button is back, ready for another go.
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
  })

  it('refreshes the monitor itself after a successful retry, so "Last delivery" is current (MON-31)', async () => {
    const fetchMock = mockApi()
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    await waitFor(() => {
      const monitorGets = fetchMock.mock.calls.filter(
        ([url, init]) => String(url).endsWith('/monitors/rule-1') && (init?.method ?? 'GET') === 'GET',
      )
      expect(monitorGets.length).toBeGreaterThanOrEqual(2)
    })
  })

  it('says a deleted rule is not found, with a way back instead of a retry (SH-33)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/alert-deliveries')) return jsonResponse(HISTORY)
      return jsonResponse({ detail: 'Not found' }, 404)
    })

    renderDetail()

    // "Alert rule", not "Monitor": one name for one object (JR-28).
    expect(await screen.findByText('Alert rule not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to alert rules' })).toHaveAttribute(
      'href',
      '/p/demo/alerting?section=monitors',
    )
    expect(screen.queryByRole('button', { name: /Try again|Retry/ })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps a retry for a rule that failed to load for another reason', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/alert-deliveries')) return jsonResponse(HISTORY)
      return jsonResponse({ detail: 'Internal server error' }, 500)
    })

    renderDetail()

    expect(await screen.findByText('Could not load this alert rule')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument()
  })

  it('shows the page shape while the rule loads, not a sentence', () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(() => {}))

    renderDetail()

    expect(screen.getByRole('status')).toHaveTextContent('Loading alert rule…')
  })
})

/**
 * A watched scope with nothing behind it.
 *
 * "Watching: Value drifts" is a promise, and in production it was one the
 * project could not keep — no variable documented an allowed-values list, so
 * the scope could never produce a candidate. The chip said the same thing
 * either way.
 */
describe('MonitorDetailPage inert scopes', () => {
  // Loose on purpose: the exact wording is pinned once, in InertScopeNotice's
  // own test. Repeating it here only bought a second file to edit whenever the
  // qualifiers the backend enforces changed.
  const VALUE_DRIFT_SENTENCE = /documents an allowed-values list/

  it('marks a watched scope the project cannot feed, and links to the fix', async () => {
    mockApi({
      monitor: {
        ...BASE_MONITOR,
        include_variable_value_drifts: true,
        scope_readiness: { variable_value_drift: false, distribution_drift: true },
      },
    })

    renderDetail()

    expect(await screen.findByText(VALUE_DRIFT_SENTENCE)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Properties' })).toHaveAttribute(
      'href',
      '/p/demo/variables',
    )
    // The chip carries the same sentence, so hovering the marked scope answers
    // the question the marking raises.
    expect(screen.getByText('Value drift')).toHaveAttribute(
      'title',
      expect.stringContaining('documents an allowed-values list'),
    )
  })

  it('opens the fix in this tab, because nothing here is unsaved', async () => {
    // The rule editor passes `newTab` to protect a draft it holds in state.
    // This page holds no unsaved anything, so a new tab would only litter the
    // reader's tab bar for nothing.
    mockApi({
      monitor: {
        ...BASE_MONITOR,
        include_variable_value_drifts: true,
        scope_readiness: { variable_value_drift: false, distribution_drift: true },
      },
    })

    renderDetail()

    expect(await screen.findByRole('link', { name: 'Properties' })).not.toHaveAttribute('target')
  })

  it('leaves a fed scope as a plain chip with nothing appended', async () => {
    mockApi({ monitor: { ...BASE_MONITOR, include_variable_value_drifts: true } })

    renderDetail()

    expect(await screen.findByText('Value drift')).not.toHaveAttribute('title')
    expect(screen.queryByText(VALUE_DRIFT_SENTENCE)).toBeNull()
  })

  it('sends a scan-bound reader to that scan, not to the scan list', async () => {
    // Note the fixture: distribution_drift readiness is FALSE, so nothing in the
    // project feeds the scope — this notice cannot render in the case where only
    // the bound scan is empty, and it is not the answer to that question
    // (readiness ships as a PROJECT verdict; the bound scan is named
    // separately in the Condition panel). What this pins is narrower and real:
    // when the notice DOES render for a scan-bound rule, it sends the reader to
    // that scan's own settings rather than making them find it in the list.
    mockApi({
      monitor: {
        ...BASE_MONITOR,
        include_distribution_drifts: true,
        scan_config_id: 'scan-7',
        scan_name: 'ios hourly',
        scope_readiness: { variable_value_drift: true, distribution_drift: false },
      },
    })

    renderDetail()

    expect(await screen.findByRole('link', { name: 'Scan settings' })).toHaveAttribute(
      'href',
      '/p/demo/scans/scan-7',
    )
  })

  it('sends a project-wide reader to the scan list, as before', async () => {
    mockApi({
      monitor: {
        ...BASE_MONITOR,
        include_distribution_drifts: true,
        scope_readiness: { variable_value_drift: true, distribution_drift: false },
      },
    })

    renderDetail()

    expect(await screen.findByRole('link', { name: 'Scan settings' })).toHaveAttribute(
      'href',
      '/p/demo/scans',
    )
  })

  it('says nothing when the response carries no readiness at all', async () => {
    // A server or fixture that predates the field must render exactly as
    // before: a missing fact is not a negative one.
    const { scope_readiness, ...withoutReadiness } = BASE_MONITOR
    // Assert the baseline really carries it, so this test is provably removing
    // something rather than asserting against a fixture that never had it.
    expect(scope_readiness).toBeDefined()
    mockApi({ monitor: { ...withoutReadiness, include_variable_value_drifts: true } })

    renderDetail()

    expect(await screen.findByText('Value drift')).toBeInTheDocument()
    expect(screen.queryByText(VALUE_DRIFT_SENTENCE)).toBeNull()
  })
})

// LIVE-11: the monitor detail used the kit's page head, a second title scale.
describe('MonitorDetailPage — the shared page header (LIVE-11)', () => {
  it('renders the same title element as the other Observe pages', async () => {
    mockApi()
    renderDetail()
    const heading = await screen.findByRole('heading', { level: 1, name: 'payment_failed spike' })

    const { container } = render(<PageHeader eyebrow="Observe" title="Metrics" />)
    const reference = container.querySelector('h1')
    expect(reference).not.toBeNull()
    expect(heading.className).toBe(reference?.className)
  })
})
