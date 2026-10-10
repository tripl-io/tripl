import { PageHeader } from '@/components/primitives/page-header'
import { PageContainer } from '@/components/primitives/page-container'
import { Button } from '@/components/ui/button'
import { FieldError } from '@/components/forms/FieldError'
import { SaveBar } from '@/components/forms/SaveBar'
import {
  REQUIRED_MESSAGE,
  focusFirstInvalid,
  invalidAria,
  missingSummary,
} from '@/components/forms/validation'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import type {
  DuplicateCandidate,
  DuplicateMatch,
  Event as TEvent,
  EventMutationResponse,
  EventType,
  ImpactChange,
  MetaFieldDefinition,
  Variable,
} from '@/types'
import { aiApi } from '@/api/ai'
import { eventsApi } from '@/api/events'
import { scansApi } from '@/api/scans'
import { planBranchesApi } from '@/api/planBranches'
import { usersApi } from '@/api/users'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { useAiStatus } from '@/hooks/useAiStatus'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenario, useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { EVENT_ATTRIBUTE_LABEL } from '@/lib/eventAttributes'
import { EVENT_STATUS_LABELS, EVENT_STATUSES } from '@/lib/eventStatus'
import type { EventStatus } from '@/lib/eventStatus'
import { ErrorState } from '@/components/error-state'
import { ImpactNotice } from '@/components/dependencies/ImpactNotice'
import { DuplicateHints, DuplicateLiveRegion } from '@/components/duplicates/DuplicateHints'
import { visibleDuplicates } from '@/components/duplicates/duplicateHints'
import { useDuplicateCheck } from '@/components/duplicates/useDuplicateCheck'
import { validateJsonWithVars } from './jsonTemplate'
import { applyEventNameFormat, nameFormatBaseColumns } from './utils'
import { DateTimePicker } from '@/components/ui/date-time-picker'
import { EvField, EvInput, EvTextarea, EventsBackButton, SelectControl, SurfCard } from './eventFormLayout'
import { NoEventTypesNotice } from './NoEventTypesNotice'
import { CheckCircle2, Copy, Loader2, Plus, Save, Sparkles } from 'lucide-react'
import { branchTicket } from '@/lib/branchTicket'
import {
  branchEventIdentityProbesKey,
  branchEventsKey,
  eventNameSampleKey,
  eventTagsKey,
  planBranchesKey,
  projectEventKey,
  projectHealthRootKey,
  scansKey,
  usersKey,
} from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice } from '@/components/states'
import {
  carryFieldValues,
  isNumberFieldValue,
  normalizeNumberFieldValue,
  normalizeMetricBreakdownColumns,
  normalizeTag,
  sunsetInputValue,
  sunsetIsoValue,
  withPendingChip,
} from './eventFormValues'
import { useEventIdentityProbe, type CreatedIdentity } from './useEventIdentityProbe'
import {
  breaksNameConvention,
  inferNameConvention,
  NAME_SAMPLE_SIZE,
} from './eventNameConvention'
import { SuccessorPicker } from './SuccessorPicker'
import { ownerFieldHint } from './eventOwner'
import { useEventTypeOwners } from './useEventTypeOwners'
import { FieldValuesCard, MetaFieldsCard, TagsBreakdownsCard } from './EventFormCards'
import type { EventDuplicate } from './duplicateEvent'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { pluralize } from '@/lib/plural'

const NO_CREATED: CreatedIdentity[] = []

/** The scan naming rule of one of `eventTypes`, by id. */
const nameFormatOf = (eventTypes: readonly EventType[], eventTypeId: string): string | null =>
  eventTypes.find(et => et.id === eventTypeId)?.event_name_format ?? null
const NO_DUPLICATE_CANDIDATES: DuplicateCandidate[] = []

export function EventForm({
  slug,
  eventTypes,
  metaFields,
  projectVariables,
  event,
  defaultEventTypeId,
  onClose,
  onCreated,
  hasOtherUnsavedInput = false,
  beforeActions,
  banner,
  lockedReason,
  lockedAction,
  duplicate,
  onDuplicate,
}: {
  slug: string
  eventTypes: EventType[]
  metaFields: MetaFieldDefinition[]
  projectVariables: Variable[]
  event: TEvent | null
  defaultEventTypeId?: string
  onClose: () => void
  /** Runs after a CREATE succeeds, with the event that was created, before the
   *  form closes. Return `false` to keep it from closing — the caller has taken
   *  over the navigation. Never called on an update: the event already existed,
   *  so there is nothing here that a save makes possible. */
  onCreated?: (created: EventMutationResponse) => Promise<boolean | void> | boolean | void
  /** Input the page holds outside the form (the draft discussion note) that
   *  leaving would also lose. */
  hasOtherUnsavedInput?: boolean
  /** Rendered above the action row. The create page's draft discussion note
   *  sat below "Create event", so an author working top to bottom pressed
   *  Create — and left the page — before reaching it. Outside the
   *  fieldset and not part of the payload. */
  beforeActions?: ReactNode
  /** Rendered under the page title, as part of the page: the branch banner. */
  banner?: ReactNode
  /**
   * Why this event cannot be saved from here even by an editor — a main event
   * opened while a branch is active. The form turns read-only,
   * the reason takes Save's place on the action bar, and `lockedAction` (the
   * banner's "Switch to main") takes the button's. It used to render a form
   * with no type and no field values whose Save answered "Event not found".
   */
  lockedReason?: ReactNode
  lockedAction?: ReactNode
  /**
   * A new event started from an existing one (Duplicate): the form opens
   * filled in from `seed`, still a CREATE form — `event` stays null, so every
   * "editing a saved row" branch below keeps its meaning. Read once, by the
   * state initializers: the page keys the form on the route so a new source
   * remounts it.
   */
  duplicate?: EventDuplicate
  /** The edit form's Duplicate action. A plain navigation: the router blocker
   *  asks once about unsaved edits, the way Cancel does. */
  onDuplicate?: () => void
}) {
  const qc = useQueryClient()
  const branchId = useActiveBranchId()
  // A viewer reaching this page (a shared link, Back) gets the event read-only:
  // every control disabled and no Save, instead of a form whose Save is a 403.
  const canWrite = useCanWriteProject()
  // What the controls, the leave guard and the action row follow: a viewer
  // and a locked event are read the same way.
  const editable = canWrite && !lockedReason
  const aiEnabled = useAiStatus(slug)
  const { step: scenarioStep } = useDemoScenario()
  const { notifyStepCompleted } = useDemoScenarioActions()
  const { confirm, dialog: confirmDialog } = useConfirm()
  const formRef = useRef<HTMLFormElement>(null)
  const isNew = !event
  // Never set together with `event`: a duplicate is a new event. Held as the
  // form opened, like every value seeded from it: the page rebuilds the object
  // on each render, and a new one must not re-run what depends on it.
  const [seed] = useState(() => (event ? undefined : duplicate?.seed))
  const [duplicateOf] = useState(() => (event ? undefined : duplicate?.source))
  const [etId, setEtId] = useState(
    // Preselect the only event type so a fresh form shows its fields at once.
    event?.event_type_id
      ?? seed?.eventTypeId
      ?? defaultEventTypeId
      ?? (eventTypes.length === 1 ? (eventTypes[0]?.id ?? '') : ''),
  )
  const [name, setName] = useState(event?.name ?? seed?.name ?? '')
  const [title, setTitle] = useState(event?.title ?? seed?.title ?? '')
  const [description, setDescription] = useState(event?.description ?? seed?.description ?? '')
  // A duplicate starts as Draft: the source's Live or Implemented would claim
  // data the new event does not have. Its sunset date and successor are the
  // source's lifecycle, and stay behind with it.
  const [status, setStatus] = useState(event?.status ?? 'draft')
  const [ownerId, setOwnerId] = useState(event?.owner_id ?? seed?.ownerId ?? '')
  const [sunsetAt, setSunsetAt] = useState(() => sunsetInputValue(event?.sunset_at))
  const [supersededBy, setSupersededBy] = useState(event?.superseded_by_event_id ?? '')
  const [metricBreakdownColumns, setMetricBreakdownColumns] = useState(
    () => normalizeMetricBreakdownColumns(event?.metric_breakdown_columns ?? seed?.metricBreakdownColumns ?? []),
  )
  const [tags, setTags] = useState<string[]>(event?.tags?.map(t => t.name) ?? seed?.tags ?? [])
  const [tagInput, setTagInput] = useState('')
  const [breakdownInput, setBreakdownInput] = useState('')
  const [fieldValues, setFieldValues] = useState<Record<string, string>>(() =>
    event
      ? Object.fromEntries(event.field_values.map(fv => [fv.field_definition_id, fv.value]))
      // Copied as stored, `${token}` values included; they are authored on the
      // new event, which has no scan behind it yet.
      : { ...seed?.fieldValues },
  )
  // A list per field, even where only one value is allowed: a field with
  // `allow_multiple` carries several rows, and one shape for
  // both keeps every read site from having to ask which kind it is holding.
  const [metaValues, setMetaValues] = useState<Record<string, string[]>>(() => {
    if (!event) return seed ? { ...seed.metaValues } : {}
    const grouped: Record<string, string[]> = {}
    for (const mv of event.meta_values) {
      ;(grouped[mv.meta_field_definition_id] ??= []).push(mv.value)
    }
    return grouped
  })
  // Columns the event was ALREADY splitting by when the form opened. A column
  // added in this session has no collected rows behind it yet, so linking
  // straight to the Breakdowns tab would open an empty chart.
  const collectedBreakdownColumns = useMemo(
    () => new Set(event?.metric_breakdown_columns ?? []),
    [event],
  )
  // What the form must say about a field depends on the SAVED row, not only on
  // what is typed: `_authored_after_edit` freezes a value the moment its text
  // changes, and the box alone cannot tell a frozen value from a live one.
  const storedFieldValues = useMemo(
    () =>
      new Map(
        (event?.field_values ?? []).map(fv => [
          fv.field_definition_id,
          { value: fv.value, isAuthored: fv.is_authored ?? false },
        ]),
      ),
    [event],
  )

  // The owner used to be settable only from the list's bulk bar, after the
  // event existed; the form is where the analyst is when they know who it is
  // for. GET /users is open to any signed-in user.
  const usersQuery = useQuery({ queryKey: usersKey(), queryFn: () => usersApi.list() })
  const users = usersQuery.data ?? []
  // With no owner picked, the type's owners answer for the event and the
  // health score counts them, so the hint names them. Only main's types have
  // owners.
  const { owners: typeOwners } = useEventTypeOwners(slug, etId, { enabled: branchId === null && !ownerId })
  const ownerHint = ownerFieldHint(ownerId, eventTypes.find(et => et.id === etId)?.display_name, typeOwners)

  // A branch named after a ticket pre-fills the meta field that links to it,
  // once, on a new event; a field the reader has touched — typed into, or
  // cleared before the branch list arrived — is never overwritten, which is
  // why the check is for the KEY, not for a value. Off main
  // there is no branch name to read.
  const branchesQuery = useQuery({
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug),
    enabled: isNew && branchId !== null,
  })
  const activeBranchName = branchesQuery.data?.items.find(b => b.id === branchId)?.name
  const ticket = useMemo(
    () => branchTicket(activeBranchName, metaFields),
    [activeBranchName, metaFields],
  )
  const ticketPrefilled = useRef(false)
  // What the prefill wrote, so the unsaved-changes check below does not count
  // the form's own suggestion as the author's input.
  const [prefilledTicket, setPrefilledTicket] = useState<{ fieldId: string; key: string } | null>(null)
  // A duplicate that copied a key for this field keeps it, and the form makes
  // no suggestion of its own: the copied value is the starting point, and
  // marking it as prefilled would read the untouched form as edited.
  const seedHasTicket = !!(seed && ticket && ticket.field.id in seed.metaValues)
  useEffect(() => {
    if (!isNew || !ticket || seedHasTicket || ticketPrefilled.current) return
    ticketPrefilled.current = true
    setMetaValues(prev =>
      ticket.field.id in prev ? prev : { ...prev, [ticket.field.id]: [ticket.key] },
    )
    setPrefilledTicket({ fieldId: ticket.field.id, key: ticket.key })
  }, [isNew, seedHasTicket, ticket])

  const selectedEt = eventTypes.find(e => e.id === etId)
  const sortedFields = useMemo(
    () => selectedEt ? [...selectedEt.field_definitions].sort((a, b) => a.order - b.order) : [],
    [selectedEt],
  )
  const numberFieldIds = new Set(
    sortedFields.filter(field => field.field_type === 'number').map(field => field.id),
  )
  const editedField =sortedFields.find(field => field.name === SCENARIO_SEEDED.editedFieldName)
  const editedFieldValue = editedField ? (fieldValues[editedField.id] ?? '') : ''
  const editFieldCoachStep = scenarioStep.id === 'edit-event/set-token'
    ? 'edit-event/set-token'
    : 'edit-event/set-value'
  const editFieldCoachActive =
    scenarioStep.id === 'edit-event/set-value' || scenarioStep.id === 'edit-event/set-token'

  // Reconcile the coach with the controlled value. A restored form, browser
  // autofill, or a missed input callback must not leave a satisfied step stuck.
  useEffect(() => {
    const value = editedFieldValue.trim()
    if (
      scenarioStep.id === 'edit-event/set-value' &&
      value === SCENARIO_SEEDED.editedFieldValue
    ) {
      notifyStepCompleted('edit-event/set-value')
    } else if (
      scenarioStep.id === 'edit-event/set-token' &&
      value === SCENARIO_SEEDED.editedFieldToken
    ) {
      notifyStepCompleted('edit-event/set-token')
    }
  }, [editedFieldValue, notifyStepCompleted, scenarioStep.id])

  // The breakdown picker below asks the project's scan configs which warehouse
  // columns it can actually collect. Nothing else reads them here any more:
  // the naming rule used to be picked out of this list by event_type_id, which
  // on a plan branch never matched — a branch copy of the type has a new id no
  // config names — so the form offered free text where a scan rule governed.
  // The server now resolves the rule onto the type itself.
  const { data: scanConfigs } = useQuery({
    queryKey: scansKey(slug),
    queryFn: () => scansApi.list(slug),
  })
  // Scan naming rule: when a scan generates names for this event type, manual
  // creation must use the SAME template or the event never merges with its
  // scan-generated counterpart (identity keys on the formatted name). Read off
  // the type, which the form loads in branch context, so a branch copy carries
  // its main counterpart's rule.
  const nameFormat = isNew && selectedEt ? selectedEt.event_name_format ?? null : null
  const generatedName = useMemo(() => {
    if (!nameFormat) return null
    const valuesByField: Record<string, string> = {}
    for (const field of sortedFields) {
      const value = fieldValues[field.id]
      if (value) valuesByField[field.name] = value
    }
    return applyEventNameFormat(nameFormat, valuesByField)
  }, [nameFormat, sortedFields, fieldValues])
  // The rows that decide the name, so the card below can say so. Without this
  // the reader had to map "Fill field values for: category, action, label" —
  // raw warehouse columns — onto rows labelled "Category", "Action", "Label",
  // and a dotted key named no row at all.
  const namingColumns = useMemo(() => nameFormatBaseColumns(nameFormat), [nameFormat])
  const namingFieldIds = useMemo(
    () => sortedFields.filter(field => namingColumns.has(field.name)).map(field => field.id),
    [sortedFields, namingColumns],
  )
  // What the last "Save and add another" wrote. Cleared as soon as the form
  // describes a different event, so it can never label the one on screen now.
  const [justCreated, setJustCreated] = useState<string | null>(null)
  // Every event this form has created, for the identity check below.
  const [createdHere, setCreatedHere] = useState<CreatedIdentity[]>(NO_CREATED)
  // The identity the probe already knows is taken: what this form created,
  // plus — on a duplicate — the source, which the copied values recompose
  // under a scan rule. The source is NOT in `createdHere`: this form did not
  // create it, and the "has just created" block must not claim it did.
  const knownIdentities = useMemo(
    () =>
      duplicateOf
        ? [
            ...createdHere,
            {
              id: duplicateOf.id,
              name: nameFormatOf(eventTypes, duplicateOf.eventTypeId) ? duplicateOf.identity : duplicateOf.name,
              eventTypeId: duplicateOf.eventTypeId,
            },
          ]
        : createdHere,
    [createdHere, duplicateOf, eventTypes],
  )

  // Warehouse columns worth offering as a breakdown, in the order a reader
  // would look for them. Three sources, and the first two are what the docs have
  // described all along — "select scalar event-type fields or add another
  // warehouse column manually; JSON fields are excluded"
  // (website/docs/use/feature-reference.md). The redesign replaced that with
  // four literals, of which no production scan queries three.
  //
  //  1. the selected type's scalar fields — these ARE warehouse columns, and the
  //     backend's own support test measures a configured column against the
  //     scan's real columns (metric_rows `_is_supported_metric_breakdown_column`);
  //  2. the columns the project's scans collect scan-wide, which reserved-column
  //     handling deliberately keeps OUT of field definitions, so `platform`
  //     could otherwise only be typed from memory. The app VERSION column is
  //     excluded here although it is collected the same way: it already has its
  //     own series (Breakdowns → App version), so an event listing it as a
  //     breakdown made the collector write the same row twice, and the API now
  //     refuses it;
  //  3. anything already stored on this event, so editing never silently drops a
  //     setting the user did not touch — including a version column stored back
  //     when this list offered it, which stays visible so it can be removed.
  //
  // Anything else stays reachable through the manual input below.
  const breakdownOptions = useMemo(() => {
    const columns: string[] = []
    const add = (column: string | null | undefined) => {
      const trimmed = (column ?? '').trim()
      if (trimmed && !columns.includes(trimmed)) columns.push(trimmed)
    }
    for (const field of sortedFields) {
      if (field.field_type !== 'json') add(field.name)
    }
    for (const config of scanConfigs ?? []) {
      for (const column of config.metric_breakdown_columns ?? []) add(column)
      add(config.platform_column)
    }
    for (const column of metricBreakdownColumns) add(column)
    return columns
  }, [sortedFields, scanConfigs, metricBreakdownColumns])
  const missingFieldLabels = useMemo(() => {
    if (!generatedName) return []
    return generatedName.missing.map(key => {
      const [column, ...path] = key.split('.')
      const field = sortedFields.find(item => item.name === column)
      if (!field) return key
      return path.length > 0 ? `${field.display_name} → ${path.join('.')}` : field.display_name
    })
  }, [generatedName, sortedFields])

  // The server refuses malformed JSON with a 422 (`_normalize_json_template_value`,
  // event_service.py); ask the same question here so Save is refused with the row
  // NAMED instead of round-tripping to find out.
  //
  // It runs the validator over the value that will be POSTed rather than reading
  // JsonEditor's own error state: the editor validates what the user TYPES, so an
  // untouched field holding an invalid stored value would sail past a gate that
  // trusted the child. The client validator is strictly more permissive than the
  // server's, so this can let a 422 through — it can never block a save the server
  // would have accepted.
  const invalidJsonFieldLabels = useMemo(
    () =>
      sortedFields
        .filter(field => field.field_type === 'json')
        .filter(field => {
          const value = fieldValues[field.id] ?? ''
          // Empty is not sent at all; whitespace IS sent and `json.loads` refuses
          // it, while `validateJsonWithVars` treats it as empty.
          if (value === '') return false
          return value.trim() === '' || validateJsonWithVars(value) !== null
        })
        .map(field => field.display_name),
    [sortedFields, fieldValues],
  )
  // Number fields take text now, so the same gate names a row holding neither
  // a number nor a ${variable}. Only a value the author typed: the
  // backend does not validate number values, so a scan may have stored `N/A`,
  // and blocking on it held the whole event hostage — the description could
  // not be saved without rewriting a value that rewriting freezes against scans.
  // The field itself still warns.
  const invalidNumberFieldLabels = useMemo(
    () =>
      sortedFields
        .filter(field => field.field_type === 'number')
        .filter(field => {
          const value = fieldValues[field.id] ?? ''
          return value !== storedFieldValues.get(field.id)?.value && !isNumberFieldValue(value)
        })
        .map(field => field.display_name),
    [sortedFields, fieldValues, storedFieldValues],
  )

  const completedName =
    generatedName && generatedName.missing.length === 0 ? generatedName.name : null
  // A typed name is probed too: the check used to run only for a name a
  // scan rule composes, so on any other type "Save and add another" followed
  // by an unchanged Create made a byte-identical second event.
  const typedName = !generatedName && isNew ? name.trim() : ''
  const identityTaken = useEventIdentityProbe({
    slug,
    branchId,
    eventTypeId: etId,
    completedName: generatedName ? completedName : typedName || null,
    enabled: isNew,
    createdHere: knownIdentities,
  })
  // Under a scan rule the composed name IS the scan identity, and the server
  // refuses a second holder (409): a hard block, as before. Without one the
  // backend allows namesakes on purpose (models/event.py), so an existing
  // namesake is a warning — but repeating a name this form has just created is
  // a second press, never a plan, and blocks until the name changes.
  const identityBlocks = !!generatedName && identityTaken !== null
  const repeatsCreated =
    typedName !== ''
    && createdHere.some(item => item.name === typedName && item.eventTypeId === etId)
  // A duplicate prefills the source's name on a type without a rule. Saving it
  // unchanged makes a byte-identical second event, so it blocks the same way.
  const repeatsSource =
    !!duplicateOf
    && !repeatsCreated
    && typedName !== ''
    && typedName === duplicateOf.name
    && etId === duplicateOf.eventTypeId
  const namesake = !generatedName && !repeatsCreated && !repeatsSource ? identityTaken : null
  // Under a rule, the copied values compose the source's own identity until a
  // naming field changes; the block below says so instead of "open it instead".
  const identityIsSource = !!duplicateOf && identityTaken?.id === duplicateOf.id

  // The convention this type's own events follow, read off a few of them:
  // the placeholder shows one, and a name in another style is pointed
  // out without blocking. Only for a free name — a rule writes the name itself.
  const nameSampleQuery = useQuery({
    queryKey: eventNameSampleKey(slug, branchId, etId),
    queryFn: ({ signal }) =>
      eventsApi.list(slug, { event_type_id: etId, limit: NAME_SAMPLE_SIZE }, branchId, signal),
    enabled: isNew && !!etId && !nameFormat,
    staleTime: 60_000,
    // Advisory: without a sample the placeholder stays generic.
    meta: SILENT_ERROR_META,
  })
  const nameConvention = useMemo(
    () => inferNameConvention((nameSampleQuery.data?.items ?? []).map(item => item.name)),
    [nameSampleQuery.data],
  )

  // Near-duplicates and the project's naming convention, asked of the server
  // while the name and the values are typed (F12, #265). Advisory: nothing
  // here blocks Save. Only for a new event — an existing one already is the
  // catalog entry the check would compare against.
  const duplicateCandidates = useMemo((): DuplicateCandidate[] => {
    if (!isNew || !etId) return NO_DUPLICATE_CANDIDATES
    const candidateName = generatedName ? completedName ?? '' : name.trim()
    if (!candidateName) return NO_DUPLICATE_CANDIDATES
    const trimmedDescription = description.trim()
    return [{
      name: candidateName,
      event_type_id: etId,
      ...(trimmedDescription ? { description: trimmedDescription } : {}),
      field_values: Object.entries(fieldValues)
        .filter(([, value]) => value !== '')
        .map(([fieldId, value]) => ({ field_definition_id: fieldId, value })),
    }]
  }, [isNew, etId, generatedName, completedName, name, description, fieldValues])
  const duplicateCheck = useDuplicateCheck({
    slug,
    branchId,
    candidates: duplicateCandidates,
    enabled: isNew && editable,
  })
  const duplicateResult = duplicateCheck.results?.[0]
  // The exact namesake has its own line above, and an event this form has just
  // created is not a duplicate the author needs pointing at.
  const duplicateExclude = useMemo(() => {
    const ids = new Set(createdHere.map(item => item.id))
    if (identityTaken) ids.add(identityTaken.id)
    // The event being duplicated is named by the notice already.
    if (duplicateOf) ids.add(duplicateOf.id)
    return ids
  }, [createdHere, identityTaken, duplicateOf])
  // "Mark as replacement": `EventCreate` takes no successor (a new event has
  // no predecessor to name), so the marked event is retired AFTER the create —
  // deprecated, with the new event as its successor, through the ordinary
  // update. The same two fields the Duplicates page's merge sets.
  // Remembers the type it was marked under: a match belongs to one answer
  // about one would-be event, and the form describing another type is not it.
  const [replaces, setReplaces] = useState<
    (Pick<DuplicateMatch, 'event_id' | 'name'> & { eventTypeId: string }) | null
  >(null)
  // Adjust-during-render with an equality guard (the idiom used for
  // `justCreated` below): the mark is dropped the moment the event type
  // changes, or once the latest answer — not one still in flight, which is
  // `undefined` — no longer lists the marked event. Otherwise a save would
  // deprecate an event nothing on screen still points at.
  if (
    replaces !== null
    && (replaces.eventTypeId !== etId
      || (duplicateResult !== undefined
        && !duplicateResult.duplicates.some(match => match.event_id === replaces.event_id)))
  ) {
    setReplaces(null)
  }
  const replacesImpact = useMemo(
    (): ImpactChange[] => (replaces ? [{ kind: 'event', id: replaces.event_id, change: 'deprecate' }] : []),
    [replaces],
  )
  // How many matches the hints list, for the form's one live region.
  const duplicateMatchCount = visibleDuplicates(duplicateResult, duplicateExclude).length
  // The server's lint supersedes the sample-based pointer below once it has
  // something to say, so the same advice is not given twice.
  const serverLintShown = (duplicateResult?.lint.length ?? 0) > 0 || !!duplicateResult?.suggestion
  const offConvention =
    !generatedName && isNew && !namesake && !serverLintShown && breaksNameConvention(name, nameConvention)

  // Adjust-during-render with an equality guard — this repo's idiom for state
  // that has to follow a computed value (see the comments in
  // ProjectAlertingTab.tsx). The moment the form composes a different name it is
  // describing a different event, so the "created" line must stop claiming it.
  const composedName = generatedName ? generatedName.name : name
  if (justCreated !== null && composedName !== justCreated) setJustCreated(null)

  // Text still sitting in the Tags and column inputs is part of the draft: it
  // is added on save, so it counts as a change and is what is sent.
  const effectiveTags = withPendingChip(tags, tagInput, normalizeTag)
  const effectiveBreakdownColumns = normalizeMetricBreakdownColumns(
    withPendingChip(metricBreakdownColumns, breakdownInput),
  )

  // What this save would retire or rename, for the dependents notice (#257).
  const savedName = event?.name
  const savedStatus = event?.status
  const nextName = generatedName ? generatedName.name : name
  const impactChanges = useMemo((): ImpactChange[] => {
    if (!event) return []
    const out: ImpactChange[] = []
    if ((status === 'deprecated' || status === 'archived') && status !== savedStatus) {
      out.push({ kind: 'event', id: event.id, change: 'deprecate' })
    }
    if (nextName.trim() !== '' && nextName !== savedName) {
      out.push({ kind: 'event', id: event.id, change: 'rename' })
    }
    return out
  }, [event, nextName, savedName, savedStatus, status])

  // Everything a save would send, as one comparable string. Empty values are
  // dropped the way the payload drops them, so clearing a box you typed into
  // is not a change, and the branch-ticket prefill counts as the starting point.
  const draftSnapshot = JSON.stringify({
    etId,
    name,
    title,
    description,
    status,
    ownerId,
    sunsetAt,
    supersededBy,
    metricBreakdownColumns: effectiveBreakdownColumns,
    tags: effectiveTags,
    fieldValues: Object.entries(fieldValues).filter(([, v]) => v !== '').sort(),
    metaValues: Object.entries(metaValues)
      .map(([k, values]) => [k, values.filter(v => v !== '')] as const)
      .filter(([k, values]) =>
        values.length > 0
        && !(prefilledTicket?.fieldId === k && values.length === 1 && values[0] === prefilledTicket.key))
      .sort(),
  })
  // The draft as it was when the form opened, or as the last "Save and add
  // another" wrote it.
  const [savedSnapshot, setSavedSnapshot] = useState(draftSnapshot)
  // A viewer's form is disabled and so never dirty.
  const unsaved = useUnsavedChangesGuard(
    editable && (draftSnapshot !== savedSnapshot || hasOtherUnsavedInput),
  )

  const toggleBreakdown = (column: string) => {
    setMetricBreakdownColumns(current =>
      current.includes(column)
        ? current.filter(item => item !== column)
        : normalizeMetricBreakdownColumns([...current, column]),
    )
  }
  const commitTag = () => {
    setTags(current => withPendingChip(current, tagInput, normalizeTag))
    setTagInput('')
  }
  const commitBreakdown = () => {
    setMetricBreakdownColumns(current =>
      normalizeMetricBreakdownColumns(withPendingChip(current, breakdownInput)),
    )
    setBreakdownInput('')
  }
  const setFieldValue = (fieldId: string, value: string) =>
    setFieldValues(current => ({ ...current, [fieldId]: value }))

  // Changing the type used to wipe every field value without a word.
  // Values move to the new type's field of the same name; only when some cannot
  // follow does the form ask before dropping them.
  const changeEventType = async (nextId: string) => {
    const nextType = eventTypes.find(et => et.id === nextId)
    const carried = carryFieldValues(sortedFields, nextType?.field_definitions ?? [], fieldValues)
    if (carried.dropped.length > 0) {
      const ok = await confirm({
        title: 'Change the event type?',
        message: `${nextType ? nextType.display_name : 'No type'} has no field for ${carried.dropped.join(', ')}, so ${
          pluralize(carried.dropped.length, 'that value is', 'those values are')
        } cleared. Values of fields with the same name are kept.`,
        confirmLabel: 'Change type',
        variant: 'primary',
      })
      if (!ok) return
    }
    setEtId(nextId)
    setFieldValues(carried.values)
  }

  const aiDescribeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => aiApi.describeEvent(slug, event!.id, branchId),
    onSuccess: data => setDescription(data.description),
  })

  // `snapshot` is the draft as this save SENT it, carried in the variables: the
  // form may have been edited while the request was in flight, and storing the
  // snapshot of whatever render answers would mark those edits as saved.
  const saveMut = useMutation<
    EventMutationResponse,
    unknown,
    { closeAfterSave: boolean; snapshot: string; tags: string[]; breakdownColumns: string[] }
  >({
    meta: SILENT_ERROR_META,
    mutationFn: ({ tags: sentTags, breakdownColumns }) => {
      const payload = {
        event_type_id: etId,
        name: generatedName ? generatedName.name : name,
        title: title.trim(),
        description,
        status,
        owner_id: ownerId || null,
        sunset_at: status === 'deprecated' ? sunsetIsoValue(sunsetAt) : null,
        metric_breakdown_columns: breakdownColumns,
        tags: sentTags,
        field_values: Object.entries(fieldValues)
          .filter(([, v]) => v !== '')
          .map(([k, v]) => ({
            field_definition_id: k,
            // `1,5` from a comma-locale keyboard goes out as `1.5`; a stored
            // value left alone goes back untouched, so it is not made authored.
            value:
              numberFieldIds.has(k) && v !== storedFieldValues.get(k)?.value
                ? normalizeNumberFieldValue(v)
                : v,
          })),
        meta_values: Object.entries(metaValues).flatMap(([k, values]) => {
          // Submit what the control SHOWS. A field that held several values and
          // then lost `allow_multiple` renders as a single input on `values[0]`;
          // sending the rest would 422 the save and strand the event on a
          // setting the author may not own.
          const allowMultiple = metaFields.find(mf => mf.id === k)?.allow_multiple ?? false
          const visible = allowMultiple ? values : values.slice(0, 1)
          return visible
            .filter(value => value !== '')
            .map(value => ({ meta_field_definition_id: k, value }))
        }),
      }
      return event
        ? eventsApi.update(
            slug,
            event.id,
            {
              ...payload,
              // Cleared alongside the sunset date when the event leaves
              // `deprecated`: both answer "this is being retired, here is what
              // to do about it", and a successor left behind on a live event
              // documents a retirement that was called off.
              superseded_by_event_id: status === 'deprecated' ? supersededBy || null : null,
            },
            branchId,
          )
        : eventsApi.create(
            slug,
            {
              ...payload,
              // A duplicate inherits the source's presence threshold (F23).
              // Create only: on an update the properties grid owns it, and
              // sending it from here would overwrite what the grid saved.
              ...(seed?.requiredPresenceThreshold != null
                ? { required_presence_threshold: seed.requiredPresenceThreshold }
                : {}),
            },
            branchId,
          )
    },
    onSuccess: async (_data, { closeAfterSave, snapshot }) => {
      // The draft is saved: nothing below may be stopped by the leave guard,
      // including a caller that navigates to the created event.
      unsaved.release()
      setSavedSnapshot(snapshot)
      qc.invalidateQueries({ queryKey: branchEventsKey(slug, branchId) })
      qc.invalidateQueries({ queryKey: eventTagsKey(slug, branchId) })
      if (event) qc.invalidateQueries({ queryKey: projectEventKey(slug) })
      // Health scores the main plan: its documentation component reads the
      // description and owner edited right here, and status moves the rest.
      if (branchId === null) qc.invalidateQueries({ queryKey: projectHealthRootKey(slug) })
      if (!event) {
        // The name just created is no longer free: a probe that answered "not
        // taken" a moment ago must ask again.
        qc.invalidateQueries({ queryKey: branchEventIdentityProbesKey(slug, branchId) })
        if (_data.id && _data.name) {
          setCreatedHere(current => [
            ...current,
            { id: _data.id, name: _data.name, eventTypeId: _data.event_type_id },
          ])
        }
        // The event this one was marked to replace is retired now that its
        // successor exists. A failure leaves the new event in place and says
        // what is left to do; it never undoes the create.
        if (replaces && _data.id) {
          const replaced = replaces
          setReplaces(null)
          try {
            await eventsApi.update(
              slug,
              replaced.event_id,
              { status: 'deprecated', superseded_by_event_id: _data.id },
              branchId,
            )
            toast.success(`Deprecated ${replaced.name}; ${_data.name} replaces it.`)
          } catch (err) {
            const reason = err instanceof Error ? ` ${err.message}` : ''
            toast.error(
              `Created ${_data.name}, but could not deprecate ${replaced.name}. Set its successor from its own page.${reason}`,
            )
          }
          qc.invalidateQueries({ queryKey: projectEventKey(slug) })
        }
      }
      // Direct scenario completion — inert unless the demo's edit-event chapter
      // is sitting on exactly this step (the reducer drops everything else).
      // A save may be the mutation that lands one of the coached field states;
      // notify only states the persisted value actually satisfies. The reducer
      // drops out-of-order notices, so neither step can be skipped accidentally.
      if (editedFieldValue.trim() === SCENARIO_SEEDED.editedFieldValue) {
        notifyStepCompleted('edit-event/set-value')
      }
      if (editedFieldValue.trim() === SCENARIO_SEEDED.editedFieldToken) {
        notifyStepCompleted('edit-event/set-token')
      }
      notifyStepCompleted('edit-event/save')
      // Whatever has to happen with the event now that it EXISTS — today, the
      // discussion note drafted while it was being authored. Awaited, so the
      // form does not close out from under a post that is still in flight, and
      // able to veto the close: if the caller has already landed the author
      // somewhere else because that post failed, stepping back through history
      // on top of it would undo the recovery.
      const closeAfterCreate = event ? true : ((await onCreated?.(_data)) ?? true)
      if (closeAfterSave && closeAfterCreate) {
        onClose()
        return
      }
      if (!closeAfterCreate) return
      // Keeping the entered values is the point of this button, not an
      // oversight: it exists to author a RUN of similar events by changing one
      // field between saves, and the docs say so
      // (website/docs/use/feature-reference.md, "keeps the entered form values
      // in place for the next one"). What was missing is any sign that a save
      // happened — so pressing it again looked like the next step, and on a
      // rule-governed type the retained values regenerate the same name, which
      // is the scan identity. Say what was created, and put the
      // cursor on the field the next event most likely differs in.
      setJustCreated(_data.name)
      const form = formRef.current
      if (form) {
        const target =
          namingFieldIds[0] !== undefined
            ? form.querySelector<HTMLElement>(`#${CSS.escape(`field-${namingFieldIds[0]}`)}`)
            : form.querySelector<HTMLElement>('[id^="field-"]')
        target?.focus()
      }
    },
  })

  // One answer for both buttons and the keyboard submit path. The disable
  // expression was already written out twice; a third copy for the identity
  // check is how the two would start disagreeing.
  const cannotSave =
    saveMut.isPending
    || (generatedName?.missing.length ?? 0) > 0
    || identityBlocks
    || repeatsCreated
    || repeatsSource
    || invalidJsonFieldLabels.length > 0
    || invalidNumberFieldLabels.length > 0

  const save = (closeAfterSave: boolean) => {
    // What is left in the chip inputs becomes chips now, so the form shows what
    // was sent.
    setTags(effectiveTags)
    setTagInput('')
    setMetricBreakdownColumns(effectiveBreakdownColumns)
    setBreakdownInput('')
    saveMut.mutate({
      closeAfterSave,
      snapshot: draftSnapshot,
      tags: effectiveTags,
      breakdownColumns: effectiveBreakdownColumns,
    })
  }

  // Required rows, validated by the form rather than by browser bubbles:
  // the form is `noValidate`, a refused Save marks every empty row with
  // "Required", names them beside the button and focuses the first. Shown only
  // once Save has been pressed, never while an empty form is being filled in.
  const [submitted, setSubmitted] = useState(false)
  const requiredErrors: Record<string, string> = {}
  const missingLabels: string[] = []
  if (!etId) {
    missingLabels.push('Event type')
    if (eventTypes.length > 0) requiredErrors['form-event-type'] = REQUIRED_MESSAGE
  }
  if (!generatedName && name.trim() === '') {
    missingLabels.push('Name')
    requiredErrors['form-name'] = REQUIRED_MESSAGE
  }
  // A required value the scan rule does not name the event from (those are
  // "Fill field values for" above, which already blocks Save). The server
  // refuses the event without it.
  for (const field of sortedFields) {
    if (!field.is_required || namingColumns.has(field.name)) continue
    if ((fieldValues[field.id] ?? '') !== '') continue
    missingLabels.push(field.display_name)
    requiredErrors[`field-${field.id}`] = REQUIRED_MESSAGE
  }
  const shownErrors: Record<string, string> = submitted ? requiredErrors : {}

  const attemptSave = (closeAfterSave: boolean) => {
    if (missingLabels.length > 0) {
      setSubmitted(true)
      // After the render that marks the rows invalid.
      requestAnimationFrame(() => {
        if (formRef.current) focusFirstInvalid(formRef.current)
      })
      return
    }
    if (cannotSave) return
    save(closeAfterSave)
  }

  const typeLabel = selectedEt?.display_name ?? etId
  const blockingFieldLabels = [...invalidJsonFieldLabels, ...invalidNumberFieldLabels]
  // One line beside Save, in red because every reason here blocks it.
  // A JSON or number field can sit far above the fold, so the reason a
  // disabled Save is disabled belongs next to the button, not only beside the
  // field.
  const blockingSummary =
    blockingFieldLabels.length > 0 ? (
      <>
        {invalidJsonFieldLabels.length > 0 && <>Fix the JSON in: {invalidJsonFieldLabels.join(', ')}</>}
        {invalidJsonFieldLabels.length > 0 && invalidNumberFieldLabels.length > 0 && '. '}
        {invalidNumberFieldLabels.length > 0 && (
          <>Enter a number or a property in: {invalidNumberFieldLabels.join(', ')}</>
        )}
      </>
    ) : submitted ? (
      missingSummary(missingLabels)
    ) : null

  return (
    // The narrow page container: the shell already pads the page, so
    // the form starts at the same left edge as the list it came from instead
    // of its own centred, re-padded 880px column.
    <PageContainer width="narrow" className="space-y-0 pb-0">
      {unsaved.dialog}
      {confirmDialog}
      <form
        ref={formRef}
        noValidate
        onSubmit={e => { e.preventDefault(); attemptSave(true) }}
      >
        <PageHeader
          className="mb-[18px]"
          eyebrow="Plan · Event"
          // The edited event is named, as the metric editor names its metric:
          // a generic "Edit event" left the page without the event's name.
          title={isNew ? 'New event' : `${canWrite ? 'Edit' : 'Event'} · ${event!.name}`}
          back={<EventsBackButton onClick={onClose} />}
        />
        {banner}
        {!canWrite && <ReadOnlyNotice className="mb-[18px]" />}

        {/* `disabled` on a fieldset reaches every native control inside it, so
            the read-only view needs no per-field flag. `contents` keeps it out
            of the layout. */}
        <fieldset disabled={!editable} className="contents">

          <SurfCard title="Details">
            <EvField
              label={EVENT_ATTRIBUTE_LABEL.event_type_name}
              // A select with nothing to select is not a control, so the caption
              // names no control either (the `Field` escape hatch this repo uses
              // elsewhere for rows that hold prose).
              htmlFor={eventTypes.length === 0 ? undefined : 'form-event-type'}
              required
              hint={isNew ? undefined : "Can't be changed after creation."}
              last={false}
            >
              {eventTypes.length === 0 ? (
                <NoEventTypesNotice slug={slug} />
              ) : (
                <>
                  <SelectControl
                    id="form-event-type"
                    value={etId}
                    onChange={value => void changeEventType(value)}
                    disabled={!isNew}
                    ariaRequired
                    {...invalidAria('form-event-type', shownErrors['form-event-type'])}
                  >
                    <option value="">Select type…</option>
                    {eventTypes.map(et => <option key={et.id} value={et.id}>{et.display_name}</option>)}
                  </SelectControl>
                  <FieldError inputId="form-event-type" message={shownErrors['form-event-type']} />
                </>
              )}
            </EvField>

            <EvField
              label={EVENT_ATTRIBUTE_LABEL.name}
              htmlFor="form-name"
              required
              hint={
                generatedName ? (
                  <>
                    <span className="mono">generated by scan rule: {nameFormat}</span>
                    {/* The rule owns this box, so the analyst's wording has to go
                        somewhere the scan never reads. */}
                    <span className="mt-[2px] block">Your own wording goes in Title.</span>
                  </>
                ) : undefined
              }
              notes={
                <>
                  {generatedName && generatedName.missing.length > 0 && (
                    // Red, not amber: it blocks Save.
                    <p className="mt-1 text-body-sm text-(--danger)">
                      Fill field values for: {missingFieldLabels.join(', ')}
                    </p>
                  )}
                  {generatedName && name.trim() !== '' && (
                    // The typed name is kept in state (switching to a type with no
                    // rule brings it back), so say plainly that it is not being used
                    // rather than letting it vanish and reappear.
                    <p className="mt-1 text-body-sm text-fg-tertiary">
                      This event type names its events from the scan rule, so “{name.trim()}” is not used.
                    </p>
                  )}
                  {repeatsSource && duplicateOf && (
                    <p className="mt-1 text-body-sm text-(--danger)">
                      This is the name of {duplicateOf.name}, the event you are duplicating. Change
                      the name before saving.
                    </p>
                  )}
                  {repeatsCreated && (
                    <p className="mt-1 text-body-sm text-(--danger)">
                      This form has just created “{typedName}”. Change at least the name before
                      saving the next one.
                    </p>
                  )}
                  {namesake && (
                    <p className="mt-1 text-body-sm text-warning">
                      An event of this type is already named “{namesake.name}”:{' '}
                      <Link
                        to={projectPath(currentOrgSlug(), slug, `/monitoring/event/${namesake.id}`)}
                        className="underline underline-offset-2"
                      >
                        open it
                      </Link>
                      . Creating another splits the events that match between the two.
                    </p>
                  )}
                  {isNew && editable && (
                    <DuplicateLiveRegion count={replaces ? 0 : duplicateMatchCount} />
                  )}
                  {isNew && editable && (
                    <DuplicateHints
                      slug={slug}
                      name={generatedName ? generatedName.name : name}
                      result={duplicateResult}
                      exclude={duplicateExclude}
                      // A scan rule writes the name: there is nothing to rename.
                      onUseSuggestion={generatedName ? undefined : setName}
                      onMarkReplacement={match =>
                        setReplaces({ event_id: match.event_id, name: match.name, eventTypeId: etId })}
                      replacement={replaces}
                      onClearReplacement={() => setReplaces(null)}
                      disabled={saveMut.isPending}
                    />
                  )}
                  {replaces && (
                    <ImpactNotice slug={slug} branchId={branchId} changes={replacesImpact} />
                  )}
                  {offConvention && nameConvention && (
                    // A pointer, not a block: the name must be what the app
                    // sends, whatever the other events look like.
                    <p className="mt-1 text-body-sm text-warning">
                      Other {typeLabel} events look like “{nameConvention.example}”.
                    </p>
                  )}
                  {identityBlocks && identityTaken && identityIsSource && (
                    <p className="mt-1 text-body-sm text-(--danger)" role="alert">
                      These values name {identityTaken.name}, the event you are duplicating.
                      Change a field the name is built from before saving.
                    </p>
                  )}
                  {identityBlocks && identityTaken && !identityIsSource && (
                    <p className="mt-1 text-body-sm text-(--danger)" role="alert">
                      An event already answers to this name and would take every scan update:{' '}
                      <Link
                        to={projectPath(currentOrgSlug(), slug, `/monitoring/event/${identityTaken.id}`)}
                        className="underline underline-offset-2"
                      >
                        open it instead
                      </Link>
                      .
                    </p>
                  )}
                </>
              }
            >
              {/* readOnly, not disabled: a disabled input cannot be focused,
                  selected or copied — so the name you are about to create could
                  not be lifted into a ticket — and it is skipped by constraint
                  validation, which made the `required` mark a promise the browser
                  never kept. readOnly keeps both, and agrees with the ARIA
                  already declared here. */}
              <EvInput
                id="form-name"
                className="read-only:opacity-70"
                value={generatedName ? generatedName.name : name}
                onChange={e => setName(e.target.value)}
                // No example to offer once the rule writes this box.
                // Not a fixed sample either: "e.g. checkout:completed" suggested
                // a convention next to catalogs that use another one. A
                // name of this type's own is the example when its events agree
                // on a style; otherwise the one rule that holds everywhere.
                placeholder={
                  generatedName
                    ? undefined
                    : nameConvention
                      ? `e.g. ${nameConvention.example}`
                      : 'The exact name the app sends'
                }
                aria-required
                readOnly={!!generatedName}
                aria-readonly={generatedName ? 'true' : undefined}
                {...invalidAria('form-name', shownErrors['form-name'])}
              />
              <FieldError inputId="form-name" message={shownErrors['form-name']} />
            </EvField>

            {/* The name is the scan identity and, under a rule, not the author's
                to write; the title is the human label, and the event has room for
                exactly this split now — production had analysts' wording jammed
                into names that could never match a scan. */}
            <EvField
              label={EVENT_ATTRIBUTE_LABEL.title}
              htmlFor="form-title"
              hint="Shown beside the identity in lists and the diff. Never part of the name a scan matches on."
            >
              <EvInput
                id="form-title"
                value={title}
                onChange={e => setTitle(e.target.value)}
                maxLength={500}
                placeholder="Human-readable label, e.g. Tap on a model card"
              />
            </EvField>

            <EvField
              label={EVENT_ATTRIBUTE_LABEL.description}
              htmlFor="form-description"
              notes={
                aiDescribeMut.isError ? (
                  <p className="mt-1 text-caption text-danger">
                    {aiDescribeMut.error instanceof Error ? aiDescribeMut.error.message : 'AI unavailable'}
                  </p>
                ) : undefined
              }
            >
              <EvTextarea
                id="form-description"
                rows={2}
                value={description}
                onChange={e => setDescription(e.target.value)}
                placeholder="What does this event represent?"
              />
              {!isNew && aiEnabled && (
                <button
                  type="button"
                  onClick={() => aiDescribeMut.mutate()}
                  disabled={aiDescribeMut.isPending}
                  className="mt-[6px] inline-flex items-center gap-[5px] text-caption transition-colors hover:text-[var(--accent)]"
                  style={{ color: 'var(--fg-subtle)' }}
                >
                  {aiDescribeMut.isPending
                    ? <Loader2 className="animate-spin" size={12} aria-hidden="true" />
                    : <Sparkles size={12} aria-hidden="true" />}
                  Suggest with AI
                </button>
              )}
            </EvField>

            <EvField label={EVENT_ATTRIBUTE_LABEL.status} htmlFor="form-status">
              <SelectControl id="form-status" value={status} onChange={v => setStatus(v as EventStatus)}>
                {EVENT_STATUSES.map(s => <option key={s} value={s}>{EVENT_STATUS_LABELS[s]}</option>)}
              </SelectControl>
            </EvField>

            <EvField
              label={EVENT_ATTRIBUTE_LABEL.owner_id}
              htmlFor="form-owner"
              hint={ownerHint}
              last={status !== 'deprecated'}
            >
              <SelectControl id="form-owner" value={ownerId} onChange={setOwnerId}>
                <option value="">No owner</option>
                {users.map(u => (
                  <option key={u.id} value={u.id}>{u.name || u.email}</option>
                ))}
              </SelectControl>
            </EvField>

            {status === 'deprecated' && (
              <EvField
                label={EVENT_ATTRIBUTE_LABEL.sunset_at}
                htmlFor="form-sunset"
                hint="When this event stops being supported, in your local time."
                last={isNew}
              >
                <DateTimePicker
                  id="form-sunset"
                  label={EVENT_ATTRIBUTE_LABEL.sunset_at}
                  value={sunsetAt}
                  onChange={setSunsetAt}
                  clearable
                />
              </EvField>
            )}

            {/* Not offered while creating: `EventCreate` does not accept a
                successor — a brand-new event has no predecessor to name — so the
                control would quietly discard the choice. */}
            {status === 'deprecated' && event && (
              <SuccessorPicker
                slug={slug}
                branchId={branchId}
                eventId={event.id}
                value={supersededBy}
                onChange={setSupersededBy}
                eventTypes={eventTypes}
              />
            )}
          </SurfCard>

          {/* Deprecating, archiving or renaming a saved event names what
              depends on it (#257). A warning: Save stays as it was. */}
          {event && impactChanges.length > 0 && (
            <div className="mb-[18px]">
              <ImpactNotice slug={slug} branchId={branchId} changes={impactChanges} />
            </div>
          )}

          <TagsBreakdownsCard
            tags={tags}
            onTagsChange={setTags}
            tagInput={tagInput}
            onTagInputChange={setTagInput}
            onCommitTag={commitTag}
            breakdownOptions={breakdownOptions}
            breakdownColumns={metricBreakdownColumns}
            onToggleBreakdown={toggleBreakdown}
            breakdownInput={breakdownInput}
            onBreakdownInputChange={setBreakdownInput}
            onCommitBreakdown={commitBreakdown}
          />

          <FieldValuesCard
            slug={slug}
            event={event}
            fields={sortedFields}
            typeLabel={typeLabel}
            eventTypeId={selectedEt?.id}
            nameFormat={nameFormat}
            namingColumns={namingColumns}
            fieldValues={fieldValues}
            onFieldValueChange={setFieldValue}
            variables={projectVariables}
            storedFieldValues={storedFieldValues}
            collectedBreakdownColumns={collectedBreakdownColumns}
            breakdownColumns={metricBreakdownColumns}
            coachStep={editFieldCoachStep}
            coachActive={editFieldCoachActive}
            errors={shownErrors}
          />

          <MetaFieldsCard
            slug={slug}
            metaFields={metaFields}
            metaValues={metaValues}
            onMetaValuesChange={(id, next) => setMetaValues(current => ({ ...current, [id]: next }))}
            variables={projectVariables}
          />
        </fieldset>

        {beforeActions}

        {saveMut.isError && (
          <div className="mb-[18px]">
            <ErrorState compact title="Could not save event" error={saveMut.error} />
          </div>
        )}

        {/* The sticky action row: Save stays on screen however long the
            form, with the one line that says why it is blocked, or what the
            last "Save and add another" created. */}
        <SaveBar
          status={
            lockedReason ??
            blockingSummary ??
            (justCreated !== null ? (
              // Success-toned, with a check: a grey line was easy to
              // miss, and missing it is how a second press happened.
              <span className="inline-flex items-start gap-1.5">
                <CheckCircle2 className="mt-[3px] size-3.5 shrink-0" aria-hidden="true" />
                <span>
                  Created {justCreated}. The values below are still the ones it was made
                  from — change what differs and save the next one.
                </span>
              </span>
            ) : isNew && editable && replaces ? (
              // The one side effect of this save that is not the event itself,
              // named beside the button that causes it.
              <span data-testid="replaces-confirm">
                Creating this event also deprecates{' '}
                <span className="font-medium">{replaces.name}</span>, with this event as its
                successor.
              </span>
            ) : isNew && activeBranchName ? (
              // Where this lands, beside the button that lands it.
              <>
                Adds to branch <span className="font-medium">{activeBranchName}</span>; it
                reaches main when the branch is merged.
              </>
            ) : null)
          }
          statusTone={
            lockedReason
              ? 'warning'
              : blockingSummary
                ? 'danger'
                : justCreated !== null
                  ? 'success'
                  : isNew && editable && replaces
                    ? 'warning'
                    : 'muted'
          }
          onStatusClick={
            blockingSummary
              ? () => {
                  if (formRef.current) focusFirstInvalid(formRef.current)
                }
              : undefined
          }
        >
          <Button type="button" variant="ghost" onClick={onClose}>
            {editable ? 'Cancel' : 'Close'}
          </Button>
          {canWrite && lockedAction}
          {editable && !isNew && onDuplicate && (
            // type="button": the form's submit is Save. No requestLeave here —
            // it is a navigation, so the router blocker asks once on its own.
            <Button type="button" variant="ghost" onClick={onDuplicate}>
              <Copy aria-hidden="true" />
              Duplicate
            </Button>
          )}
          {editable && isNew && (
            <Button
              type="button"
              variant="outline"
              onClick={() => attemptSave(false)}
              disabled={cannotSave}
            >
              {saveMut.isPending
                ? <Loader2 className="animate-spin" aria-hidden="true" />
                : <Plus aria-hidden="true" />}
              Save and add another
            </Button>
          )}
          {editable && (
            <ScenarioCoachMark step="edit-event/save" when={!isNew}>
              <Button type="submit" disabled={cannotSave}>
                {saveMut.isPending
                  ? <Loader2 className="animate-spin" aria-hidden="true" />
                  : isNew ? <Plus aria-hidden="true" /> : <Save aria-hidden="true" />}
                {isNew ? 'Create event' : 'Save event'}
              </Button>
            </ScenarioCoachMark>
          )}
        </SaveBar>
      </form>
    </PageContainer>
  )
}
