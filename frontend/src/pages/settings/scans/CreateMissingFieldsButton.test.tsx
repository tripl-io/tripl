import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { EventType, ScanConfigPreview } from '@/types'

import { CreateMissingFieldsButton } from './CreateMissingFieldsButton'

// Declared with a rest parameter so the assertions below can read the exact
// argument list the component sent — the payload IS the finding.
const bulkCreate = vi.fn<(...args: unknown[]) => Promise<unknown[]>>(async () => [])

vi.mock('@/api/fields', () => ({
  fieldsApi: { bulkCreate: (...args: unknown[]) => bulkCreate(...args) },
}))

/**
 * The scan names events from `event_name`, buckets metrics on `created_at` and
 * reads releases from `app_version`. Only `country` and `props` are ordinary
 * columns, and only `props` has no field definition yet.
 */
const preview: ScanConfigPreview = {
  columns: [
    { name: 'event_name', type_name: 'String', is_nullable: false },
    { name: 'created_at', type_name: 'DateTime', is_nullable: false },
    { name: 'app_version', type_name: 'String', is_nullable: true },
    { name: 'country', type_name: 'String', is_nullable: true },
    { name: 'props', type_name: 'JSON', is_nullable: true },
  ],
  rows: [],
  json_columns: [],
}

const eventType = {
  id: 'type-1',
  display_name: 'Purchase',
  field_definitions: [{ id: 'field-1', name: 'country' }],
} as unknown as EventType

function renderButton(unmappedColumns: string[], onCreated?: () => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CreateMissingFieldsButton
        slug="demo"
        eventType={eventType}
        preview={preview}
        unmappedColumns={unmappedColumns}
        branchId={null}
        onCreated={onCreated}
      />
    </QueryClientProvider>,
  )
}

describe('CreateMissingFieldsButton — one source of truth for "which column has no field"', () => {
  beforeEach(() => bulkCreate.mockClear())

  // The defect: this button used to recompute the answer in TypeScript from
  // preview.columns minus {eventTypeColumn, timeColumn}, while the dry-run panel
  // rendered directly above it took the backend's full reserved_catalog_columns
  // set — app version, platform, event-group-rule and name-format columns too.
  // So the panel listed app_version under "Reserved columns — tripl already uses
  // these, so they never become event fields" and this button, one line below,
  // offered to create a field for it. Accepting that offer makes
  // plan_column_meta treat a reserved column as an ordinary field, which changes
  // event identity on every subsequent run.
  it('offers exactly the columns the dry run reported unmapped, and no reserved one', async () => {
    renderButton(['props'])

    expect(screen.getByRole('button', { name: 'Create 1 field' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Create 1 field' }))

    await waitFor(() => expect(bulkCreate).toHaveBeenCalledTimes(1))
    expect(bulkCreate).toHaveBeenCalledWith(
      'demo',
      'type-1',
      [{ name: 'props', display_name: 'props', field_type: 'json' }],
      null,
    )
  })

  // With nothing unmapped the panel above already says "No new fields — every
  // column is already mapped". A second component restating it in different
  // words next to a disabled button is noise, and it was the wording most likely
  // to disagree with the panel.
  it('renders nothing when the dry run found no unmapped column', () => {
    const { container } = renderButton([])

    expect(container).toBeEmptyDOMElement()
  })

  // The list is the dry run's answer from BEFORE the fields existed, so the
  // button used to keep offering "Create 1 field" — a second click made a
  // duplicate or hit a conflict, and nothing said the first one worked.
  it('says what it created, stops offering it, and asks for a fresh answer', async () => {
    const onCreated = vi.fn()
    renderButton(['props'], onCreated)

    fireEvent.click(screen.getByRole('button', { name: 'Create 1 field' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Created 1 field on "Purchase".')
    expect(screen.queryByRole('button', { name: /Create/ })).not.toBeInTheDocument()
    expect(onCreated).toHaveBeenCalledTimes(1)
  })

  it('keeps every created batch off the offer, not only the latest one', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const ui = (unmapped: string[]) => (
      <QueryClientProvider client={client}>
        <CreateMissingFieldsButton
          slug="demo"
          eventType={eventType}
          preview={preview}
          unmappedColumns={unmapped}
          branchId={null}
        />
      </QueryClientProvider>
    )
    const { rerender } = render(ui(['props']))
    fireEvent.click(screen.getByRole('button', { name: 'Create 1 field' }))
    await screen.findByText(/Created 1 field/)

    // The re-check has not landed, and the list now also names a new column.
    rerender(ui(['props', 'country']))
    fireEvent.click(screen.getByRole('button', { name: 'Create 1 field' }))
    await screen.findByText(/Created 2 fields/)
    expect(screen.queryByRole('button', { name: /^Create/ })).toBeNull()
  })

  it('says so when the fields could not be created', async () => {
    bulkCreate.mockRejectedValueOnce(new Error('Field props already exists'))
    renderButton(['props'])

    fireEvent.click(screen.getByRole('button', { name: 'Create 1 field' }))

    expect(await screen.findByText('Could not create the fields')).toBeInTheDocument()
    expect(screen.getByText('Field props already exists')).toBeInTheDocument()
  })
})
