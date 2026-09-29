import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, RouterProvider, createMemoryRouter } from 'react-router-dom'
import type { Event as TEvent, EventType, MetaFieldDefinition, Project, Variable } from '@/types'
import { eventsApi } from '@/api/events'
import { planBranchesApi } from '@/api/planBranches'
import { scansApi } from '@/api/scans'
import { usersApi } from '@/api/users'
import { BranchContext } from '@/components/branch-context-internal'
import { DemoScenarioProvider } from '@/demo/DemoScenarioProvider'
import { readScenarioState, writeScenarioState } from '@/demo/scenarioModel'
import { chapterState } from '@/demo/scenarioTestState'
import { toast } from 'sonner'
import { EventForm } from './EventForm'
import { expectNoAxeViolations } from '@/test/axe'

vi.mock('@/api/events', () => ({
  eventsApi: {
    create: vi.fn(),
    update: vi.fn(),
    // The form probes the catalog for an event already holding the scan
    // identity it is about to claim. Defaulting to "nothing found" keeps every
    // other test on the path where the probe answers and finds no clash —
    // leaving it off the mock would make the query throw, and the tests would
    // pass for the wrong reason.
    byNames: vi.fn().mockResolvedValue({ items: [] }),
    // The successor picker's roster.
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    // Resolves the current successor when one is set. Only fired for a
    // deprecated event that names one, so most tests never reach it.
    get: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

// Duplicate detection (F12, #265) asks the catalog as the name is typed. No
// matches by default, so every other test stays on the quiet path.
vi.mock('@/api/duplicates', () => ({
  MAX_DUPLICATE_CANDIDATES: 500,
  duplicatesApi: {
    check: vi.fn().mockResolvedValue({ items: [], threshold: 0.88 }),
    clusters: vi.fn(),
    dismiss: vi.fn(),
  },
}))

vi.mock('@/api/users', () => ({
  usersApi: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}))
vi.mock('@/api/scans', () => ({
  scansApi: {
    list: vi.fn().mockResolvedValue([]),
  },
}))

// useAiStatus fires aiApi.status under the hood; stub it so no real request runs.
vi.mock('@/api/ai', () => ({
  aiApi: {
    status: vi.fn().mockResolvedValue({ enabled: false }),
    describeEvent: vi.fn(),
  },
}))

// Minimal fixtures: the event type has no field definitions so the form renders
// only the Details card (no FieldValueControl / MetaFieldControl branches).
const EVENT_TYPE = {
  id: 'et-1',
  name: 'checkout',
  display_name: 'Checkout',
  field_definitions: [],
} as unknown as EventType

const EXISTING_EVENT = {
  id: 'ev-1',
  event_type_id: 'et-1',
  name: 'checkout:completed',
  description: '',
  status: 'draft',
  sunset_at: null,
  metric_breakdown_columns: [],
  tags: [],
  field_values: [],
  meta_values: [],
} as unknown as TEvent

const TEMPLATE_EVENT_TYPE = {
  ...EVENT_TYPE,
  field_definitions: [
    {
      id: 'field-variant',
      event_type_id: 'et-1',
      name: 'variant',
      display_name: 'Variant',
      field_type: 'string',
      is_required: false,
      enum_options: null,
      order: 0,
    },
  ],
} as unknown as EventType

const JSON_TEMPLATE_EVENT_TYPE = {
  ...TEMPLATE_EVENT_TYPE,
  field_definitions: [
    ...TEMPLATE_EVENT_TYPE.field_definitions,
    {
      id: 'field-payload',
      event_type_id: 'et-1',
      name: 'payload',
      display_name: 'Payload',
      field_type: 'json',
      is_required: false,
      enum_options: null,
      order: 1,
    },
  ],
} as unknown as EventType

const JSON_PRODUCT_ID_EVENT_TYPE = {
  ...EVENT_TYPE,
  field_definitions: [
    {
      id: 'field-json-product-id',
      event_type_id: 'et-1',
      name: 'product_id',
      display_name: 'Product ID',
      field_type: 'json',
      is_required: false,
      enum_options: null,
      order: 0,
    },
  ],
} as unknown as EventType

const TEMPLATE_VARIABLE: Variable = {
  id: 'var-1',
  project_id: 'project-1',
  name: 'variant',
  source_name: 'legacy.variant',
  variable_type: 'string',
  description: 'Experiment variant',
  allowed_values: ['control', 'treatment', 'holdout', 'overflow'],
  bindings: ['payload.variant'],
}

const EDIT_EVENT_TYPE = {
  ...EVENT_TYPE,
  name: 'purchase',
  display_name: 'Purchase',
  field_definitions: [
    {
      id: 'field-product-id',
      event_type_id: 'et-1',
      name: 'product_id',
      display_name: 'Product ID',
      field_type: 'string',
      is_required: false,
      enum_options: null,
      order: 0,
    },
  ],
} as unknown as EventType

const EDIT_EVENT = {
  ...EXISTING_EVENT,
  name: 'Trial Started',
  field_values: [{ field_definition_id: 'field-product-id', value: '${product_id}' }],
} as unknown as TEvent

const PRODUCT_ID_VARIABLE: Variable = {
  id: 'var-product-id',
  project_id: 'project-1',
  name: 'product_id',
  source_name: 'product_id',
  variable_type: 'string',
  description: 'Store product / SKU identifier.',
  allowed_values: [],
  bindings: [],
}

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  // The form links out — to the existing event holding a claimed scan identity,
  // and to event-type creation on an empty project — so it needs a router.
  return createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(MemoryRouter, null, children),
  )
}

function renderForm(
  event: TEvent | null,
  {
    eventTypes = [EVENT_TYPE],
    metaFields = [] as MetaFieldDefinition[],
    projectVariables = [] as Variable[],
  }: {
    eventTypes?: EventType[]
    metaFields?: MetaFieldDefinition[]
    projectVariables?: Variable[]
  } = {},
) {
  return render(
    createElement(EventForm, {
      slug: 'demo',
      eventTypes,
      metaFields,
      projectVariables,
      event,
      onClose: () => {},
    }),
    { wrapper },
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // clearAllMocks keeps implementations, so a scan list one test set would
  // otherwise leak into the next; start each from the empty project.
  vi.mocked(scansApi.list).mockResolvedValue([])
  vi.mocked(eventsApi.byNames).mockResolvedValue({ items: [] })
  // Also the name-convention sample (AU-41): no events, no convention.
  vi.mocked(eventsApi.list).mockResolvedValue({ items: [], total: 0 } as never)
})

afterEach(() => {
  queryClient.clear()
  vi.clearAllMocks()
})

describe('EventForm event-type field', () => {
  it('disables the event-type select and shows the immutability helper when editing', () => {
    renderForm(EXISTING_EVENT)

    expect(screen.getByLabelText('Event type', { exact: false })).toBeDisabled()
    expect(screen.getByText("Can't be changed after creation.")).toBeInTheDocument()
  })

  it('keeps the event-type select editable with no helper when creating', () => {
    renderForm(null)

    expect(screen.getByLabelText('Event type', { exact: false })).toBeEnabled()
    expect(screen.queryByText("Can't be changed after creation.")).not.toBeInTheDocument()
  })
})

describe('EventForm name field', () => {
  it('suggests no invented convention when the type has no events to learn from (AU-41)', () => {
    renderForm(null)

    // "e.g. checkout:completed" sat next to catalogs named another way; with no
    // names of the type's own to show, the one rule that holds in every project
    // is that it must match what is sent.
    expect(screen.getByLabelText(/Name/)).toHaveAttribute('placeholder', 'The exact name the app sends')
  })

  it("offers one of the type's own names as the example, and points out a name in another style (AU-41)", async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({
      items: [
        { id: 'ev-a', name: 'Home Screen View' },
        { id: 'ev-b', name: 'Settings Screen View' },
        { id: 'ev-c', name: 'Spot Screen View' },
      ],
      total: 3,
    } as never)
    renderForm(null)

    await waitFor(() =>
      expect(screen.getByLabelText(/Name/)).toHaveAttribute('placeholder', 'e.g. Home Screen View'),
    )
    expect(eventsApi.list).toHaveBeenCalledWith(
      'demo',
      { event_type_id: 'et-1', limit: 20 },
      null,
      expect.anything(),
    )

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:completed' } })
    expect(screen.getByText(/events look like “Home Screen View”/)).toBeInTheDocument()
    // A pointer, never a block.
    expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled()

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Profile Screen View' } })
    expect(screen.queryByText(/events look like/)).not.toBeInTheDocument()
  })
})

describe('EventForm template authoring', () => {
  it('does not constrain an uncoached JSON field merely because it is named product_id', () => {
    renderForm(null, { eventTypes: [JSON_PRODUCT_ID_EVENT_TYPE] })

    expect(
      screen.getByLabelText('Product ID').closest('[class~="max-w-[320px]"]'),
    ).toBeNull()
  })

  it('shows rich ${ suggestions, inline unknown-token warnings, and copyable documented values', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    renderForm(null, {
      eventTypes: [TEMPLATE_EVENT_TYPE],
      projectVariables: [TEMPLATE_VARIABLE],
    })

    const input = screen.getByLabelText('Variant')
    fireEvent.change(input, { target: { value: '${' } })

    const selectedSuggestion = screen.getByRole('option', { name: /\$\{variant\}/ })
    expect(selectedSuggestion).toBeInTheDocument()
    // The highlight is the option's selected state, on the neutral hover
    // surface (DS-10), not a brand-coloured text class.
    expect(selectedSuggestion).toHaveAttribute('aria-selected', 'true')
    expect(within(selectedSuggestion).getByText('${variant}')).toBeInTheDocument()
    const description = within(selectedSuggestion).getByText('Experiment variant')
    expect(description.parentElement).toHaveClass('min-w-0', 'flex-1', 'overflow-hidden')
    expect(description).toHaveClass('w-full', 'truncate')
    expect(within(selectedSuggestion).getByText('payload.variant')).toHaveClass(
      'w-full',
      'truncate',
    )
    expect(within(selectedSuggestion).getByText('control · treatment · holdout')).toHaveClass(
      'w-full',
      'truncate',
    )

    fireEvent.change(input, { target: { value: '${missing}' } })
    expect(screen.getByText('Unknown property token: ${missing}')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '${variant}' } })
    const copyChip = screen.getByRole('button', { name: 'Copy documented value control' })
    fireEvent.click(copyChip)
    expect(writeText).toHaveBeenCalledWith('control')
    // Reported through a toast, like the spec card's copy buttons (EVT-49).
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Copied control'))
    expect(input).toHaveValue('${variant}')
  })

  it('says so when the clipboard refuses, instead of claiming a copy (EVT-49)', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    })
    renderForm(null, {
      eventTypes: [TEMPLATE_EVENT_TYPE],
      projectVariables: [TEMPLATE_VARIABLE],
    })
    fireEvent.change(screen.getByLabelText('Variant'), { target: { value: '${variant}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Copy documented value control' }))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Could not copy control'))
    expect(toast.success).not.toHaveBeenCalled()
    expect(screen.queryByText(/Copied control/)).toBeNull()
  })
})

// The naming rule arrives ON the type, resolved by the server (through the
// main counterpart for a branch copy). Reading it off the scan list by
// event_type_id is what broke on branches (tripl-kjhi.1).
const RULED_TYPE = { ...TEMPLATE_EVENT_TYPE, event_name_format: 'pv:{variant}' } as EventType
const RULED_JSON_TYPE = {
  ...JSON_TEMPLATE_EVENT_TYPE,
  event_name_format: 'pv:{variant}',
} as EventType

describe('EventForm scan-rule generated names', () => {
  it('locks the name input and previews the template-generated name', async () => {
    renderForm(null, {
      eventTypes: [RULED_TYPE],
      projectVariables: [],
    })

    const nameInput = (await screen.findByLabelText(/Name/)) as HTMLInputElement
    await screen.findByText(/generated by scan rule/)
    // readOnly, not disabled: the generated name has to stay selectable and
    // copyable, and a disabled input is skipped by constraint validation so its
    // `required` mark would promise a check nothing runs (tripl-u2h9.5).
    expect(nameInput).toHaveAttribute('readonly')
    expect(nameInput).not.toBeDisabled()
    // Unresolved template key is called out and blocks submit. It names the row
    // on screen ("Variant"), not the raw warehouse column ("variant") the
    // reader would have had to map it onto (tripl-u2h9.4).
    expect(screen.getByText(/Fill field values for: Variant/)).toBeInTheDocument()
    expect(nameInput.value).toBe('pv:{variant}')

    // "Variant*", not "Variant": a column the name is built from is required in
    // practice, so the row carries the mark and the control carries the
    // attribute (tripl-u2h9.4).
    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })
    expect(nameInput.value).toBe('pv:b2')
    expect(screen.queryByText(/Fill field values for/)).not.toBeInTheDocument()
  })

  it('takes the rule from the type, not from a scan config naming another id', async () => {
    // The shape of a plan branch: the scan config is bound to the main-branch
    // type id, and the branch copy the form is editing has a new one. Filtering
    // the scan list by id found nothing here and offered free text where the
    // rule governed (tripl-kjhi.1).
    vi.mocked(scansApi.list).mockResolvedValue([
      {
        id: 'scan-main',
        event_type_id: 'et-main',
        event_name_format: 'scan:{variant}',
        updated_at: '2026-01-03T00:00:00Z',
      } as never,
    ])

    renderForm(null, { eventTypes: [RULED_TYPE] })

    await screen.findByText(/generated by scan rule: pv:\{variant\}/)
    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })
    expect(screen.getByLabelText(/Name/)).toHaveValue('pv:b2')
    expect(screen.getByLabelText(/Name/)).toHaveAttribute('readonly')
  })

  it('offers free text when the type carries no rule, whatever the scan list says', async () => {
    vi.mocked(scansApi.list).mockResolvedValue([
      {
        id: 'scan-1',
        event_type_id: 'et-1',
        event_name_format: 'pv:{variant}',
        updated_at: '2026-01-03T00:00:00Z',
      } as never,
    ])

    renderForm(null, { eventTypes: [TEMPLATE_EVENT_TYPE] })

    // The breakdown picker reads the scan list, so wait for it to have landed
    // before concluding the name box stayed free.
    await screen.findByLabelText(/Metric breakdowns/)
    expect(screen.getByLabelText(/Name/)).not.toHaveAttribute('readonly')
    expect(screen.queryByText(/generated by scan rule/)).not.toBeInTheDocument()
  })

  it('points the author at Title for their own wording', async () => {
    renderForm(null, { eventTypes: [RULED_TYPE] })

    await screen.findByText(/generated by scan rule/)
    expect(screen.getByText('Your own wording goes in Title.')).toBeInTheDocument()
  })
})

describe('EventForm — authoring an event the scan will recognise', () => {
  const RULED_SCANS = [
    {
      id: 'scan-1',
      event_type_id: 'et-1',
      event_name_format: 'pv:{variant}',
      metric_breakdown_columns: ['country'],
      platform_column: 'platform',
      app_version_column: null,
      updated_at: '2026-01-01T00:00:00Z',
    } as never,
  ]

  // The same scan with a version column declared. Kept apart from RULED_SCANS
  // because `app_version_column: null` there is itself the case under test for
  // every other breakdown assertion in this block: nothing to exclude.
  const VERSIONED_SCANS = [
    {
      id: 'scan-1',
      event_type_id: 'et-1',
      event_name_format: 'pv:{variant}',
      metric_breakdown_columns: ['country'],
      platform_column: 'platform',
      app_version_column: 'app_version',
      updated_at: '2026-01-01T00:00:00Z',
    } as never,
  ]

  it('marks the rows the name is built from, and says so on the card', async () => {
    renderForm(null, { eventTypes: [RULED_JSON_TYPE] })

    await screen.findByText(/generated by scan rule/)
    // The naming column is called out; the one that plays no part in the name
    // is not. Before this the reader had six unmarked rows and a warning naming
    // raw warehouse columns (tripl-u2h9.4).
    expect(screen.getByText(/The event name is built from variant/)).toBeInTheDocument()
    expect(screen.getAllByText('names the event')).toHaveLength(1)
    expect(screen.getByLabelText(/^Variant/)).toBeRequired()
    expect(screen.getByLabelText('Payload')).not.toBeRequired()
  })

  it('warns that a ${variable} in a naming field is stored literally', async () => {
    renderForm(null, { eventTypes: [RULED_JSON_TYPE] })

    await screen.findByText(/generated by scan rule/)
    // The form validates the token and offers documented values, which reads as
    // a promise the scanner will expand it. It substitutes {column} only, so the
    // event would be stamped with an identity matching nothing.
    fireEvent.change(screen.getByLabelText(/^Variant/), {
      target: { value: 'profile_click_${variant}' },
    })

    expect(await screen.findByText(/stored literally/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Group them with a scan event rule/i })).toHaveAttribute(
      'href',
      '/p/demo/settings/scans',
    )

    // The field that plays no part in the name gets no such warning.
    fireEvent.change(screen.getByLabelText('Payload'), {
      target: { value: '{"a": "${variant}"}' },
    })
    expect(screen.getAllByText(/stored literally/)).toHaveLength(1)
  })

  it('offers no name example while the rule writes the box', async () => {
    renderForm(null, { eventTypes: [RULED_TYPE] })

    await screen.findByText(/generated by scan rule/)
    // "e.g. checkout:completed" advertises free text on a control that has none
    // (tripl-u2h9.9).
    expect(screen.getByLabelText(/Name/)).not.toHaveAttribute('placeholder')
  })

  it('says a name typed before the type was chosen is not used', async () => {
    // Two types, so none is preselected and the Name box starts editable —
    // exactly the order in which a user meets this: type a name, then choose.
    const OTHER_TYPE = { ...EVENT_TYPE, id: 'et-2', display_name: 'Other' } as EventType
    renderForm(null, { eventTypes: [OTHER_TYPE, RULED_TYPE] })

    const nameInput = screen.getByLabelText(/Name/)
    expect(nameInput).not.toHaveAttribute('readonly')
    fireEvent.change(nameInput, { target: { value: 'my crooked name' } })
    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-1' } })

    // The typed name survives in state and would return on a type with no rule,
    // so it must be accounted for rather than left to vanish (tripl-u2h9.7).
    expect(await screen.findByText(/“my crooked name” is not used/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Name/)).toHaveValue('pv:{variant}')
  })

  it('refuses a name an event already answers to, by identity and not by display name', async () => {
    vi.mocked(eventsApi.byNames).mockResolvedValue({
      // The clash is on source_name while the display name has been renamed
      // away from it — the case matching on `name` alone would miss, and the
      // reason source_name exists at all.
      items: [{ identity: 'pv:b2', event_id: 'ev-existing', name: 'Variant B2 pageview', source_name: 'pv:b2' }],
    })
    renderForm(null, { eventTypes: [RULED_TYPE] })

    await screen.findByText(/generated by scan rule/)
    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })

    // The probe is debounced by design before it asks the server, so this waits
    // past the default 1000ms budget.
    const warning = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(warning).toHaveTextContent(/already answers to this name/)
    expect(within(warning).getByRole('link', { name: /open it instead/i })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-existing',
    )
    expect(screen.getByRole('button', { name: /Create event/i })).toBeDisabled()
  })

  it('refuses a name a row with no identity yet will adopt', async () => {
    vi.mocked(eventsApi.byNames).mockResolvedValue({
      // Authored before any rule governed this type: the next scan adopts its
      // name as the identity, so the name is not free. Same second arm the
      // server tests in `_event_holding_scan_identity`.
      items: [{ identity: 'pv:b2', event_id: 'ev-early', name: 'pv:b2', source_name: null }],
    })
    renderForm(null, { eventTypes: [RULED_TYPE] })

    await screen.findByText(/generated by scan rule/)
    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })

    const warning = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(within(warning).getByRole('link', { name: /open it instead/i })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-early',
    )
  })

  it('asks the exact-name lookup, and a free name stays free (EVT-37)', async () => {
    renderForm(null, { eventTypes: [RULED_TYPE] })

    await screen.findByText(/generated by scan rule/)
    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })

    // One exact identity, not a substring search whose page could leave the
    // matching row out.
    await waitFor(
      () =>
        expect(eventsApi.byNames).toHaveBeenCalledWith('demo', 'et-1', ['pv:b2'], null, expect.anything()),
      { timeout: 3000 },
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled(),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('offers the type schema and the scans, not four hardcoded columns', async () => {
    // The scan list is still what says which columns the project collects —
    // only the naming rule stopped being read off it (tripl-kjhi.1).
    vi.mocked(scansApi.list).mockResolvedValue(RULED_SCANS)
    renderForm(null, { eventTypes: [RULED_JSON_TYPE] })

    await screen.findByText(/generated by scan rule/)
    // What the docs describe and the redesign dropped: the type's scalar fields,
    // JSON excluded, plus the columns the scans actually collect. 'device_model'
    // was one of the four literals and belongs to neither (tripl-u2h9.6).
    expect(screen.getByRole('button', { name: 'variant' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'payload' })).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'country' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'platform' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'device_model' })).not.toBeInTheDocument()
  })

  it('offers the platform column but not the app-version column', async () => {
    // Both are collected scan-wide and neither has a field definition, so the
    // offered set is the only place either can come from. The version column is
    // left out all the same: it already has its own series (Breakdowns → App
    // version), so an event listing it made the collector write the same row
    // twice, and the API now refuses it (tripl-0zpq.15).
    vi.mocked(scansApi.list).mockResolvedValue(VERSIONED_SCANS)
    renderForm(null, { eventTypes: [RULED_JSON_TYPE] })

    await screen.findByText(/generated by scan rule/)
    // 'country' comes from the same scan record, so waiting on it proves the
    // scan list has landed and the absence below is not just a pending query.
    expect(await screen.findByRole('button', { name: 'country' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'platform' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'app_version' })).not.toBeInTheDocument()
  })

  it('keeps a version column the event already stores, so it can be switched off', async () => {
    // The exclusion above is about what the list OFFERS. A value written back
    // when it still did must stay on screen and pressed, or the only way off it
    // would be to retype it into the manual box.
    vi.mocked(scansApi.list).mockResolvedValue(VERSIONED_SCANS)
    const legacy = {
      ...EXISTING_EVENT,
      metric_breakdown_columns: ['app_version'],
    } as unknown as TEvent
    renderForm(legacy, { eventTypes: [RULED_JSON_TYPE] })

    expect(await screen.findByRole('button', { name: 'country' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'app_version', pressed: true })).toBeInTheDocument()
  })

  it('still takes a column no schema or scan knows about', async () => {
    renderForm(null, { eventTypes: [TEMPLATE_EVENT_TYPE] })

    const input = await screen.findByLabelText(/Metric breakdowns/)
    fireEvent.change(input, { target: { value: 'device_model' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(screen.getByRole('button', { name: 'device_model' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('sends a project with no event types somewhere instead of nowhere', async () => {
    renderForm(null, { eventTypes: [] })

    // The select offered "Select type…" and nothing else, on the first screen a
    // new project reaches (tripl-u2h9.3).
    expect(await screen.findByText(/This project has no event types yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Create an event type/i })).toHaveAttribute(
      'href',
      '/p/demo/event-types',
    )
    expect(screen.queryByRole('combobox', { name: /Event type/ })).not.toBeInTheDocument()
  })
})

describe('EventForm save and add another', () => {
  it('creates the event without closing or clearing entered values', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    vi.mocked(scansApi.list).mockResolvedValue([])
    const onClose = vi.fn()
    render(
      createElement(EventForm, {
        slug: 'demo',
        eventTypes: [JSON_TEMPLATE_EVENT_TYPE],
        metaFields: [],
        projectVariables: [],
        event: null,
        onClose,
      }),
      { wrapper },
    )

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Starts checkout.' } })
    fireEvent.change(screen.getByLabelText('Variant'), { target: { value: 'b2' } })
    // The raw JSON view: the property grid opens first (F23).
    fireEvent.click(screen.getByRole('button', { name: 'Edit JSON' }))
    fireEvent.change(screen.getByLabelText('Payload'), { target: { value: '{"source":"cta"}' } })
    fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'critical' } })
    fireEvent.keyDown(screen.getByLabelText('Tags'), { key: 'Enter' })

    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))

    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({
          name: 'checkout:started',
          description: 'Starts checkout.',
          tags: ['critical'],
          field_values: [
            { field_definition_id: 'field-variant', value: 'b2' },
            { field_definition_id: 'field-payload', value: '{"source":"cta"}' },
          ],
        }),
        null,
      ),
    )
    expect(onClose).not.toHaveBeenCalled()
    // Retention is the point of this button, and documented as such in
    // website/docs/use/feature-reference.md — a run of similar events is
    // authored by changing one field between saves. Do not "fix" it by
    // resetting the form.
    expect(screen.getByLabelText(/Name/)).toHaveValue('checkout:started')
    expect(screen.getByLabelText('Description')).toHaveValue('Starts checkout.')
    expect(screen.getByLabelText('Variant')).toHaveValue('b2')
    expect(screen.getByLabelText('Payload')).toHaveValue('{"source":"cta"}')
    expect(screen.getByText('critical')).toBeInTheDocument()
  })

  it('says what it created, and stops saying it once the form describes another event', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({ name: 'checkout:started' } as never)
    vi.mocked(scansApi.list).mockResolvedValue([])
    render(
      createElement(EventForm, {
        slug: 'demo',
        eventTypes: [JSON_TEMPLATE_EVENT_TYPE],
        metaFields: [],
        projectVariables: [],
        event: null,
        onClose: vi.fn(),
      }),
      { wrapper },
    )

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))

    // Without this the form looked untouched after a save, which is why a
    // second press read as the obvious next action (tripl-u2h9.2).
    // The line sits in the sticky save bar's status region (AU-6).
    const created = await screen.findByText(/Created checkout:started/)
    expect(created.closest('[role="status"]')).not.toBeNull()

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:completed' } })
    expect(screen.queryByText(/Created checkout:started/)).not.toBeInTheDocument()
  })
})

describe('EventForm title (tripl-kjhi.3)', () => {
  it('sends the title, trimmed, beside the name it is never part of', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null)

    const titleInput = screen.getByLabelText('Title')
    expect(titleInput).toHaveAttribute('maxlength', '500')
    expect(titleInput).not.toBeRequired()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'model:card:tap' } })
    fireEvent.change(titleInput, { target: { value: '  Tap on a model card  ' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))

    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ name: 'model:card:tap', title: 'Tap on a model card' }),
        null,
      ),
    )
  })

  it('prefills the stored title when editing, and sends the change', async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    renderForm({ ...EXISTING_EVENT, title: 'Checkout done' } as TEvent)

    expect(screen.getByLabelText('Title')).toHaveValue('Checkout done')
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Checkout completed' } })
    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))

    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({ name: 'checkout:completed', title: 'Checkout completed' }),
        null,
      ),
    )
  })
})

describe('EventForm meta field link template (tripl-kjhi.5)', () => {
  const JIRA_FIELD: MetaFieldDefinition = {
    id: 'mf-jira',
    project_id: 'project-1',
    name: 'jira',
    display_name: 'Jira',
    field_type: 'string',
    is_required: false,
    enum_options: null,
    default_value: null,
    link_template: 'https://jira.example/browse/${value}',
    order: 0,
    sensitivity: 'none',
  }

  it('asks for the key with the link it will open, in the reader\'s own template', () => {
    renderForm(null, { metaFields: [JIRA_FIELD] })

    // "Uses link template with ${value}" named a mechanism; production held
    // whole addresses where keys were meant (tripl-kjhi.5).
    expect(screen.getByText(/Enter the key/)).toHaveTextContent(
      'Enter the key, e.g. PROJ-1234 — opens https://jira.example/browse/PROJ-1234',
    )
  })

  it('says nothing about a template with no ${value} to fill (AU-9)', () => {
    // No `$`, so no link ever resolves; "Uses link template with ${value}."
    // named a mechanism that was not even working.
    renderForm(null, { metaFields: [{ ...JIRA_FIELD, link_template: 'https://jira.example/{value}' }] })

    expect(screen.getByLabelText('Jira')).toBeInTheDocument()
    expect(screen.queryByText(/link template/i)).toBeNull()
    expect(screen.queryByText(/Enter the key/)).toBeNull()
  })

  it('keeps only the key when the whole address is pasted', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null, { metaFields: [JIRA_FIELD] })

    const jira = screen.getByLabelText('Jira')
    fireEvent.change(jira, { target: { value: 'https://jira.example/browse/PROJ-4770' } })
    expect(jira).toHaveValue('PROJ-4770')

    // A bare key, or an address from elsewhere, is stored as typed.
    fireEvent.change(jira, { target: { value: 'https://other.example/PROJ-1' } })
    expect(jira).toHaveValue('https://other.example/PROJ-1')
    fireEvent.change(jira, { target: { value: 'PROJ-4770' } })

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({
          meta_values: [{ meta_field_definition_id: 'mf-jira', value: 'PROJ-4770' }],
        }),
        null,
      ),
    )
  })
})

describe('EventForm — coached demo scenario (tripl-odrj.4)', () => {
  const SLUG = 'demo'

  const demoProject = {
    id: 'p-1',
    name: 'Demo',
    slug: SLUG,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
  } as unknown as Project

  function renderCoachedEditEvent(event: TEvent = EDIT_EVENT) {
    return render(
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(
          MemoryRouter,
          { initialEntries: [`/p/${SLUG}/events/purchase/ev-1/edit`] },
          createElement(
            DemoScenarioProvider,
            { project: demoProject, pollIntervalMs: 10_000, children: null },
            createElement(EventForm, {
              slug: SLUG,
              eventTypes: [EDIT_EVENT_TYPE],
              metaFields: [],
              projectVariables: [PRODUCT_ID_VARIABLE],
              event,
              onClose: () => {},
            }),
          ),
        ),
      ),
    )
  }

  afterEach(() => {
    window.localStorage.clear()
  })

  it('coaches a documented Product ID and then restores the seeded property token', async () => {
    writeScenarioState(SLUG, chapterState('edit-event', 'edit-event/set-value'))

    renderCoachedEditEvent()

    const productId = screen.getByLabelText('Product ID')
    expect(productId).toHaveAttribute('role', 'combobox')
    expect(productId).toHaveValue('${product_id}')
    const coachTarget = productId.closest('[data-coach-target="edit-event/set-value"]')
    expect(coachTarget).not.toBeNull()
    expect(coachTarget).toHaveClass('max-w-[320px]')
    expect(coachTarget).toContainElement(productId)
    expect(
      screen.getByText(
        'Replace the current Product ID value with prod_monthly. The guide advances automatically — do not save yet.',
      ),
    ).toBeInTheDocument()
    fireEvent.change(productId, { target: { value: 'prod_monthly' } })

    await waitFor(() =>
      expect(readScenarioState(SLUG).chapters['edit-event']?.step).toBe('edit-event/set-token'),
    )
    expect(
      screen.getByText(
        'Replace prod_monthly: type $ in Product ID, choose ${product_id}, then follow the guide to Save.',
      ),
    ).toBeInTheDocument()

    fireEvent.change(productId, { target: { value: '$' } })
    fireEvent.mouseDown(screen.getByRole('option', { name: /\$\{product_id\}/ }))

    await waitFor(() => {
      expect(readScenarioState(SLUG).chapters['edit-event']?.step).toBe('edit-event/save')
      expect(screen.getByLabelText('Product ID')).toHaveValue('${product_id}')
    })
    expect(screen.getByText('Save the event — the tracking plan updates immediately.')).toBeInTheDocument()
  })

  it('catches up when the rendered Product ID already satisfies the active step', async () => {
    writeScenarioState(SLUG, chapterState('edit-event', 'edit-event/set-value'))
    renderCoachedEditEvent({
      ...EDIT_EVENT,
      field_values: [{ field_definition_id: 'field-product-id', value: 'prod_monthly' }],
    } as TEvent)

    await waitFor(() =>
      expect(readScenarioState(SLUG).chapters['edit-event']?.step).toBe('edit-event/set-token'),
    )
    expect(screen.getByText(/type \$ in Product ID/)).toBeInTheDocument()
  })

  it("completes the edit-event chapter's save step through the real save mutation", async () => {
    writeScenarioState(SLUG, chapterState('edit-event', 'edit-event/save'))
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)

    render(
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(
          MemoryRouter,
          { initialEntries: [`/p/${SLUG}/events/all/ev-1/edit`] },
          createElement(
            DemoScenarioProvider,
            { project: demoProject, pollIntervalMs: 10_000, children: null },
            createElement(EventForm, {
              slug: SLUG,
              eventTypes: [EVENT_TYPE],
              metaFields: [],
              projectVariables: [],
              event: EXISTING_EVENT,
              onClose: () => {},
            }),
          ),
        ),
      ),
    )

    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))

    await waitFor(() =>
      expect(readScenarioState(SLUG).chapters['edit-event']?.status).toBe('completed'),
    )
  })
})

describe('EventForm owner (tripl-kjhi.16)', () => {
  it('offers the roster next to Status and sends the chosen owner', async () => {
    vi.mocked(usersApi.list).mockResolvedValue([
      { id: 'u-maya', email: 'maya@example.com', name: 'Maya R.', role: 'editor', created_at: '' },
      { id: 'u-priya', email: 'priya@example.com', name: null, role: 'editor', created_at: '' },
    ] as never)
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null)

    const owner = screen.getByLabelText('Owner', { exact: false })
    expect(await screen.findByRole('option', { name: 'Maya R.' })).toBeInTheDocument()
    // No name falls back to the email, as the roster does everywhere else.
    expect(screen.getByRole('option', { name: 'priya@example.com' })).toBeInTheDocument()
    fireEvent.change(owner, { target: { value: 'u-priya' } })

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ owner_id: 'u-priya' }),
        null,
      ),
    )
  })

  it('sends no owner when none is chosen', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null)

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ owner_id: null }),
        null,
      ),
    )
  })
})

describe('EventForm ticket prefill from the branch name (tripl-kjhi.14)', () => {
  const JIRA_FIELD: MetaFieldDefinition = {
    id: 'mf-jira',
    project_id: 'project-1',
    name: 'jira',
    display_name: 'Jira',
    field_type: 'string',
    is_required: false,
    enum_options: null,
    default_value: null,
    link_template: 'https://jira.example/browse/${value}',
    order: 0,
    sensitivity: 'none',
  }

  function renderInBranch(
    event: TEvent | null,
    branchName: string,
    { deferred = false }: { deferred?: boolean } = {},
  ) {
    const list = { items: [{ id: 'b-proj', name: branchName, kind: 'working' }], total: 1 } as never
    let release = () => {}
    if (deferred) {
      vi.mocked(planBranchesApi.list).mockReturnValue(
        new Promise(resolve => {
          release = () => resolve(list)
        }),
      )
    } else {
      vi.mocked(planBranchesApi.list).mockResolvedValue(list)
    }
    render(
      createElement(EventForm, {
        slug: 'demo',
        eventTypes: [EVENT_TYPE],
        metaFields: [JIRA_FIELD],
        projectVariables: [],
        event,
        onClose: () => {},
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(
            QueryClientProvider,
            { client: queryClient },
            createElement(
              BranchContext.Provider,
              { value: { branchId: 'b-proj', setBranchId: () => {}, slug: 'demo' } },
              createElement(MemoryRouter, null, children),
            ),
          ),
      },
    )
    return { release }
  }

  it('fills the linking meta field with the key the branch is named after', async () => {
    renderInBranch(null, 'PROJ-4770')
    await waitFor(() => expect(screen.getByLabelText('Jira')).toHaveValue('PROJ-4770'))
  })

  it('leaves a branch not named after a ticket, and an existing event, alone', async () => {
    renderInBranch(null, 'checkout-v2')
    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    expect(screen.getByLabelText('Jira')).toHaveValue('')
  })

  it('leaves a field the reader cleared before the branch list arrived alone', async () => {
    const { release } = renderInBranch(null, 'PROJ-4770', { deferred: true })
    const jira = screen.getByLabelText('Jira')
    fireEvent.change(jira, { target: { value: 'PROJ-1' } })
    fireEvent.change(jira, { target: { value: '' } })

    release()
    // The branch has arrived and the prefill effect has had its turn.
    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 50))
    })
    expect(jira).toHaveValue('')
  })

  it('never overwrites what an existing event already holds', async () => {
    const existing = {
      ...EXISTING_EVENT,
      meta_values: [{ meta_field_definition_id: 'mf-jira', value: 'PROJ-1' }],
    } as unknown as TEvent
    renderInBranch(existing, 'PROJ-4770')
    expect(screen.getByLabelText('Jira')).toHaveValue('PROJ-1')
    expect(planBranchesApi.list).not.toHaveBeenCalled()
  })
})

describe('EventForm scan maintenance notice', () => {
  const scanned = (value: string) =>
    ({
      ...EDIT_EVENT,
      field_values: [{ field_definition_id: 'field-product-id', value, is_authored: false }],
    }) as unknown as TEvent
  const authored = (value: string) =>
    ({
      ...EDIT_EVENT,
      field_values: [{ field_definition_id: 'field-product-id', value, is_authored: true }],
    }) as unknown as TEvent

  it('says nothing about a scan-maintained value until the reader changes it', () => {
    renderForm(scanned('prod_monthly'), { eventTypes: [EDIT_EVENT_TYPE] })
    // Scoped to the field's own notices (tied to it by aria-describedby): the
    // card subtitle now says, of every field, that "scans match the event on
    // these" (AU-22), which is not a notice about this value.
    const input = screen.getByLabelText(/Product ID/)
    expect(input).not.toHaveAccessibleDescription(/scan/i)
    expect(screen.queryByText(/stops scans|scans leave it alone|next scan fills/i)).not.toBeInTheDocument()

    fireEvent.change(input, { target: { value: 'prod_annual' } })
    expect(screen.getByText('Saving this stops scans from updating the field.')).toBeInTheDocument()
    expect(input).toHaveAccessibleDescription(/Saving this stops scans from updating the field\./)
  })

  it('marks a hand-edited value as frozen and hands it back on request', () => {
    renderForm(authored('prod_monthly'), { eventTypes: [EDIT_EVENT_TYPE] })
    expect(screen.getByText(/Edited by hand, so scans leave it alone/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Hand back to scans' }))
    expect(screen.getByLabelText(/Product ID/)).toHaveValue('')
    expect(
      screen.getByText('Cleared. Save, and the next scan fills this in again.'),
    ).toBeInTheDocument()
  })

  it('does not offer to clear a required field, which the server would reject', () => {
    const requiredType = {
      ...EDIT_EVENT_TYPE,
      field_definitions: [{ ...EDIT_EVENT_TYPE.field_definitions[0], is_required: true }],
    } as unknown as EventType
    renderForm(authored('prod_monthly'), { eventTypes: [requiredType] })

    expect(screen.getByText(/Edited by hand, so scans leave it alone/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Hand back to scans' })).not.toBeInTheDocument()
  })

  it('treats a response with no flag as still maintained, not as frozen', () => {
    const legacy = {
      ...EDIT_EVENT,
      field_values: [{ field_definition_id: 'field-product-id', value: 'prod_monthly' }],
    } as unknown as TEvent
    renderForm(legacy, { eventTypes: [EDIT_EVENT_TYPE] })
    expect(screen.queryByText(/Edited by hand/)).not.toBeInTheDocument()
  })
})

describe('EventForm field breakdown link', () => {
  it('links a field the event already splits by to the Breakdowns tab', () => {
    const splitting = {
      ...EDIT_EVENT,
      metric_breakdown_columns: ['product_id'],
    } as unknown as TEvent
    renderForm(splitting, { eventTypes: [EDIT_EVENT_TYPE] })

    expect(screen.getByRole('link', { name: 'See every value this field takes' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-1?tab=breakdowns&column=product_id',
    )
  })

  it('takes the reader to the breakdown toggle instead of toggling from afar (AU-23)', () => {
    renderForm(EDIT_EVENT, { eventTypes: [EDIT_EVENT_TYPE] })

    fireEvent.click(screen.getByRole('button', { name: 'Split volume by this field' }))

    // The one switch is the chip in the Metric breakdowns row: the line under
    // the field brings it into view and leaves it as it was.
    const chip = screen.getByRole('button', { name: 'product_id', pressed: false })
    expect(chip).toHaveFocus()
  })

  it('says a field is split once its chip is on, and that the data is not there yet', () => {
    renderForm(EDIT_EVENT, { eventTypes: [EDIT_EVENT_TYPE] })

    fireEvent.click(screen.getByRole('button', { name: 'product_id', pressed: false }))

    // Not a link: the column was added in this session, so there are no
    // collected rows behind it and the tab would open on an empty chart.
    expect(screen.queryByRole('link', { name: /every value/ })).not.toBeInTheDocument()
    expect(screen.getByText(/collection starts splitting by/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Split by this field' }))
    // A status that points at the toggle, not a second switch.
    expect(screen.getByRole('button', { name: 'product_id', pressed: true })).toHaveFocus()
  })

  it('offers nothing on an event that does not exist yet', () => {
    renderForm(null, { eventTypes: [EDIT_EVENT_TYPE] })
    expect(
      screen.queryByRole('button', { name: 'Split volume by this field' }),
    ).not.toBeInTheDocument()
  })

  it('offers nothing for a JSON field, which is no warehouse column', () => {
    const jsonType = {
      ...EDIT_EVENT_TYPE,
      field_definitions: [{ ...EDIT_EVENT_TYPE.field_definitions[0], field_type: 'json' }],
    } as unknown as EventType
    renderForm(EDIT_EVENT, { eventTypes: [jsonType] })
    expect(
      screen.queryByRole('button', { name: 'Split volume by this field' }),
    ).not.toBeInTheDocument()
  })
})

describe('EventForm multi-value meta field (tripl-h2sx.31)', () => {
  const KEYS_FIELD: MetaFieldDefinition = {
    id: 'mf-keys',
    project_id: 'project-1',
    name: 'jira_keys',
    display_name: 'Jira keys',
    field_type: 'string',
    is_required: false,
    allow_multiple: true,
    enum_options: null,
    default_value: null,
    link_template: 'https://jira.example/browse/${value}',
    order: 0,
    sensitivity: 'none',
  }

  it('sends every key entered, each as its own value', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null, { metaFields: [KEYS_FIELD] })

    const input = screen.getByLabelText('Add Jira keys')
    fireEvent.change(input, { target: { value: 'PROJ-1' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.change(input, { target: { value: 'PROJ-2' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({
          meta_values: [
            { meta_field_definition_id: 'mf-keys', value: 'PROJ-1' },
            { meta_field_definition_id: 'mf-keys', value: 'PROJ-2' },
          ],
        }),
        null,
      ),
    )
  })

  it('keeps only the key when a whole address is pasted into a chip', () => {
    renderForm(null, { metaFields: [KEYS_FIELD] })

    const input = screen.getByLabelText('Add Jira keys')
    fireEvent.change(input, { target: { value: 'https://jira.example/browse/PROJ-4770' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(screen.getByRole('button', { name: 'Remove PROJ-4770' })).toBeInTheDocument()
  })

  it('prefills every stored value when editing', () => {
    renderForm({
      ...EXISTING_EVENT,
      meta_values: [
        { id: 'mv-1', meta_field_definition_id: 'mf-keys', value: 'PROJ-1' },
        { id: 'mv-2', meta_field_definition_id: 'mf-keys', value: 'PROJ-2' },
      ],
    } as unknown as TEvent, { metaFields: [KEYS_FIELD] })

    expect(screen.getByRole('button', { name: 'Remove PROJ-1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove PROJ-2' })).toBeInTheDocument()
  })

  it('submits only the value it shows once the field is single-valued again', async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    // The admin turned Allow multiple off; the two stored values stayed. The
    // form renders one input, so saving must not send a second value the
    // server would refuse — the event would otherwise be unsaveable.
    renderForm({
      ...EXISTING_EVENT,
      meta_values: [
        { id: 'mv-1', meta_field_definition_id: 'mf-keys', value: 'PROJ-1' },
        { id: 'mv-2', meta_field_definition_id: 'mf-keys', value: 'PROJ-2' },
      ],
    } as unknown as TEvent, { metaFields: [{ ...KEYS_FIELD, allow_multiple: false }] })

    expect(screen.getByLabelText('Jira keys')).toHaveValue('PROJ-1')
    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({
          meta_values: [{ meta_field_definition_id: 'mf-keys', value: 'PROJ-1' }],
        }),
        null,
      ),
    )
  })
})

describe('EventForm JSON validity gate (tripl-h2sx.10)', () => {
  it('refuses Save while a JSON field is malformed, and names the row', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null, { eventTypes: [JSON_TEMPLATE_EVENT_TYPE] })

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    // The raw JSON view: the property grid opens first (F23).
    fireEvent.click(screen.getByRole('button', { name: 'Edit JSON' }))
    fireEvent.change(screen.getByLabelText('Payload'), { target: { value: '{"a":' } })

    expect(screen.getByText('Fix the JSON in: Payload')).toBeInTheDocument()
    const save = screen.getByRole('button', { name: /Create event/i })
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(eventsApi.create).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Payload'), { target: { value: '{"a":1}' } })
    expect(screen.queryByText(/Fix the JSON in/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled()
  })

  it('blocks a stored value nobody has touched', () => {
    // Seven backend paths write a field value without normalizing it, so an
    // event can arrive holding JSON the server would refuse. A gate reading
    // JsonEditor's own error state would pass this: the editor only validates
    // what is TYPED.
    renderForm(
      {
        ...EXISTING_EVENT,
        event_type_id: 'et-1',
        field_values: [{ field_definition_id: 'field-payload', value: '{oops' }],
      } as unknown as TEvent,
      { eventTypes: [JSON_TEMPLATE_EVENT_TYPE] },
    )

    expect(screen.getByText('Fix the JSON in: Payload')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save event/i })).toBeDisabled()
  })

  it('lets a template through — the client must never be stricter than the server', () => {
    renderForm(null, { eventTypes: [JSON_TEMPLATE_EVENT_TYPE] })

    fireEvent.change(screen.getByLabelText('Payload'), {
      target: { value: '{"screen": "${screen}"}' },
    })

    expect(screen.queryByText(/Fix the JSON in/)).not.toBeInTheDocument()
  })
})


describe('EventForm successor', () => {
  const DEPRECATED = {
    ...EXISTING_EVENT,
    status: 'deprecated',
  } as unknown as TEvent

  it('offers replacements from the catalog and sends the one picked', async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    vi.mocked(eventsApi.list).mockResolvedValue({
      items: [
        { id: 'ev-1', name: 'checkout:completed' },
        { id: 'ev-2', name: 'checkout:done' },
      ],
      total: 2,
    } as never)
    // Picking a successor reads it by id; answer with the event picked.
    vi.mocked(eventsApi.get).mockResolvedValue({ id: 'ev-2', name: 'checkout:done' } as never)
    renderForm(DEPRECATED)

    // Wait for the OPTION, not the field: the select renders the moment the
    // status is deprecated, and changing it before the roster lands would set a
    // value no option carries — a silent no-op that passes for the wrong reason.
    await screen.findByRole('option', { name: 'checkout:done' })
    const select = screen.getByLabelText('Replaced by')
    // The event being edited is not offered as its own replacement — the
    // server answers 400, and offering it invites the trip.
    expect(within(select).queryByRole('option', { name: 'checkout:completed' })).toBeNull()

    fireEvent.change(select, { target: { value: 'ev-2' } })
    expect(select).toHaveValue('ev-2')
    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))

    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({ superseded_by_event_id: 'ev-2' }),
        null,
      ),
    )
  })

  it('says how many events the search did not show', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({
      items: [{ id: 'ev-2', name: 'checkout:done' }],
      total: 240,
    } as never)
    renderForm(DEPRECATED)

    // A truncated roster and a complete one are otherwise indistinguishable,
    // which is the defect the variables tab already fixed for its own picker.
    expect(await screen.findByText('239 more not shown — narrow the search.')).toBeInTheDocument()
  })

  it('clears the successor when the event stops being deprecated', async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    // Set explicitly rather than inherited: clearAllMocks keeps implementations,
    // so leaning on the previous test's roster would make this one pass or fail
    // by ordering.
    vi.mocked(eventsApi.list).mockResolvedValue({
      items: [{ id: 'ev-2', name: 'checkout:done' }],
      total: 1,
    } as never)
    vi.mocked(eventsApi.get).mockResolvedValue({ id: 'ev-2', name: 'checkout:done' } as never)
    renderForm({ ...DEPRECATED, superseded_by_event_id: 'ev-2' } as unknown as TEvent)

    await screen.findByRole('option', { name: 'checkout:done' })
    expect(screen.getByLabelText('Replaced by')).toHaveValue('ev-2')
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'live' } })
    // The control is gone with the status, and the pointer must not survive it:
    // a successor on a live event documents a retirement that was called off.
    expect(screen.queryByLabelText('Replaced by')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({ superseded_by_event_id: null }),
        null,
      ),
    )
  })

  it('is not offered while creating, since create cannot accept it', () => {
    renderForm(null)
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'deprecated' } })

    expect(screen.getByLabelText('Sunset date')).toBeInTheDocument()
    expect(screen.queryByLabelText('Replaced by')).toBeNull()
  })
})

describe('EventForm accessibility', () => {
  it('has no axe violations on a new event and on an existing one', async () => {
    const { unmount } = renderForm(null)
    await expectNoAxeViolations(document.body)
    unmount()
    renderForm(EXISTING_EVENT)
    await expectNoAxeViolations(document.body)
  })
})

/** Whether a reload/tab-close right now would get the browser's prompt. */
function reloadIsGuarded(): boolean {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

describe('EventForm unsaved-changes guard (EVT-8)', () => {
  it('keeps an edit typed while "Save and add another" was in flight unsaved', async () => {
    let answerCreate: (value: never) => void = () => {}
    vi.mocked(eventsApi.create).mockImplementation(
      () => new Promise(resolve => { answerCreate = resolve as (value: never) => void }),
    )
    renderForm(null)

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Sent title' } })
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() => expect(eventsApi.create).toHaveBeenCalled())

    // Typed after the request left, before it answered: not part of the save.
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Typed during save' } })
    await act(async () => answerCreate({ name: 'checkout:started' } as never))

    expect(await screen.findByText(/Created checkout:started/)).toBeInTheDocument()
    expect(reloadIsGuarded()).toBe(true)
  })

  it('arms the reload prompt only once the author has changed something', () => {
    renderForm(null)
    expect(reloadIsGuarded()).toBe(false)

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Checkout completed' } })
    expect(reloadIsGuarded()).toBe(true)

    // Back to where it started: nothing is at stake again.
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: '' } })
    expect(reloadIsGuarded()).toBe(false)
  })

  it('asks before an in-app exit drops the draft, and stays put on Cancel', async () => {
    const router = createMemoryRouter(
      [
        {
          path: '/p/demo/events/all/new',
          element: createElement(EventForm, {
            slug: 'demo',
            eventTypes: [EVENT_TYPE],
            metaFields: [],
            projectVariables: [],
            event: null,
            onClose: () => void router.navigate('/p/demo/events'),
          }),
        },
        { path: '/p/demo/events', element: createElement('p', null, 'Events list') },
      ],
      { initialEntries: ['/p/demo/events/all/new'] },
    )
    render(createElement(RouterProvider, { router }), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client: queryClient }, children),
    })

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Checkout completed' } })
    // The form's own Cancel; the confirm is not open yet.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    // "Keep editing", not a second "Cancel" two seconds after the form's (AU-42).
    const confirm = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(router.state.location.pathname).toBe('/p/demo/events/all/new')
    expect(screen.getByLabelText('Title')).toHaveValue('Checkout completed')
  })

  it('does not count the branch-ticket prefill as the author\'s input', async () => {
    const jira: MetaFieldDefinition = {
      id: 'mf-jira',
      project_id: 'project-1',
      name: 'jira',
      display_name: 'Jira',
      field_type: 'string',
      is_required: false,
      enum_options: null,
      default_value: null,
      link_template: 'https://jira.example/browse/${value}',
      order: 0,
      sensitivity: 'none',
    }
    vi.mocked(planBranchesApi.list).mockResolvedValue(
      { items: [{ id: 'b-proj', name: 'PROJ-4770', kind: 'working' }], total: 1 } as never,
    )
    render(
      createElement(EventForm, {
        slug: 'demo',
        eventTypes: [EVENT_TYPE],
        metaFields: [jira],
        projectVariables: [],
        event: null,
        onClose: () => {},
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(
            QueryClientProvider,
            { client: queryClient },
            createElement(
              BranchContext.Provider,
              { value: { branchId: 'b-proj', setBranchId: () => {}, slug: 'demo' } },
              createElement(MemoryRouter, null, children),
            ),
          ),
      },
    )
    await waitFor(() => expect(screen.getByLabelText('Jira')).toHaveValue('PROJ-4770'))
    expect(reloadIsGuarded()).toBe(false)
  })
})

const NUMBER_EVENT_TYPE = {
  ...EVENT_TYPE,
  field_definitions: [
    {
      id: 'field-price',
      event_type_id: 'et-1',
      name: 'price',
      display_name: 'Price',
      field_type: 'number',
      is_required: false,
      enum_options: null,
      order: 0,
    },
  ],
} as unknown as EventType

const PRICE_VARIABLE: Variable = {
  ...PRODUCT_ID_VARIABLE,
  id: 'var-price',
  name: 'price',
  source_name: 'price',
  description: 'Price paid',
}

describe('EventForm templated number fields (EVT-23)', () => {
  it('shows a stored ${variable} in a number field and saves it back unchanged', async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    renderForm(
      {
        ...EXISTING_EVENT,
        field_values: [{ field_definition_id: 'field-price', value: '${price}' }],
      } as unknown as TEvent,
      { eventTypes: [NUMBER_EVENT_TYPE], projectVariables: [PRICE_VARIABLE] },
    )

    // A native number input sanitised the token to an empty display.
    const input = screen.getByLabelText('Price')
    expect(input).toHaveValue('${price}')
    expect(input).toHaveAttribute('inputmode', 'decimal')
    expect(input).toHaveAttribute('role', 'combobox')

    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({
          field_values: [{ field_definition_id: 'field-price', value: '${price}' }],
        }),
        null,
      ),
    )
  })

  it('opens the property autocomplete on a number field', () => {
    renderForm(null, { eventTypes: [NUMBER_EVENT_TYPE], projectVariables: [PRICE_VARIABLE] })
    fireEvent.change(screen.getByLabelText('Price'), { target: { value: '${' } })
    expect(screen.getByRole('option', { name: /\$\{price\}/ })).toBeInTheDocument()
  })

  it('takes a plain number, and refuses text that is neither a number nor a token', () => {
    renderForm(null, { eventTypes: [NUMBER_EVENT_TYPE] })
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:paid' } })
    const input = screen.getByLabelText('Price')

    fireEvent.change(input, { target: { value: '12.5' } })
    expect(input).not.toHaveAttribute('aria-invalid')
    expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled()

    fireEvent.change(input, { target: { value: 'twelve' } })
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription(/Enter a number or a \$\{variable\} token/)
    expect(screen.getByRole('button', { name: /Create event/i })).toBeDisabled()
    // Beside Save, in the save bar, in the blocking (red) tone (AU-5 / AU-6).
    expect(screen.getByText('Enter a number or a property in: Price')).toBeInTheDocument()
  })

  it('does not hold the event hostage to a stored value that is not a number', async () => {
    // The backend does not validate number values, so a scan can store `N/A`.
    // Rewriting it would freeze the field against scans; editing the rest of the
    // event must not require that.
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    renderForm(
      {
        ...EXISTING_EVENT,
        field_values: [{ field_definition_id: 'field-price', value: 'N/A', is_authored: false }],
      } as unknown as TEvent,
      { eventTypes: [NUMBER_EVENT_TYPE] },
    )

    expect(screen.getByLabelText('Price')).toHaveAttribute('aria-invalid', 'true')
    const save = screen.getByRole('button', { name: /Save event/i })
    expect(save).not.toBeDisabled()
    fireEvent.click(save)
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({
          field_values: [{ field_definition_id: 'field-price', value: 'N/A' }],
        }),
        null,
      ),
    )

    // A new value the author types is still held to a number or a token.
    fireEvent.change(screen.getByLabelText('Price'), { target: { value: 'twelve' } })
    expect(screen.getByRole('button', { name: /Save event/i })).toBeDisabled()
  })

  it('takes a decimal comma from a comma-locale keyboard and sends a point', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null, { eventTypes: [NUMBER_EVENT_TYPE] })
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:paid' } })
    const input = screen.getByLabelText('Price')

    fireEvent.change(input, { target: { value: '1,5' } })
    expect(input).not.toHaveAttribute('aria-invalid')
    const create = screen.getByRole('button', { name: /Create event/i })
    expect(create).not.toBeDisabled()
    fireEvent.click(create)
    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({
          field_values: [{ field_definition_id: 'field-price', value: '1.5' }],
        }),
        null,
      ),
    )
  })
})

describe('EventForm sunset date zone (EVT-27)', () => {
  // Pinned off UTC: CI runs in UTC, where local wall time and UTC coincide and
  // a form that treated the picker value as UTC would pass unnoticed.
  beforeEach(() => {
    vi.stubEnv('TZ', 'Asia/Tokyo')
  })
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('runs in a zone that is not UTC', () => {
    expect(new Date(2026, 9, 1).getTimezoneOffset()).toBe(-540)
  })

  it('shows the stored instant in local time and saves the same instant back', async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    renderForm({
      ...EXISTING_EVENT,
      status: 'deprecated',
      sunset_at: '2026-10-01T09:00:00Z',
    } as unknown as TEvent)

    // The picker shows local wall time — the zone the detail page formats in —
    // not the UTC wall time a slice of the ISO string gave.
    expect(screen.getByLabelText('Sunset date')).toHaveValue('2026-10-01T18:00')

    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({ sunset_at: '2026-10-01T09:00:00.000Z' }),
        null,
      ),
    )
  })

  it('sends a local time as the instant it names', async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    renderForm({ ...EXISTING_EVENT, status: 'deprecated' } as unknown as TEvent)

    fireEvent.change(screen.getByLabelText('Sunset date'), { target: { value: '2026-12-31T09:00' } })
    fireEvent.click(screen.getByRole('button', { name: /Save event/i }))

    // 09:00 in Tokyo is midnight UTC.
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-1',
        expect.objectContaining({ sunset_at: '2026-12-31T00:00:00.000Z' }),
        null,
      ),
    )
  })
})

describe('EventForm pending chip text (EVT-26)', () => {
  it('saves a tag and a column typed without pressing Enter', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({} as never)
    renderForm(null)
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'Checkout' } })
    fireEvent.change(screen.getByLabelText('Metric breakdowns'), { target: { value: 'country' } })

    fireEvent.click(screen.getByRole('button', { name: /Create event/i }))

    await waitFor(() =>
      expect(eventsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ tags: ['checkout'], metric_breakdown_columns: ['country'] }),
        null,
      ),
    )
  })

  it('turns the text into a chip when the input is left', () => {
    renderForm(null)
    const tags = screen.getByLabelText('Tags')
    fireEvent.change(tags, { target: { value: 'critical' } })
    fireEvent.blur(tags)

    expect(tags).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Remove critical tag' })).toBeInTheDocument()
  })

  it('counts text left in the tag input as an unsaved change', () => {
    renderForm(null)
    fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'critical' } })
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
  })
})

describe('EventForm "Save and add another" identity (EVT-25)', () => {
  it('refuses the name it has just created on the next press', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({ items: [], total: 0 } as never)
    vi.mocked(eventsApi.create).mockResolvedValue(
      { id: 'ev-new', name: 'pv:b2', event_type_id: 'et-1' } as never,
    )
    renderForm(null, { eventTypes: [RULED_TYPE] })

    await screen.findByText(/generated by scan rule/)
    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })
    // The probe answers "free" first — the case that used to stay cached.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Save and add another/i })).not.toBeDisabled(),
      { timeout: 3000 },
    )

    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() => expect(eventsApi.create).toHaveBeenCalledTimes(1))

    const warning = await screen.findByRole('alert')
    expect(warning).toHaveTextContent(/already answers to this name/)
    expect(within(warning).getByRole('link', { name: /open it instead/i })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-new',
    )
    expect(screen.getByRole('button', { name: /Save and add another/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Create event/i })).toBeDisabled()
  })

  it('does not hold a name taken under one type against another type', async () => {
    // Identities are unique per event type: the server keys on both.
    const OTHER_RULED_TYPE = {
      ...RULED_TYPE,
      id: 'et-2',
      name: 'other',
      display_name: 'Other',
      field_definitions: [
        {
          id: 'field-variant-2',
          event_type_id: 'et-2',
          name: 'variant',
          display_name: 'Variant',
          field_type: 'string',
          is_required: false,
          enum_options: null,
          order: 0,
        },
      ],
    } as unknown as EventType
    vi.mocked(eventsApi.list).mockResolvedValue({ items: [], total: 0 } as never)
    vi.mocked(eventsApi.create).mockResolvedValue(
      { id: 'ev-new', name: 'pv:b2', event_type_id: 'et-1' } as never,
    )
    renderForm(null, { eventTypes: [RULED_TYPE, OTHER_RULED_TYPE] })
    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-1' } })

    await screen.findByText(/generated by scan rule/)
    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Save and add another/i })).not.toBeDisabled(),
      { timeout: 3000 },
    )
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() => expect(eventsApi.create).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/already answers to this name/)).toBeInTheDocument()

    // The same composed name under the other type is free.
    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-2' } })
    await waitFor(() => expect(screen.queryByText(/already answers to this name/)).toBeNull())
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Save and add another/i })).not.toBeDisabled(),
      { timeout: 3000 },
    )
  })
})

describe('EventForm event-type change (EVT-47)', () => {
  const OTHER_TYPE = {
    ...TEMPLATE_EVENT_TYPE,
    id: 'et-2',
    name: 'other',
    display_name: 'Other',
    field_definitions: [
      {
        id: 'field-variant-2',
        event_type_id: 'et-2',
        name: 'variant',
        display_name: 'Variant',
        field_type: 'string',
        is_required: false,
        enum_options: null,
        order: 0,
      },
    ],
  } as unknown as EventType

  it('carries values onto the fields of the same name without asking', () => {
    renderForm(null, { eventTypes: [TEMPLATE_EVENT_TYPE, OTHER_TYPE] })
    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-1' } })
    fireEvent.change(screen.getByLabelText('Variant'), { target: { value: 'b2' } })

    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-2' } })

    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByLabelText(/Event type/)).toHaveValue('et-2')
    expect(screen.getByLabelText('Variant')).toHaveValue('b2')
  })

  it('asks before dropping a value the new type has no field for, and keeps it on Cancel', async () => {
    renderForm(null, { eventTypes: [JSON_TEMPLATE_EVENT_TYPE, OTHER_TYPE] })
    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-1' } })
    // The raw JSON view: the property grid opens first (F23).
    fireEvent.click(screen.getByRole('button', { name: 'Edit JSON' }))
    fireEvent.change(screen.getByLabelText('Payload'), { target: { value: '{"a":1}' } })

    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-2' } })

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/Other has no field for Payload/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(screen.getByLabelText(/Event type/)).toHaveValue('et-1')
    expect(screen.getByLabelText('Payload')).toHaveValue('{"a":1}')
  })
})

describe('EventForm required-field validation (AU-4)', () => {
  it('marks every empty required row inline instead of a browser bubble, and names them by Save', async () => {
    const otherType = { ...EVENT_TYPE, id: 'et-2', display_name: 'Other' } as EventType
    const { container } = renderForm(null, { eventTypes: [EVENT_TYPE, otherType] })
    expect(container.querySelector('form')).toHaveAttribute('novalidate')
    // Nothing is flagged while the empty form is being filled in.
    expect(screen.queryByText('Required')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Create event/i }))

    expect(await screen.findAllByText('Required')).toHaveLength(2)
    expect(screen.getByLabelText(/Event type/)).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText(/^Name/)).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText(/^Name/)).toHaveAccessibleDescription(/Required/)
    expect(screen.getByText('Fill in: Event type, Name')).toBeInTheDocument()
    expect(eventsApi.create).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'checkout:started' } })
    expect(screen.getByLabelText(/^Name/)).not.toHaveAttribute('aria-invalid')
    expect(screen.getByText('Fill in: Event type')).toBeInTheDocument()
  })
})

describe('EventForm hint wiring (EVT-48)', () => {
  it('ties a row hint and a consequential notice to the control', () => {
    renderForm(
      {
        ...EDIT_EVENT,
        field_values: [{ field_definition_id: 'field-product-id', value: 'prod_monthly', is_authored: false }],
      } as unknown as TEvent,
      { eventTypes: [EDIT_EVENT_TYPE] },
    )
    expect(screen.getByLabelText('Title')).toHaveAccessibleDescription(
      /Never part of the name a scan matches on/,
    )

    const productId = screen.getByLabelText(/Product ID/)
    fireEvent.change(productId, { target: { value: 'prod_annual' } })
    expect(productId).toHaveAccessibleDescription(/Saving this stops scans from updating the field/)
  })

  it('does not read the required star aloud; the control says it is required', () => {
    const { container } = renderForm(null)
    // The star sits beside the label, outside its text, so the accessible
    // name stays "Name" (DS-17).
    const star = container.querySelector('label[for="form-name"] + [aria-hidden="true"]')
    expect(star).toHaveTextContent('*')
    expect(star).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByLabelText(/^Name/)).toBeRequired()
  })
})

describe('EventForm duplicate names without a scan rule (AU-2)', () => {
  it('holds Create after "Save and add another" until the name changes', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue(
      { id: 'ev-new', name: 'checkout:started', event_type_id: 'et-1' } as never,
    )
    renderForm(null)

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })
    fireEvent.click(screen.getByRole('button', { name: /Save and add another/i }))
    await waitFor(() => expect(eventsApi.create).toHaveBeenCalledTimes(1))

    // An unchanged second press made a byte-identical second event.
    expect(await screen.findByText(/has just created “checkout:started”/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Create event/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Save and add another/i })).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:completed' } })
    expect(screen.queryByText(/has just created/)).toBeNull()
    expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled()
  })

  it('warns about a namesake of this type, and still lets the author create it', async () => {
    vi.mocked(eventsApi.byNames).mockResolvedValue({
      items: [{ identity: 'checkout:started', event_id: 'ev-9', name: 'checkout:started', source_name: null }],
    } as never)
    renderForm(null)

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'checkout:started' } })

    // The backend allows namesakes on a type no scan rule governs, so this is
    // advice, not a refusal.
    expect(
      await screen.findByText(/already named “checkout:started”/, undefined, { timeout: 3000 }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'open it' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-9',
    )
    expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled()
  })
})

describe('EventForm card subtitles (AU-22)', () => {
  const OWNER_TEAM: MetaFieldDefinition = {
    id: 'mf-team',
    project_id: 'project-1',
    name: 'team',
    display_name: 'Owner team',
    field_type: 'string',
    is_required: false,
    enum_options: null,
    default_value: null,
    link_template: null,
    order: 0,
    sensitivity: 'none',
  }

  it('says what each card holds and links to where it is defined', () => {
    renderForm(null, { eventTypes: [TEMPLATE_EVENT_TYPE], metaFields: [OWNER_TEAM] })

    expect(screen.getByText(/Tags are labels for finding events/)).toBeInTheDocument()
    expect(screen.getByText(/scans match the event on these/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Checkout' })).toHaveAttribute(
      'href',
      '/p/demo/event-types/et-1',
    )
    expect(screen.getByText(/Project-wide attributes for people, not scans/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Manage meta fields' })).toHaveAttribute(
      'href',
      '/p/demo/meta-fields',
    )
  })
})

describe('EventForm successor options (AU-24)', () => {
  it("names each replacement's type, so namesakes can be told apart", async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({
      items: [{ id: 'ev-2', name: 'checkout:done', event_type_id: 'et-1' }],
      total: 1,
    } as never)
    renderForm({ ...EXISTING_EVENT, status: 'deprecated' } as unknown as TEvent)

    expect(await screen.findByRole('option', { name: 'checkout:done · Checkout' })).toBeInTheDocument()
  })
})
