import { PageHeader } from '@/components/primitives/page-header'
import { PageContainer } from '@/components/primitives/page-container'
import { Button } from '@/components/ui/button'
import { SaveBar } from '@/components/forms/SaveBar'
import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import type { DuplicateCandidate, DuplicateCheckResult, EventType } from '@/types'
import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { usersApi } from '@/api/users'
import { ChipListInput } from '@/components/chip-list-input'
import { PageSkeleton } from '@/components/states'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { EVENT_STATUS_LABELS, EVENT_STATUSES } from '@/lib/eventStatus'
import type { EventStatus } from '@/lib/eventStatus'
import { ErrorState } from '@/components/error-state'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  branchEventIdentityProbesKey,
  branchEventsKey,
  eventIdentityLookupKey,
  eventTypesKey,
  usersKey,
} from '@/lib/queryKeys'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { AlertTriangle, Check, ChevronLeft, Loader2, Plus, X, type LucideIcon } from 'lucide-react'
import { EV_INPUT_CLASS, EvField, SelectControl, SurfCard } from './eventFormLayout'
import { nameFormatBaseColumns } from './utils'
import {
  bulkExtraColumns,
  bulkUnsupportedReason,
  parseBulkDraft,
  replaceBulkLineName,
  type BulkRow,
} from './bulkEventDraft'
import { DuplicateHints, DuplicateLiveRegion } from '@/components/duplicates/DuplicateHints'
import { visibleDuplicates } from '@/components/duplicates/duplicateHints'
import { useDuplicateCheck } from '@/components/duplicates/useDuplicateCheck'
import { normalizeTag } from './eventFormValues'
import { rememberCreatedEvents } from './createdEventsHandoff'
import { useCanWriteProject } from '@/lib/permissions'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const EMPTY_EVENT_TYPES: EventType[] = []

/**
 * How many distinct pasted names are checked against the catalog.
 *
 * All of them in ONE exact-name lookup (`GET /events/by-names`), answered by
 * the rule create refuses on. It used to read up to 5,000 full event
 * rows on every type selection, and then one substring search per name. Past
 * this many names the page says so rather than reporting a preview it could
 * not have verified; the server refuses a taken identity regardless, so a miss
 * here costs a rejected submit, not a duplicate. Below the route's own cap.
 */
const IDENTITY_PROBE_LIMIT = 100

/** What the lookup established about the probed names. */
interface ProbeSummary {
  /** Every probed identity an event already holds. */
  taken: ReadonlySet<string>
  /** Names the catalog was not really consulted about: the lookup failed. */
  unchecked: ReadonlySet<string>
  /** The lookup has not answered yet. */
  pending: boolean
}

const EMPTY_NAMES: ReadonlySet<string> = new Set()

/** Not a blocker: the probe answers within a second or two. */
const CHECKING_STATUS = 'Checking the names…'

const STATUS_LABEL: Record<BulkRow['status'], string> = {
  ready: 'will be created',
  incomplete: 'missing values',
  invalid: 'invalid value',
  duplicate: 'repeated above',
  exists: 'already in the catalog',
}

const STATUS_COLOR: Record<BulkRow['status'], string> = {
  ready: 'var(--fg-muted)',
  incomplete: 'var(--warning)',
  invalid: 'var(--warning)',
  duplicate: 'var(--warning)',
  exists: 'var(--warning)',
}

/** A verdict's icon, so it reads without its colour: a check for a
 *  line that will be created, a cross for one that will not. */
const STATUS_ICON: Record<BulkRow['status'], LucideIcon> = {
  ready: Check,
  incomplete: X,
  invalid: X,
  duplicate: X,
  exists: X,
}

/**
 * Author a run of events from a pasted block.
 *
 * `POST /events/bulk` has existed since the endpoint was written and had no way
 * in from the app, so a tracking plan of thirty events meant thirty passes
 * through the single-event form. What the paste carries depends
 * on the event type: where a scan names its events, the columns the name is
 * built from — because the formatted name IS the scan identity, and an event
 * authored under any other name would never merge with its traffic. Where no
 * rule governs the type, one name per line.
 */
export default function EventBulkForm() {
  const { slug, tab } = useParams<{ slug: string; tab?: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const branchId = useActiveBranchId()
  const qc = useQueryClient()
  // Creating events is an editor action; a viewer who lands here by URL is
  // sent back to the list with the reason, not shown a disabled form (#237).
  const canWrite = useCanWriteProject()

  // `null` is "not chosen yet": until the reader picks, the type the route names
  // (`/events/se/bulk`) is the choice, as on the single-event form.
  // A cleared select is '' — a choice — and stays cleared.
  const [chosenEtId, setEtId] = useState<string | null>(null)
  const [status, setStatus] = useState<EventStatus>('draft')
  const [draft, setDraft] = useState('')
  // Shared by the whole batch, so it does not need a second pass through the
  // list's bulk bar to set them.
  const [ownerId, setOwnerId] = useState('')
  const [tags, setTags] = useState<string[]>([])
  // The pasted list is the work at risk; a type or status choice alone is one
  // click to redo.
  const unsaved = useUnsavedChangesGuard(canWrite && draft.trim() !== '')

  // The query string comes along: it is the list's filters and the `?branch=`
  // EventsPage carries into this page on purpose, and dropping it returned the
  // reader to an unfiltered list on main.
  const listPath = !tab || tab === 'all' ? projectPath(currentOrgSlug(), slug, '/events') : projectPath(currentOrgSlug(), slug, `/events/${tab}`)
  const goBack = () => {
    navigate(`${listPath}${location.search}`)
  }

  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug!, branchId),
    enabled: !!slug && canWrite,
  })
  const usersQuery = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
    enabled: canWrite,
  })
  const users = usersQuery.data ?? []
  const eventTypes = eventTypesQuery.data ?? EMPTY_EVENT_TYPES
  const routedEt = tab && tab !== 'all' ? eventTypes.find(et => et.name === tab) : undefined
  const etId = chosenEtId ?? routedEt?.id ?? ''
  const selectedEt = eventTypes.find(et => et.id === etId)

  // The rule comes with the type, resolved by the server, as on the single
  // form. It used to be picked out of the scan configs by event_type_id, which
  // on a plan branch never matched — the branch copy of a type has a new id no
  // config names — so the page took free names for events a scan rule governs.
  const nameFormat = selectedEt?.event_name_format ?? null

  const namingColumns = useMemo(() => [...nameFormatBaseColumns(nameFormat)], [nameFormat])
  const fieldsByName = useMemo(
    () => new Map((selectedEt?.field_definitions ?? []).map(field => [field.name, field])),
    [selectedEt],
  )
  const unsupported = useMemo(() => {
    if (!selectedEt) return null
    return bulkUnsupportedReason({
      nameFormat,
      namingColumns,
      requiredJsonFields: selectedEt.field_definitions
        .filter(field => field.is_required && field.field_type === 'json')
        .map(field => field.name),
    })
  }, [selectedEt, nameFormat, namingColumns])
  // A required field the name is not built from used to make the whole type
  // unpasteable; the paste now carries it as a column of its own,
  // after the identity columns and before the title.
  const extraColumns = useMemo(
    () => bulkExtraColumns(selectedEt?.field_definitions ?? [], namingColumns),
    [selectedEt, namingColumns],
  )
  const extraNames = extraColumns.map(column => column.name)
  // A naming column with no field definition cannot be written as a field value,
  // so the created event would carry the name and none of what built it.
  const unmappedColumns = namingColumns.filter(column => !fieldsByName.has(column))

  // The names the paste would create, before the catalog is consulted. Debounced
  // so a paste typed line by line does not probe every half-written name.
  const debouncedDraft = useDebouncedValue(draft, 350)
  const candidateNames = useMemo(() => {
    if (!etId || unsupported) return []
    const names = new Set<string>()
    for (const row of parseBulkDraft(debouncedDraft, { columns: namingColumns, nameFormat, extraColumns })) {
      if (row.status === 'ready') names.add(row.name)
    }
    return [...names]
  }, [debouncedDraft, etId, unsupported, namingColumns, nameFormat, extraColumns])
  const probedNames = useMemo(() => candidateNames.slice(0, IDENTITY_PROBE_LIMIT), [candidateNames])
  const overLimitCount = candidateNames.length - probedNames.length

  const identityQuery = useQuery({
    queryKey: eventIdentityLookupKey(slug, branchId, etId, probedNames),
    queryFn: ({ signal }) => eventsApi.byNames(slug!, etId, probedNames, branchId, signal),
    enabled: !!slug && !!etId && probedNames.length > 0,
    // Its failure is shown on the preview ("could not be checked"), not toasted.
    meta: SILENT_ERROR_META,
  })
  const probes = useMemo<ProbeSummary>(() => {
    if (probedNames.length === 0) return { taken: EMPTY_NAMES, unchecked: EMPTY_NAMES, pending: false }
    // A failed lookup proves nothing; reading it as "not taken" would preview
    // lines the server then refuses.
    if (identityQuery.isError) {
      return { taken: EMPTY_NAMES, unchecked: new Set(probedNames), pending: false }
    }
    if (!identityQuery.data) return { taken: EMPTY_NAMES, unchecked: EMPTY_NAMES, pending: true }
    return {
      taken: new Set(identityQuery.data.items.map(item => item.identity)),
      unchecked: EMPTY_NAMES,
      pending: false,
    }
  }, [probedNames, identityQuery.isError, identityQuery.data])

  const taken = probes.taken
  const rows = useMemo(
    () =>
      etId && !unsupported
        ? parseBulkDraft(draft, { columns: namingColumns, nameFormat, taken, extraColumns })
        : [],
    [draft, etId, unsupported, namingColumns, nameFormat, taken, extraColumns],
  )
  const ready = useMemo(() => rows.filter(row => row.status === 'ready'), [rows])

  // Near-duplicates and naming hints per line that would be created (F12,
  // #265): one request for the whole paste, debounced like the identity probe.
  // A warning on the line, never a refusal — the server creates what it is sent.
  const readyRows = ready
  const duplicateCandidates = useMemo(
    (): DuplicateCandidate[] =>
      readyRows.map(row => ({
        name: row.name,
        event_type_id: etId,
        field_values: bulkFieldValues(row, namingColumns, extraColumns, fieldsByName),
      })),
    [readyRows, etId, namingColumns, extraColumns, fieldsByName],
  )
  const duplicateCheck = useDuplicateCheck({
    slug,
    branchId,
    candidates: duplicateCandidates,
    enabled: canWrite && !!etId,
  })
  const duplicatesByLine = useMemo(() => {
    const byLine = new Map<number, DuplicateCheckResult>()
    readyRows.forEach((row, index) => {
      const result = duplicateCheck.results?.[index]
      if (result) byLine.set(row.line, result)
    })
    return byLine
  }, [readyRows, duplicateCheck.results])
  // Lines whose compact hint names a match, for the table's live region.
  const duplicateLineCount = useMemo(
    () => [...duplicatesByLine.values()].filter(result => visibleDuplicates(result, undefined, 1).length > 0).length,
    [duplicatesByLine],
  )
  // Until the paste has settled and every probe has answered, "will be created"
  // would be a guess: an empty `taken` set reads every line as free, and a
  // Create pressed then sends a batch the server refuses whole.
  const checking = rows.length > 0 && (draft !== debouncedDraft || probes.pending)
  const uncheckedCount = overLimitCount + probes.unchecked.size
  // Why Create is greyed out, on the sticky bar beside it: a disabled
  // button alone left the reason off screen or unsaid.
  const cannotPaste = !!etId && (!!unsupported || unmappedColumns.length > 0)
  const blockingReason = !etId
    ? 'Pick an event type'
    : cannotPaste
      ? 'This event type cannot be filled from a pasted list'
      : rows.length === 0
        ? 'Paste at least one event name'
        : checking
          ? CHECKING_STATUS
          : ready.length === 0
            ? 'No line can be created'
            : null

  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      eventsApi.bulkCreate(
        slug!,
        ready.map(row => ({
          event_type_id: etId,
          name: row.name,
          // Only when the line gave one: the label is optional and an empty
          // string would read as a deliberate blank.
          ...(row.title ? { title: row.title } : {}),
          status,
          // Only when chosen, as with the title: the batch otherwise carries
          // exactly what it always did.
          ...(ownerId ? { owner_id: ownerId } : {}),
          ...(tags.length > 0 ? { tags } : {}),
          field_values: bulkFieldValues(row, namingColumns, extraColumns, fieldsByName),
        })),
        branchId,
      ),
    onSuccess: created => {
      qc.invalidateQueries({ queryKey: branchEventsKey(slug, branchId) })
      qc.invalidateQueries({ queryKey: branchEventIdentityProbesKey(slug, branchId) })
      unsaved.release()
      // The page used to step back to the list with no word.
      const count = Array.isArray(created) && created.length > 0 ? created.length : ready.length
      toast.success(count === 1 ? 'Created 1 event' : `Created ${count} events`)
      // And the list it returns to scrolls to and marks the new rows.
      if (Array.isArray(created)) rememberCreatedEvents(slug!, created.map(event => event.id))
      goBack()
    },
  })

  useEffect(() => {
    if (!canWrite) toast.info('Only editors can add events.', { id: 'viewer-new-event' })
  }, [canWrite])
  if (!canWrite && slug) return <Navigate replace to={`${listPath}${location.search}`} />

  if (eventTypesQuery.error) {
    return (
      <PageContainer width="narrow">
        <ErrorState
          title="Could not load the event types"
          error={eventTypesQuery.error}
          onRetry={() => void eventTypesQuery.refetch()}
        />
      </PageContainer>
    )
  }

  // The page's shape while the types load, not an empty picker.
  if (eventTypesQuery.isPending && slug) {
    return (
      <PageContainer width="narrow">
        <PageSkeleton variant="form" label="Loading event types…" />
      </PageContainer>
    )
  }

  const rowVerdict = (row: BulkRow): string => {
    if (row.status === 'incomplete') return `missing ${row.missing.join(', ')}`
    if (row.status === 'invalid') return row.problems.join('; ')
    if (row.status !== 'ready') return STATUS_LABEL[row.status]
    if (checking) return 'checking…'
    const probed = probedNames.includes(row.name)
    return probed && !probes.unchecked.has(row.name) ? STATUS_LABEL.ready : 'will be created, not checked'
  }

  // The identity columns as the hint and placeholder name them: the rule's
  // columns, or the event name where no rule governs the type.
  const identityColumns = nameFormat ? namingColumns : ['event name']
  // Past one identity column a comma separates too; a single one is split on
  // a tab only, because the value itself may carry commas.
  const separators = identityColumns.length > 1 ? 'a tab or a comma' : 'a tab'
  const columnHint = extraNames.length > 0
    ? `One event per line: ${[...identityColumns, ...extraNames].join(', then ')}, separated by ${separators}. Every event of this type needs ${extraNames.join(' and ')}.`
    : nameFormat
      ? namingColumns.length > 1
        ? `One event per line: ${namingColumns.join(', then ')}, separated by a tab or a comma.`
        : `One ${namingColumns[0] ?? 'value'} per line.`
      : 'One event name per line.'
  // The title is what follows the last column.
  const titleHint = extraNames.length > 0
    ? `Add a title after ${extraNames[extraNames.length - 1]}.`
    : namingColumns.length > 1
      ? 'Add a title after the identity columns, e.g. weather_alert,show,widget,Weather alert widget shown.'
      : 'Add a title after a tab, e.g. sign_up, a tab, then User signs up.'
  const draftPlaceholder = extraNames.length > 0
    ? [...(nameFormat ? namingColumns : ['name']), ...extraNames].join('\t')
    : nameFormat && namingColumns.length > 1
      ? namingColumns.join('\t')
      : 'one per line'

  return (
    // The narrow page container, from the shell's own left edge.
    <PageContainer width="narrow" className="space-y-0 pb-0">
      {unsaved.dialog}
      <div>
        <PageHeader
          className="mb-[18px]"
          eyebrow="Plan · Event"
          title="Add many events"
          back={
            <button
              type="button"
              onClick={goBack}
              className="inline-flex items-center gap-1 text-caption transition-colors hover:text-[var(--fg)]"
              style={{ color: 'var(--fg-muted)' }}
            >
              <ChevronLeft className="size-3.5" aria-hidden="true" /> Events
            </button>
          }
        />

        <SurfCard title="What to create">
          <EvField label="Event type" htmlFor="bulk-event-type" required last={false}>
            <SelectControl
              id="bulk-event-type"
              value={etId}
              onChange={setEtId}
              ariaRequired
            >
              <option value="">Select type…</option>
              {eventTypes.map(et => (
                <option key={et.id} value={et.id}>{et.display_name}</option>
              ))}
            </SelectControl>
          </EvField>

          <EvField
            label="Status"
            htmlFor="bulk-status"
            hint="Applied to every event created here."
          >
            <SelectControl
              id="bulk-status"
              value={status}
              onChange={value => setStatus(value as EventStatus)}
            >
              {EVENT_STATUSES.map(s => (
                <option key={s} value={s}>{EVENT_STATUS_LABELS[s]}</option>
              ))}
            </SelectControl>
          </EvField>

          <EvField label="Owner" htmlFor="bulk-owner" hint="Who answers for these events.">
            <SelectControl id="bulk-owner" value={ownerId} onChange={setOwnerId}>
              <option value="">No owner</option>
              {users.map(u => (
                <option key={u.id} value={u.id}>{u.name || u.email}</option>
              ))}
            </SelectControl>
          </EvField>

          <EvField label="Tags" htmlFor="bulk-tags" hint="Added to every event created here." last>
            <ChipListInput
              inputId="bulk-tags"
              values={tags}
              onChange={next => {
                const normalized = next.map(normalizeTag).filter(Boolean)
                setTags(normalized.filter((tag, i) => normalized.indexOf(tag) === i))
              }}
              placeholder="Type tag + Enter"
              ariaLabel="Add a tag"
            />
          </EvField>
        </SurfCard>

        {/* A type a pasted list cannot fill is a dead end unless the page
            says where to go instead: the two remedies the sentence
            names, as actions. Create is hidden below rather than left at a
            disabled "Create 0 events". */}
        {cannotPaste && selectedEt && (
          <div
            role="alert"
            className="mb-[18px] flex flex-col gap-3 rounded-card border border-warning/50 bg-warning-soft px-4 py-3 text-body-sm"
          >
            <p className="flex items-start gap-2 text-warning">
              <AlertTriangle className="mt-[3px] size-3.5 shrink-0" aria-hidden="true" />
              <span>
                {unsupported ?? (
                  <>
                    The scan builds the name from {unmappedColumns.join(', ')}, which this event type
                    has no field for — the events would carry the name and none of the values behind
                    it. Add the fields to the event type first.
                  </>
                )}
              </span>
            </p>
            <div className="flex flex-wrap gap-2">
              <Button asChild size="sm">
                <Link to={projectPath(currentOrgSlug(), slug, `/events/${tab ?? 'all'}/new${location.search}`)}>Add one at a time</Link>
              </Button>
              <Button asChild size="sm" variant="outline">
                <Link to={projectPath(currentOrgSlug(), slug, `/event-types/${selectedEt.id}`)}>
                  Edit {selectedEt.display_name} fields
                </Link>
              </Button>
            </div>
          </div>
        )}

        {etId && !unsupported && unmappedColumns.length === 0 && (
          <>
            <SurfCard
              title="Events to add"
              subtitle={
                nameFormat
                  ? `${columnHint} Each event is named by the scan rule ${nameFormat}. ${titleHint}`
                  : `${columnHint} ${titleHint}`
              }
            >
              <div className="px-4 py-3">
                <label htmlFor="bulk-draft" className="sr-only">Events to create</label>
                <textarea
                  id="bulk-draft"
                  className={`${EV_INPUT_CLASS} mono min-h-[180px] py-2 leading-[1.6]`}
                  value={draft}
                  onChange={e => setDraft(e.target.value)}
                  placeholder={draftPlaceholder}
                />
              </div>
            </SurfCard>

            {rows.length > 0 && (
              <SurfCard
                title={`${ready.length} of ${rows.length} lines will be created`}
                subtitle={
                  checking
                    ? 'Checking the names against the catalog…'
                    : uncheckedCount > 0
                      ? `${uncheckedCount === 1 ? '1 name' : `${uncheckedCount} names`} could not be checked against the catalog; the server checks ${uncheckedCount === 1 ? 'it' : 'them'} on submit.`
                      : undefined
                }
              >
                {/* One polite region for the whole table: "2 possible
                    duplicates", not one announcement per row. */}
                <DuplicateLiveRegion count={duplicateLineCount} />
                <div className="max-h-[360px] overflow-auto">
                  {/* The box above scrolls both ways, so the table's own
                      wrapper does not add a second scroller whose bar sits
                      below the fold. The line number only matters for matching
                      a row back to the paste; a phone keeps the event, title
                      and verdict. */}
                  <Table scroll={false} className="text-body-sm" aria-label="Events to create">
                    <TableHeader>
                      <TableRow className="hover:bg-transparent">
                        <TableHead scope="col" className="hidden px-4 md:table-cell">Line</TableHead>
                        <TableHead scope="col" className="max-md:pl-4">Event</TableHead>
                        {extraNames.length > 0 && (
                          <TableHead scope="col" className="px-4">Fields</TableHead>
                        )}
                        <TableHead scope="col" className="px-4">Title</TableHead>
                        <TableHead scope="col" className="px-4">Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rows.map(row => (
                        <TableRow key={row.line}>
                          <TableCell
                            className="hidden px-4 py-[6px] tabular-nums md:table-cell"
                            style={{ color: 'var(--fg-subtle)' }}
                          >
                            {row.line}
                          </TableCell>
                          {/* Sans like the catalog's names; the paste above stays
                              mono, since it is raw identifier input. */}
                          <TableCell className="py-[6px] max-md:pl-4">
                            {row.name}
                            {row.status === 'ready' && slug && (
                              <DuplicateHints
                                compact
                                slug={slug}
                                name={row.name}
                                result={duplicatesByLine.get(row.line)}
                                // Under a rule the name is built from the
                                // columns, so there is no single cell to swap.
                                onUseSuggestion={
                                  nameFormat
                                    ? undefined
                                    : suggestion => setDraft(current => replaceBulkLineName(current, row.line, suggestion))
                                }
                              />
                            )}
                          </TableCell>
                          {/* The extra columns as read, so a value that slid into
                              the title (or out of it) shows before it is stored. */}
                          {extraNames.length > 0 && (
                            <TableCell className="px-4 py-[6px]">
                              {extraNames
                                .map((column, i) => `${column}: ${row.extras[i] || '—'}`)
                                .join(', ')}
                            </TableCell>
                          )}
                          {/* The parse is the only place a stray fourth column
                              becomes visible before it is stored as a title. */}
                          <TableCell
                            className="px-4 py-[6px]"
                            style={{ color: row.title ? undefined : 'var(--fg-subtle)' }}
                          >
                            {row.title || '—'}
                          </TableCell>
                          <TableCell
                            className="px-4 py-[6px]"
                            style={{ color: STATUS_COLOR[row.status] }}
                          >
                            <BulkVerdict row={row} verdict={rowVerdict(row)} />
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </SurfCard>
            )}
          </>
        )}

        {createMut.isError && (
          <div className="mb-[18px]">
            <ErrorState compact title="Could not create the events" error={createMut.error} />
          </div>
        )}

        {/* The sticky action row: the Button primitives rather
            than hand-painted ones, and Create stays on screen however long the
            parsed list gets. */}
        <SaveBar status={blockingReason} statusTone={blockingReason === CHECKING_STATUS ? 'muted' : 'danger'}>
          <Button type="button" variant="ghost" onClick={goBack}>
            Cancel
          </Button>
          {!cannotPaste && (
            <Button
              type="button"
              onClick={() => createMut.mutate()}
              disabled={ready.length === 0 || checking || createMut.isPending}
            >
              {createMut.isPending ? <Loader2 className="animate-spin" aria-hidden="true" /> : <Plus aria-hidden="true" />}
              {ready.length === 1 ? 'Create 1 event' : `Create ${ready.length} events`}
            </Button>
          )}
        </SaveBar>
      </div>
    </PageContainer>
  )
}

/**
 * The values a line writes, keyed by field id — what the create sends and what
 * the duplicate check compares. A column with no field, or left empty, is not
 * written.
 */
function bulkFieldValues(
  row: BulkRow,
  namingColumns: readonly string[],
  extraColumns: readonly { name: string }[],
  fieldsByName: ReadonlyMap<string, { id: string }>,
): { field_definition_id: string; value: string }[] {
  return [
    ...namingColumns.map((column, position) => [column, row.values[position]] as const),
    ...extraColumns.map((column, position) => [column.name, row.extras[position]] as const),
  ].flatMap(([column, value]) => {
    const field = fieldsByName.get(column)
    return field && value ? [{ field_definition_id: field.id, value }] : []
  })
}

/** One line's verdict with its icon; "checking…" has none until it is known. */
function BulkVerdict({ row, verdict }: { row: BulkRow; verdict: string }) {
  if (verdict === 'checking…') return <>{verdict}</>
  const Icon = row.status === 'ready' && verdict !== STATUS_LABEL.ready ? AlertTriangle : STATUS_ICON[row.status]
  return (
    <span className="inline-flex items-center gap-1">
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      {verdict}
    </span>
  )
}
