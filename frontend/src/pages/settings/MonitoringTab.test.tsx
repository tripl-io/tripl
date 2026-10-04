import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AuthContext } from '@/components/auth-context'
import { MonitoringTab } from './MonitoringTab'
import { personaAuth } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function settingsPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: 'settings-1',
    project_id: 'proj-1',
    anomaly_detection_enabled: true,
    detect_project_total: true,
    detect_event_types: true,
    detect_events: true,
    detect_metrics: true,
    baseline_window_buckets: 168,
    min_history_buckets: 24,
    sigma_threshold: 3,
    min_expected_count: 100,
    recent_signal_window_hours: 24,
    anomaly_ingestion_settling_minutes: 120,
    holiday_country: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function scopeOverridePayload(overrides: Record<string, unknown> = {}) {
  return {
    id: 'override-1',
    scan_config_id: 'scan-1',
    scan_config_name: 'Events',
    scope_type: 'event',
    scope_ref: 'event-1',
    scope_name: 'checkout_started',
    sigma_threshold: 4.5,
    min_expected_count: 55,
    false_positive_count: 1,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function mockSettingsFetch(
  overrides: Record<string, unknown> = {},
  scopeOverrides: Record<string, unknown>[] = [],
) {
  const patches: unknown[] = []
  const deletes: string[] = []
  let remaining = scopeOverrides
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url.includes('/anomaly-settings/holiday-countries')) {
      return jsonResponse(['DE', 'FR', 'US'])
    }
    if (url.includes('/anomaly-settings/scope-overrides')) {
      if (init?.method === 'DELETE') {
        const id = url.split('/').pop() ?? ''
        deletes.push(id)
        remaining = remaining.filter(item => item.id !== id)
        return new Response(null, { status: 204 })
      }
      return jsonResponse({ items: remaining, total: remaining.length })
    }
    if (url.includes('/anomaly-settings')) {
      if (init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body))
        patches.push(body)
        return jsonResponse(settingsPayload({ ...overrides, ...body }))
      }
      return jsonResponse(settingsPayload(overrides))
    }
    throw new Error(`Unhandled fetch: ${url}`)
  })
  return { patches, deletes }
}

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MonitoringTab slug="demo" />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('MonitoringTab — catalog metric detection', () => {
  it('exposes the metric scope alongside the other three', async () => {
    // detect_metrics has always defaulted to on with no control anywhere, so the
    // only way to stop scoring catalog metrics was to disable detection wholesale.
    mockSettingsFetch()
    renderTab()

    const metrics = await screen.findByLabelText('Metrics')
    expect(metrics).toBeChecked()
  })

  it('reflects a disabled metric scope from the server', async () => {
    mockSettingsFetch({ detect_metrics: false })
    renderTab()

    expect(await screen.findByLabelText('Metrics')).not.toBeChecked()
    // The neighbouring scopes stay independent.
    expect(screen.getByLabelText('Events')).toBeChecked()
  })

  it('patches detect_metrics when toggled off', async () => {
    const { patches } = mockSettingsFetch()
    renderTab()

    fireEvent.click(await screen.findByLabelText('Metrics'))

    await waitFor(() => expect(patches).toHaveLength(1))
    expect(patches[0]).toEqual({ detect_metrics: false })
  })
})

describe('MonitoringTab — history fields carry their unit', () => {
  // "Baseline Window 14" sat directly above two fields that name their unit and
  // explain themselves, so the page's own pattern read the bare number as days
  // when it means buckets — an order of magnitude on an hourly scan.
  it('names buckets in both labels rather than leaving the number bare', async () => {
    mockSettingsFetch({ baseline_window_buckets: 14, min_history_buckets: 7 })
    renderTab()

    expect(await screen.findByLabelText('Baseline window (buckets)')).toHaveValue(14)
    expect(screen.getByLabelText('Min history (buckets)')).toHaveValue(7)
  })

  it('says what a bucket is, in the interval of the series and not of the scan', async () => {
    // These settings govern catalog metrics too, and a MetricDefinition carries
    // its own interval — so "one collection interval of the scan" would be wrong
    // for a daily metric under an hourly scan.
    mockSettingsFetch({ baseline_window_buckets: 14 })
    renderTab()

    const note = await screen.findByText(/A bucket is one collection interval of the series/i)
    expect(note).toHaveTextContent('14 buckets of baseline is 14 hours')
    expect(note).toHaveTextContent('daily catalog metric it is 14 days')
  })
})

describe('MonitoringTab — the form explains itself', () => {
  it('groups the scope checkboxes under a heading that says what checking one does', async () => {
    // Four bare checked boxes in a row decide what gets scored at all — the most
    // consequential control on the page — and the only rationale lived in a
    // source comment.
    mockSettingsFetch()
    renderTab()

    const legend = await screen.findByText('Score these scopes')
    expect(legend.tagName).toBe('LEGEND')
    const group = legend.closest('fieldset')
    expect(group).not.toBeNull()
    // The heading has to govern the boxes, not merely sit above them.
    expect(within(group!).getByLabelText('Project total')).toBeChecked()
    expect(within(group!).getByLabelText('Metrics')).toBeChecked()
    expect(screen.getByText(/Detection scores only the scopes checked here/i)).toBeInTheDocument()
  })

  it('explains the two settings that decide whether anything is flagged', async () => {
    // Sigma threshold and min expected count carried no help text at all, while
    // the least consequential field on the page carried a seven-line paragraph.
    mockSettingsFetch({ min_expected_count: 100 })
    renderTab()

    expect(
      await screen.findByText(/counted in standard deviations of that baseline/i),
    ).toBeInTheDocument()
    const floor = screen.getByText(/A floor on the baseline, not on the bucket/i)
    expect(floor).toHaveTextContent('expects fewer than 100 is skipped')
  })

  it('labels every field in sentence case, not four in Title Case and two not', async () => {
    mockSettingsFetch()
    renderTab()

    await screen.findByLabelText('Baseline window (buckets)')
    for (const label of [
      'Min history (buckets)',
      'Sigma threshold',
      'Min expected count',
      'Open signal window (hours)',
      'Ingestion settling (minutes)',
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
  })
})

describe('MonitoringTab — settling allowance vs open signal window', () => {
  // A settling allowance that reaches the open signal window blanks the
  // Anomalies page, the sidebar badge and the Overview stat while alerts keep
  // firing, so the backend refuses the pair. These bounds are how an operator
  // sees the constraint before hitting the 422.
  it('caps the settling field one minute under the stored window', async () => {
    mockSettingsFetch({ recent_signal_window_hours: 24 })
    renderTab()

    const settling = await screen.findByLabelText('Ingestion settling (minutes)')
    expect(settling).toHaveAttribute('max', '1439')
  })

  it('never lets the settling cap exceed the absolute 1440 limit', async () => {
    // A 30-day window would otherwise imply a 43199-minute ceiling.
    mockSettingsFetch({ recent_signal_window_hours: 720 })
    renderTab()

    const settling = await screen.findByLabelText('Ingestion settling (minutes)')
    expect(settling).toHaveAttribute('max', '1440')
  })

  it('floors the window field just above the stored allowance', async () => {
    // 120 minutes of settling needs a window of 3h: 2h is exactly 120 and the
    // two must not be equal.
    mockSettingsFetch({ anomaly_ingestion_settling_minutes: 120 })
    renderTab()

    const window = await screen.findByLabelText('Open signal window (hours)')
    expect(window).toHaveAttribute('min', '3')
  })

  it('names the paired setting in both hints', async () => {
    mockSettingsFetch({ recent_signal_window_hours: 24, anomaly_ingestion_settling_minutes: 120 })
    renderTab()

    expect(
      await screen.findByText(/the ceiling is one minute under the open signal window/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/would close every signal before it could be scored/i)).toBeInTheDocument()
  })

  it('surfaces the backend refusal instead of failing silently', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/anomaly-settings/holiday-countries')) return jsonResponse(['US'])
      if (url.includes('/anomaly-settings/scope-overrides')) {
        return jsonResponse({ items: [], total: 0 })
      }
      if (url.includes('/anomaly-settings')) {
        if (init?.method === 'PATCH') {
          return new Response(
            JSON.stringify({
              detail:
                'Ingestion settling (1440 min) must stay below the open signal window ' +
                '(24h = 1440 min).',
            }),
            { status: 422, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return jsonResponse(settingsPayload())
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    // Inside the bounds the page knows, so it is the server that refuses it.
    const settling = await screen.findByLabelText('Ingestion settling (minutes)')
    fireEvent.change(settling, { target: { value: '600' } })
    fireEvent.blur(settling)

    expect(
      await screen.findByText(/must stay below the open signal window/i),
    ).toBeInTheDocument()
    // And the refused number does not stay on screen as if it had been kept
    // (PLAN-55): the saved value comes back.
    await waitFor(() => expect(settling).toHaveValue(120))
  })

  it('refuses a value outside the bounds before sending it, and keeps it in view (PLAN-55, AL-44)', async () => {
    const { patches } = mockSettingsFetch({ recent_signal_window_hours: 24 })
    renderTab()

    const settling = await screen.findByLabelText('Ingestion settling (minutes)')
    fireEvent.change(settling, { target: { value: '5000' } })
    fireEvent.blur(settling)

    expect(await screen.findByText('5000 is out of range: use 0 to 1439. Not saved.')).toBeInTheDocument()
    // The refused number stays in the box beside the message, rather than a
    // valid-looking saved value sitting under a red "must be" (AL-44).
    expect(settling).toHaveValue(5000)
    expect(settling).toHaveAttribute('aria-invalid', 'true')
    expect(patches).toEqual([])

    // Typing a valid value clears the error at once.
    fireEvent.change(settling, { target: { value: '60' } })
    expect(settling).not.toHaveAttribute('aria-invalid')
    expect(screen.queryByText(/out of range/)).toBeNull()
  })

  it('names a value under the minimum as such', async () => {
    mockSettingsFetch()
    renderTab()

    const sigma = await screen.findByLabelText('Sigma threshold')
    fireEvent.change(sigma, { target: { value: '0' } })
    fireEvent.blur(sigma)

    expect(await screen.findByText('0 is below the minimum (0.1). Not saved.')).toBeInTheDocument()
    expect(sigma).toHaveValue(0)
  })

  it('says a committed value was saved (PLAN-55)', async () => {
    const { patches } = mockSettingsFetch()
    renderTab()

    const sigma = await screen.findByLabelText('Sigma threshold')
    fireEvent.change(sigma, { target: { value: '4' } })
    fireEvent.blur(sigma)

    await waitFor(() => expect(patches).toEqual([{ sigma_threshold: 4 }]))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
  })
})

describe('MonitoringTab — false-positive scope overrides', () => {
  it('says nothing is tightened when no scope has been ratcheted', async () => {
    mockSettingsFetch()
    renderTab()

    expect(
      await screen.findByText(/No scope has been tightened/i),
    ).toBeInTheDocument()
  })

  it('names the tightened scope and the values that replaced the project settings', async () => {
    mockSettingsFetch({}, [scopeOverridePayload()])
    renderTab()

    expect(await screen.findByText('checkout_started')).toBeInTheDocument()
    expect(screen.getByText(/Event · Events · sigma 4\.5 · min expected 55/)).toBeInTheDocument()
  })

  it('does not report a clean slate when the overrides request failed', async () => {
    // "No scope has been tightened" is a claim about the ratchet, and this card
    // is the ONLY way to undo it. A failed load rendering that sentence tells an
    // operator their scopes are untouched when nobody knows.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/anomaly-settings/holiday-countries')) return jsonResponse(['US'])
      if (url.includes('/anomaly-settings/scope-overrides')) {
        return new Response(JSON.stringify({ detail: 'Database is unavailable' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/anomaly-settings')) {
        return jsonResponse(settingsPayload())
      }
      throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
    })
    renderTab()

    expect(await screen.findByText(/Database is unavailable/)).toBeInTheDocument()
    expect(screen.queryByText(/No scope has been tightened/i)).toBeNull()
  })

  it('removes an override so the scope falls back to the project settings', async () => {
    // The ratchet is permanent and never decays, so this button is the ONLY way
    // back for a scope an operator tightened by mistake.
    const { deletes } = mockSettingsFetch({}, [scopeOverridePayload()])
    renderTab()

    fireEvent.click(await screen.findByLabelText('Remove override for checkout_started'))
    // One click used to do it; the confirm names what is lost (PLAN-55).
    const confirm = await screen.findByRole('alertdialog', { name: 'Remove scope override' })
    expect(deletes).toEqual([])
    fireEvent.click(within(confirm).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(deletes).toEqual(['override-1']))
    await waitFor(() =>
      expect(screen.getByText(/No scope has been tightened/i)).toBeInTheDocument(),
    )
  })
})

describe('MonitoringTab — a viewer reads the settings without changing them', () => {
  it('shows a read view instead of a disabled form, hides Remove, and says why once', async () => {
    mockSettingsFetch({}, [scopeOverridePayload()])
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={personaAuth('viewer')}>
          <PersonaProject persona="viewer">
            <MonitoringTab slug="demo" />
          </PersonaProject>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    // No controls that do nothing (#237 rule 4): the values, as a definition.
    expect(await screen.findByText('Scored scopes')).toBeInTheDocument()
    expect(screen.getByText('Project total, Event types, Events, Metrics')).toBeInTheDocument()
    expect(screen.getByText('168 buckets')).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Toggle signal detection' })).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByRole('spinbutton')).toBeNull()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(await screen.findByText('checkout_started')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Remove override/ })).not.toBeInTheDocument()
  })
})

describe('MonitoringTab — a failed settings load (PLAN-41)', () => {
  it('shows an error with a retry instead of loading forever', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/anomaly-settings/holiday-countries')) return jsonResponse(['US'])
      if (url.includes('/anomaly-settings/scope-overrides')) {
        return jsonResponse({ items: [], total: 0 })
      }
      if (url.includes('/anomaly-settings')) {
        return new Response(JSON.stringify({ detail: 'Database is unavailable' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    expect(await screen.findByText("Couldn't load detection settings")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText('Loading detection settings…')).not.toBeInTheDocument()
  })
})

describe('MonitoringTab — a failed refresh after an autosave (review 204)', () => {
  it('keeps the settings on screen and says the refresh failed', async () => {
    let getCount = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/anomaly-settings/holiday-countries')) return jsonResponse(['US'])
      if (url.includes('/anomaly-settings/scope-overrides')) {
        return jsonResponse({ items: [], total: 0 })
      }
      if (url.includes('/anomaly-settings')) {
        if (init?.method === 'PATCH') return jsonResponse(settingsPayload({ sigma_threshold: 4 }))
        getCount += 1
        return getCount === 1
          ? jsonResponse(settingsPayload())
          : new Response(JSON.stringify({ detail: 'Bad gateway' }), {
              status: 502,
              headers: { 'Content-Type': 'application/json' },
            })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    const sigma = await screen.findByLabelText('Sigma threshold')
    fireEvent.change(sigma, { target: { value: '4' } })
    fireEvent.blur(sigma)

    expect(await screen.findByText(/Couldn't refresh detection settings/)).toBeInTheDocument()
    expect(screen.getByLabelText('Sigma threshold')).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load detection settings")).not.toBeInTheDocument()
  })
})

describe('MonitoringTab — help text is one line, the rest on demand (AL-43)', () => {
  it('titles the two sections as sections and groups the fields', async () => {
    mockSettingsFetch()
    renderTab()

    expect(await screen.findByRole('heading', { level: 2, name: 'Detection' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Scope overrides' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Sensitivity' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Timing' })).toBeInTheDocument()
  })

  it('ties a one-line hint to each field', async () => {
    mockSettingsFetch()
    renderTab()

    expect(await screen.findByLabelText('Sigma threshold'))
      .toHaveAccessibleDescription('Higher is quieter; lower catches smaller moves.')
  })
})

describe('MonitoringTab — turning detection off (AL-45)', () => {
  it('asks first, and does nothing when the reader backs out', async () => {
    const { patches } = mockSettingsFetch()
    renderTab()

    fireEvent.click(await screen.findByRole('switch', { name: 'Toggle signal detection' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Stop detecting anomalies in this project?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(patches).toEqual([])
  })

  it('turns it off once confirmed', async () => {
    const { patches } = mockSettingsFetch()
    renderTab()

    fireEvent.click(await screen.findByRole('switch', { name: 'Toggle signal detection' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Stop detecting anomalies in this project?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Turn off detection' }))

    await waitFor(() => expect(patches).toEqual([{ anomaly_detection_enabled: false }]))
  })

  it('turns it back on without asking', async () => {
    const { patches } = mockSettingsFetch({ anomaly_detection_enabled: false })
    renderTab()

    fireEvent.click(await screen.findByRole('switch', { name: 'Toggle signal detection' }))

    await waitFor(() => expect(patches).toEqual([{ anomaly_detection_enabled: true }]))
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('keeps saying so while detection is off', async () => {
    mockSettingsFetch({ anomaly_detection_enabled: false })
    renderTab()

    expect(await screen.findByText(/Detection is off for this project/)).toBeInTheDocument()
  })
})

describe('MonitoringTab — holiday calendar (F18)', () => {
  it('lists countries by name and patches the chosen one', async () => {
    const { patches } = mockSettingsFetch()
    renderTab()

    const select = await screen.findByLabelText('Country')
    await waitFor(() => expect(select).not.toBeDisabled())
    expect(within(select).getByRole('option', { name: 'Germany (DE)' })).toBeInTheDocument()
    expect(select).toHaveValue('')

    fireEvent.change(select, { target: { value: 'DE' } })
    await waitFor(() => expect(patches).toEqual([{ holiday_country: 'DE' }]))
  })

  it('clears the calendar with None', async () => {
    const { patches } = mockSettingsFetch({ holiday_country: 'US' })
    renderTab()

    const select = await screen.findByLabelText('Country')
    await waitFor(() => expect(select).toHaveValue('US'))
    fireEvent.change(select, { target: { value: '' } })
    await waitFor(() => expect(patches).toEqual([{ holiday_country: null }]))
  })
})
