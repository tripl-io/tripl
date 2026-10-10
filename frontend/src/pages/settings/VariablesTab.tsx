import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, RotateCcw, Trash2, Variable as VariableIcon } from "lucide-react"
import { variablesApi } from "@/api/variables"
import { useActiveBranchId } from "@/hooks/useBranch"
import type { Variable, VariableType } from "@/types"
import { useConfirm } from "@/hooks/useConfirm"
import { ConfirmImpactMessage } from "@/components/dependencies/ImpactNotice"
import { Button } from "@/components/ui/button"
import { IconButton } from "@/components/ui/icon-button"
import { FilterBar, FilterSearch } from "@/components/ui/filter-bar"
import { SegmentedControl } from "@/components/ui/segmented-control"
import { CodeToken } from "@/components/primitives/code-token"
import { PageContainer } from "@/components/primitives/page-container"
import { PageHeader } from "@/components/primitives/page-header"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { EmptyState } from "@/components/empty-state"
import { ErrorState } from "@/components/error-state"
import { Panel } from "@/components/settings/kit"
import { useDemoScenarioActions } from "@/demo/demoScenarioContext"
import { SCENARIO_SEEDED } from "@/demo/scenarioModel"
import { bindingExample } from "./bindingExample"
import { VariablesBulkBar } from "./VariablesBulkBar"
import { VariablesCreateDialog } from "./VariablesCreateDialog"
import { VariablesEditDialog } from "./VariablesEditDialog"
import { variableDetailPath } from "./variable-detail/variableDetailPath"
import { VariablesTableRow } from "./VariablesTableRow"
import { TYPE_LABELS } from "./variablesShared"
import { useVariableSelection } from "./useVariableSelection"
import { invalidValuesFor } from "./variableValueValidation"
import { cn, getErrorMessage } from '@/lib/utils'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice } from '@/components/states'
import { countOf, pluralize } from '@/lib/plural'
import { projectKey, variablesKey, variablesUsagePageKey } from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { PropertyDriftList } from '@/pages/events/PropertyDriftList'
import { STICKY_ACTIONS_CLASS } from './stickyActions'

// Rows rendered at once. The whole set arrives in one request, but a governance
// project can hold >1k variables and painting them all froze the tab for
// seconds — one page keeps the DOM and every re-render bounded.
const PAGE_SIZE = 50
const LOADING_SKELETON_ROWS = 6

// Matching spans every token the SCAN would resolve — display name, scan
// identity and user-editable bindings — not just what the row leads with. On a
// project whose variables were slugged by derive_display_name the raw path is
// the only name a person knows: 576 of production's acme-ios rows render as
// `${aalter}` over `property.Aalter`, so a search for "property" found none of
// them.
const matchesQuery = (variable: Variable, needle: string) =>
  variable.name.toLowerCase().includes(needle) ||
  variable.description.toLowerCase().includes(needle) ||
  (variable.source_name ?? '').toLowerCase().includes(needle) ||
  (variable.bindings ?? []).some(binding => binding.toLowerCase().includes(needle))

// Server-side, because the honest answer needs data this page does not hold.
// "Unused" is NOT "event_count is zero": a variable can have no observed
// context and still be named by a live event's field value (18 rows
// on production). Only the backend sees every stored value, so it decides — and
// the count sitting under a select-all checkbox is then exactly the set the
// retirement sweep would take, not a superset that includes rows still in use.
const USAGE_FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'used', label: 'In use' },
  { value: 'unused', label: 'Unused' },
] as const
type UsageFilter = (typeof USAGE_FILTERS)[number]['value']

/** Keeps a handler's identity stable across renders so the memoized rows do not
 * re-render every time an unrelated closure above them is recreated. The ref is
 * refreshed after commit; rows only call these from user events, never during
 * render, so they always see the latest closure. */
function useStableCallback<Args extends unknown[]>(fn: (...args: Args) => void) {
  const ref = useRef(fn)
  useEffect(() => {
    ref.current = fn
  })
  return useCallback((...args: Args) => ref.current(...args), [])
}

/** `focusId` (the `?focus=` param) scrolls to and highlights one variable —
 * where the variable sits in the list. Editing it is its own page now,
 * so a link that wants the editor goes there rather than here.
 *
 * The create and edit dialogs are their own components, each owning its form
 * state and queries, and the selection lives in `useVariableSelection`: this
 * page used to hold about 25 pieces of state and re-render its whole table and
 * both dialogs on every keystroke. */
export function VariablesTab({
  slug,
  focusId,
}: {
  slug: string
  focusId?: string
}) {
  const qc = useQueryClient()
  const branchId = useActiveBranchId()
  // Stable, so the memoized rows do not all re-render for a new function.
  const eventHref = useCallback((eventId: string) => projectPath(currentOrgSlug(), slug, `/events/all/${eventId}`), [slug])
  // The `${name}` token opens the variable's own page.
  const detailHref = useCallback((variableId: string) => variableDetailPath(slug, variableId), [slug])
  const listedEventsHref = useCallback(
    (variableId: string) => variableDetailPath(slug, variableId, 'events'),
    [slug],
  )
  const canWrite = useCanWriteProject()
  const focusRef = useRef<HTMLTableRowElement | null>(null)
  // The excluded panel renders <li>s, not table rows, so the focused variable
  // there needs its own ref — see the scroll effect below.
  const excludedFocusRef = useRef<HTMLLIElement | null>(null)
  const [showForm, setShowForm] = useState(false)
  // The id of the variable being edited, plus the row as it was when opened.
  // The dialog is handed the LIVE row from the list; the snapshot is
  // only the fallback for a row that drops out of the list while the dialog is
  // open — a usage filter it no longer matches, or a colleague's delete.
  const [editing, setEditing] = useState<{ id: string; snapshot: Variable } | null>(null)
  const [filterText, setFilterText] = useState('')
  const [usageFilter, setUsageFilter] = useState<UsageFilter>('all')
  // Page the reviewer picked, tagged with the focus target it was picked under
  // (undefined = never picked, so a ?focus= link still gets to choose).
  const [pickedPage, setPickedPage] = useState<{ focusId?: string; page: number }>({ page: 0 })
  const { confirm, dialog } = useConfirm()
  const { notifyStepCompleted } = useDemoScenarioActions()

  // ONE request for the whole tab. The list row's event names and observed
  // values ship with this response (see attach_variable_summaries), so there is
  // no per-row fan-out — the same anti-pattern documented in
  // pages/events/useEventRowMetrics.ts. `keepPreviousData` holds the previous
  // rows while the branch id resolves and changes the key, instead of dropping
  // back to an empty list.
  const variablesQuery = useQuery({
    // The PAGE key, not the items key: this is the one caller that needs
    // `total`, and caching the envelope under the shared key is what fed the
    // events rows an object instead of an array.
    // The usage filter is part of the key because it is answered server-side —
    // the page cannot narrow to "unused" itself without every event's stored
    // field values.
    queryKey: variablesUsagePageKey(slug, branchId, usageFilter),
    queryFn: () => variablesApi.listPage(slug, branchId, { usage: usageFilter }),
    placeholderData: keepPreviousData,
    // Rendered in the panel, with a retry.
    meta: SILENT_ERROR_META,
  })
  const variablePage = variablesQuery.data
  const variablesPending = variablesQuery.isPending
  const variables = useMemo(() => variablePage?.items ?? [], [variablePage])
  // Drawn from this project rather than hard-coded, because the hard-coded one
  // was the confusion: `page_data.extra.variant` is three segments deep in a
  // container many warehouses do not have, so it read as a different namespace
  // from the tokens the reader actually picks.
  const example = useMemo(() => bindingExample(variables), [variables])
  const truncatedCount = Math.max(0, (variablePage?.total ?? 0) - variables.length)

  const activeVariables = useMemo(
    () => variables.filter(v => !v.excluded_from_scans),
    [variables],
  )
  const excludedVariables = useMemo(
    () => variables.filter(v => !!v.excluded_from_scans),
    [variables],
  )

  // One row PER VARIABLE: the variable's events (names) and its observed values
  // both arrive on the list row, so a variable referenced by N events still
  // reads as a single entry, not N duplicate rows.
  const matchingVariables = useMemo(() => {
    const needle = filterText.trim().toLowerCase()
    if (!needle) return activeVariables
    return activeVariables.filter(variable => matchesQuery(variable, needle))
  }, [activeVariables, filterText])
  const matchingIds = useMemo(
    () => new Set(matchingVariables.map(variable => variable.id)),
    [matchingVariables],
  )

  const selection = useVariableSelection({
    branchId,
    matchingIds,
    loaded: variablePage !== undefined,
  })
  const { selectedIds, deselect } = selection

  const bulkUpdateMut = useMutation({
    // Its error is rendered in the bulk bar.
    meta: SILENT_ERROR_META,
    mutationFn: (patch: { variable_type?: VariableType; description?: string; allowed_values_add?: string[] }) =>
      variablesApi.bulkUpdate(slug, { variable_ids: [...selectedIds], ...patch }, branchId),
    onSuccess: () => qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) }),
  })

  const bulkDeleteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => variablesApi.bulkDelete(slug, [...selectedIds], branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) })
      // The sidebar count reads the project summary.
      qc.invalidateQueries({ queryKey: projectKey(slug) })
      selection.clear()
    },
  })

  const handleBulkDelete = async () => {
    bulkUpdateMut.reset()
    bulkDeleteMut.reset()
    const ok = await confirm({
      title: 'Delete properties',
      // Where the variables are bound and used, listed under the question
      // (#257); a warning, Delete stays armed.
      message: (
        <ConfirmImpactMessage
          message={`Delete ${countOf(selectedIds.size, 'selected property', 'selected properties')}? Event fields referencing them will keep the literal text.`}
          slug={slug}
          branchId={branchId}
          changes={[...selectedIds].map(id => ({ kind: 'variable' as const, id, change: 'delete' as const }))}
        />
      ),
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) bulkDeleteMut.mutate()
  }

  // A type change for the whole selection — which can reach past the page on
  // screen — used to apply the moment the select changed, and arrowing through
  // a closed <select> fires a change per option. The bar now stages
  // the type; this asks before it applies, and says which documented values the
  // new type would refuse. Resolves true once the change has landed,
  // so the bar knows to drop its draft.
  const handleBulkSetType = async (variableType: VariableType): Promise<boolean> => {
    bulkUpdateMut.reset()
    bulkDeleteMut.reset()
    const selected = variables.filter(variable => selectedIds.has(variable.id))
    const conflicting = selected.filter(
      variable => invalidValuesFor(variableType, variable.allowed_values ?? []).length > 0,
    )
    const conflictNote = conflicting.length > 0
      ? ` ${countOf(conflicting.length, 'of them has', 'of them have')} documented values that are not valid ${TYPE_LABELS[variableType]} values, and drift will never match those.`
      : ''
    const ok = await confirm({
      title: 'Change property type',
      message: `Change the type of ${countOf(selectedIds.size, 'selected property', 'selected properties')} to ${TYPE_LABELS[variableType]}?${conflictNote}`,
      confirmLabel: 'Change type',
      variant: 'primary',
    })
    if (!ok) return false
    await bulkUpdateMut.mutateAsync({ variable_type: variableType })
    return true
  }

  // Bulk-added values meet the same per-type check as the dialogs:
  // "abc" added to thirty Number variables used to go straight through. The
  // selection can mix types, so each value is checked against every selected
  // variable's own type, and a conflict asks before it is written.
  const handleBulkAddValues = async (values: string[]): Promise<boolean> => {
    bulkUpdateMut.reset()
    bulkDeleteMut.reset()
    const selected = variables.filter(variable => selectedIds.has(variable.id))
    const conflicting = selected.filter(
      variable => invalidValuesFor(variable.variable_type, values).length > 0,
    )
    if (conflicting.length > 0) {
      const types = [...new Set(conflicting.map(variable => TYPE_LABELS[variable.variable_type]))]
      const ok = await confirm({
        title: 'Add values that do not fit',
        message: `${countOf(conflicting.length, 'selected property is', 'selected properties are')} typed ${types.join(', ')}, and some of ${values.join(', ')} are not valid values of that type. Drift will never match them. Add them anyway?`,
        confirmLabel: 'Add anyway',
        variant: 'danger',
      })
      if (!ok) return false
    }
    await bulkUpdateMut.mutateAsync({ allowed_values_add: values })
    return true
  }

  const deleteMut = useMutation({
    // Its error is rendered under the table.
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => variablesApi.del(slug, id, branchId),
    // The row is gone server-side, so a selection still naming it inflates the
    // next bulk confirm — "Delete 12 selected variables?" over eleven rows — and
    // then takes the whole bulk call down with it: `_load_variables_by_ids`
    // raises 404 for the batch on the first id it cannot load.
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) })
      qc.invalidateQueries({ queryKey: projectKey(slug) })
      deselect(id)
    },
  })

  const excludeMut = useMutation({
    // Its error is rendered under the table.
    meta: SILENT_ERROR_META,
    mutationFn: ({ id, excluded }: { id: string; excluded: boolean }) =>
      variablesApi.update(slug, id, { excluded_from_scans: excluded }, branchId),
    // Excluding moves the row out of the table and into the panel below it, so a
    // still-selected tombstone rides along on the next bulk Delete — and per the
    // Delete copy right above, deleting an excluded variable un-excludes the
    // name, because the flag is a column on the row being dropped. The next scan
    // then re-creates it and the operator's instruction is silently revoked.
    //
    // Restore (`excluded: false`) needs no guard and gets none: it only ADDS a
    // row back to the match set, which can never leave an id naming a row nobody
    // can see. The excluded panel has no checkbox either, so a restored id can
    // only ever be one this selection already dropped on the way out.
    onSuccess: (_data, { id, excluded }) => {
      qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) })
      if (excluded) deselect(id)
    },
  })

  const handleDelete = useStableCallback(async (v: Variable) => {
    // Delete is the one variable action that really does drop rows, and the copy
    // named only the field text that SURVIVES it. What goes is the part a reader
    // misses afterwards and cannot rebuild: the value contexts held against this
    // variable and the drift raised on them, both cascaded off the id. Naming
    // them in the row's own vocabulary costs no request — the list row already
    // carries both counts, for the drift badge and the observed-values cell.
    const contextCount = v.context_count ?? 0
    const driftCount = v.open_drift_count ?? 0
    const recorded = [
      contextCount > 0 ? countOf(contextCount, 'value context', 'value contexts') : null,
      driftCount > 0 ? countOf(driftCount, 'open drift', 'open drifts') : null,
    ].filter((part): part is string => part !== null)
    // The verb agrees with the total, not the phrase count: "1 value context and
    // 1 open drift GO with it", but "1 open drift GOES with it".
    const recordedNote =
      recorded.length > 0
        ? ` Its ${recorded.join(' and ')} ${pluralize(contextCount + driftCount, 'goes', 'go')} with it.`
        : ''
    // A scan-managed variable comes back, and it comes back WITHOUT the
    // exclusion, because the flag was a column on the row just deleted.
    // Suggesting Exclude to someone already looking at an excluded variable is
    // advice they have taken; what they need instead is that deleting undoes
    // it.
    const rescanNote = !(v.source_name || (v.bindings ?? []).length > 0)
      ? ''
      : v.excluded_from_scans
        ? ' The next scan will likely re-create it, un-excluded — the exclusion is a flag on the row you are deleting.'
        : ' The next scan will likely re-create it — use Exclude to keep it out.'
    deleteMut.reset()
    excludeMut.reset()
    const ok = await confirm({
      title: 'Delete property',
      message: (
        <ConfirmImpactMessage
          message={`Delete "${v.name}"?${recordedNote} Any event fields referencing \${${v.name}} will keep the literal text.${rescanNote}`}
          slug={slug}
          branchId={branchId}
          changes={[{ kind: 'variable', id: v.id, change: 'delete' }]}
        />
      ),
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate(v.id)
  })

  const handleExclude = useStableCallback(async (v: Variable) => {
    // Describes a flag, because that is now all this is: the exclusion is what
    // every scan-side guard reads, and nothing is deleted to enforce it. Naming
    // the three things that stop is the useful half — "excluded from scans"
    // does not say on its own whether an already-open drift keeps firing.
    //
    // The other half is scoped to THIS act on purpose. Excluding deletes
    // nothing, which is a fact about the button the reader is about to press;
    // that the records then survive every later scan is not this dialog's to
    // promise, so it does not. Nor does it say Restore brings the values back —
    // Restore clears the flag, and what it restores is the variable's place in
    // scans.
    deleteMut.reset()
    excludeMut.reset()
    const ok = await confirm({
      title: 'Exclude from scans',
      message: `Exclude "${v.name}" from scans? Excluding itself deletes nothing — the values and drift already recorded are left where they are — but future scans will NOT re-create it, sample new values for it, or raise drift on it. Restore puts the property back in scans.`,
      confirmLabel: 'Exclude',
      // Not 'danger': a reversible flag with no data loss behind it should not
      // wear the same red confirm as Delete, which really does drop the rows.
      variant: 'primary',
    })
    if (ok) excludeMut.mutate({ id: v.id, excluded: true })
  })

  const startEdit = useStableCallback((v: Variable) => {
    // Opening the seeded variable IS inspecting its values — the edit dialog
    // shows documented vs observed side by side.
    if (v.name === SCENARIO_SEEDED.driftVariableName) {
      notifyStepCompleted('variables/inspect-values')
    }
    setEditing({ id: v.id, snapshot: v })
  })
  const editingVariable = editing
    ? variables.find(v => v.id === editing.id) ?? editing.snapshot
    : null

  // A branch-diff link points at one variable, which may sit on any page. The
  // page is DERIVED rather than synced in an effect: until the reviewer picks a
  // page themselves, the focused variable's page wins — its row only becomes
  // locatable once the list has arrived, well after the first render.
  const focusIndex = focusId === undefined
    ? -1
    : matchingVariables.findIndex(variable => variable.id === focusId)
  const focusPage = focusIndex < 0 ? 0 : Math.floor(focusIndex / PAGE_SIZE)
  const pageCount = Math.max(1, Math.ceil(matchingVariables.length / PAGE_SIZE))
  const chosenPage = pickedPage.focusId === focusId ? pickedPage.page : null
  const currentPage = Math.min(chosenPage ?? focusPage, pageCount - 1)
  const pageStart = currentPage * PAGE_SIZE
  const pageVariables = useMemo(
    () => matchingVariables.slice(pageStart, pageStart + PAGE_SIZE),
    [matchingVariables, pageStart],
  )
  const goToPage = (next: number) =>
    setPickedPage({ focusId, page: Math.min(Math.max(0, next), pageCount - 1) })

  /** Runs a control that changes WHICH ROWS MATCH, and drops the selection with
   * it.
   *
   * Selection deliberately spans every matching row rather than the page on
   * screen, so once the match set moves the selected ids can be rows nobody can
   * see or name. The usage-filter buttons cleared the selection; the filter
   * text box did not. Filter "checkout", tick select-all, retype
   * to "payment": the table showed only payment rows, all unticked, and the
   * bulk bar still said "12 selected". Delete confirmed with a bare count and
   * destroyed the twelve checkout variables, cascading their value contexts and
   * drifts — the ids were still loaded client-side, so nothing 404'd and no
   * toast fired. Set type, Set description and Add values hit the same
   * invisible rows.
   *
   * CLEARING, not intersecting with the visible rows: an intersection would
   * make refining a filter and then broadening it silently DROP selections the
   * operator never deselected, which is the same invisibility defect pointed
   * the other way. Pagination is deliberately NOT routed through here — it
   * changes which matching rows are painted, not which rows match, and
   * selecting across pages is the reason this table has a select-all at all.
   *
   * TWO controls route through here and they are the only two that should: the
   * filter text box and the usage-filter buttons (and the empty state's "Show
   * all", which is the usage filter by another name), each of which redraws the
   * match-set boundary under every row at once.
   *
   * The rest of the invariant lives in `useVariableSelection`: row-level Exclude
   * and Delete drop their ONE id through `deselect`, a branch switch clears the
   * selection beside the state itself, and data that moves under an untouched
   * selection is pruned there.
   *
   * Adding a control that narrows the match set WHOLESALE means routing it
   * through here; adding one that moves a single row means `deselect`. */
  const changeMatchSet = (apply: () => void) => {
    apply()
    selection.clear()
    goToPage(0)
  }

  // `excluded_from_scans` is a tracked plan-diff key, so a branch diff can carry
  // a "variable X — excluded from scans" row whose link lands here. X is exactly
  // the variable `activeVariables` filters OUT of the table, so `findIndex`
  // returned -1, `focusPage` fell back to 0, and the reviewer arrived on page 1
  // of an unrelated list with nothing marked — while X sat, unmarked, in the
  // "Excluded from scans" panel further down. Following the link
  // now marks the row wherever it renders.
  const focusedExcludedVisible =
    focusIndex < 0 && focusId !== undefined && excludedVariables.some(v => v.id === focusId)

  // Scroll the linked row into view once it is on screen. Keyed on focusId too,
  // so following a second link — to a variable already visible — scrolls to it
  // instead of leaving the reviewer where the first one landed.
  const focusedRowVisible = focusIndex >= 0 && focusPage === currentPage
  useEffect(() => {
    if (focusedRowVisible) {
      focusRef.current?.scrollIntoView({ block: 'center' })
    } else if (focusedExcludedVisible) {
      excludedFocusRef.current?.scrollIntoView({ block: 'center' })
    }
  }, [focusId, focusedRowVisible, focusedExcludedVisible])

  // Select-all reads the whole match set: ticked when every match is selected,
  // mixed when only some are. With some rows ticked it used to show unchecked,
  // and a click then selected every match across all pages.
  const selectedMatching = matchingVariables.reduce((count, v) => count + (selectedIds.has(v.id) ? 1 : 0), 0)
  const allMatchingSelected = matchingVariables.length > 0 && selectedMatching === matchingVariables.length
  const someMatchingSelected = selectedMatching > 0 && !allMatchingSelected
  const selectAllRef = useCallback(
    (node: HTMLInputElement | null) => {
      if (node) node.indeterminate = someMatchingSelected
    },
    [someMatchingSelected],
  )

  // Delete, Exclude and Restore used to fail in silence.
  const rowActionError = deleteMut.isError
    ? `Could not delete the property: ${getErrorMessage(deleteMut.error)}`
    : excludeMut.isError
      ? `Could not ${excludeMut.variables?.excluded ? 'exclude' : 'restore'} the property: ${getErrorMessage(excludeMut.error)}`
      : null
  const bulkError = bulkUpdateMut.isError
    ? bulkUpdateMut.error
    : bulkDeleteMut.isError
      ? bulkDeleteMut.error
      : null

  const selectionActive = canWrite && selectedIds.size > 0

  const showAllAction = usageFilter !== 'all' ? (
    <Button type="button" size="sm" variant="outline" onClick={() => changeMatchSet(() => setUsageFilter('all'))}>
      Show all properties
    </Button>
  ) : undefined

  return (
    // Room under the table while the floating bulk bar is up, so it never sits
    // over the pagination or the last rows.
    <PageContainer className={cn('space-y-4', selectionActive && 'pb-40 sm:pb-20')}>
      {dialog}
      {/* The shared page header: the page had no title of its own,
          only the Panel's 12.5px one. "New variable" matches the dialog it
          opens. */}
      <PageHeader
        eyebrow="Plan"
        title="Properties"
        // One sentence of purpose, like its siblings, and the definition the
        // Concepts glossary gives.
        description={<>Typed, reusable values that event field values reference as placeholders like <CodeToken>{'${platform}'}</CodeToken>. Scans learn which values each one takes.</>}
        actions={
          canWrite && (
            <Button size="sm" onClick={() => setShowForm(true)}>
              <Plus className="size-3.5" />New property
            </Button>
          )
        }
      />
      {!canWrite && <ReadOnlyNotice />}
      {/* Open property drift across the project (F23): what the sidebar's
          Properties badge counts. Detected against main, so main only. */}
      {branchId === null && <PropertyDriftList slug={slug} readOnly={!canWrite} />}

      {showForm && (
        <VariablesCreateDialog
          slug={slug}
          branchId={branchId}
          example={example}
          onClose={() => setShowForm(false)}
        />
      )}

      {editingVariable && (
        <VariablesEditDialog
          key={editingVariable.id}
          slug={slug}
          branchId={branchId}
          variable={editingVariable}
          canWrite={canWrite}
          example={example}
          onClose={() => setEditing(null)}
        />
      )}

      <Panel
        title="All properties"
        subtitle={variablesPending
          ? 'Loading…'
          : countOf(activeVariables.length, 'property', 'properties')}
      >
        {variablesPending ? (
          // A pending list is NOT an empty list — rendering the empty state here
          // made the page claim "No variables" while 1.2k were loading.
          <div className="space-y-2 px-4 py-4" aria-busy="true" aria-label="Loading properties">
            {Array.from({ length: LOADING_SKELETON_ROWS }, (_, index) => (
              <Skeleton key={index} className="h-10 w-full" />
            ))}
          </div>
        ) : variablePage === undefined ? (
          // Nor is a failed one: with no rows to show, the error is the answer.
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load properties"
              error={variablesQuery.error}
              onRetry={() => { void variablesQuery.refetch() }}
              retryLabel="Retry"
            />
          </div>
        ) : (
          <>
            {/* The filters stay up whatever they match. They used to render only
                beside a non-empty table, so "Unused" on a project with nothing
                to retire replaced the whole panel — All included — with an
                empty state, and a reload was the only way back. */}
            {/* The shared filter bar: "Search variables…" and the
                result count on the right. The usage scope is the one
                segmented control instead of a row of text buttons. */}
            {/* Over a project with no variables at all, the search and the
                usage scope filter nothing; the empty state below is the
                answer. A non-default scope keeps them up, so the way
                back to All is always on screen. */}
            {(activeVariables.length > 0 || usageFilter !== 'all') && <FilterBar
              className="px-4 py-2"
              count={
                activeVariables.length > 0
                  ? `${matchingVariables.length === 0
                    ? 'No matches'
                    : `Showing ${pageStart + 1}–${pageStart + pageVariables.length} of ${matchingVariables.length}`}${
                    truncatedCount > 0 ? ` (${truncatedCount} more not loaded)` : ''}`
                  : undefined
              }
            >
              <FilterSearch
                things="properties"
                aria-label="Filter properties"
                value={filterText}
                onValueChange={value => changeMatchSet(() => setFilterText(value))}
              />
              <SegmentedControl
                aria-label="Filter by usage"
                size="sm"
                value={usageFilter}
                options={USAGE_FILTERS}
                onChange={value => changeMatchSet(() => setUsageFilter(value))}
              />
            </FilterBar>}
            {variablesQuery.isError && (
              // Rows are still on screen from the last answer, so the failed
              // refresh is said beside them rather than replacing them.
              <p role="alert" className="px-4 pb-2 text-body-sm text-destructive">
                Couldn't refresh properties: {getErrorMessage(variablesQuery.error)}
              </p>
            )}
            {activeVariables.length > 0 ? (
              <>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-8">
                        {/* Selection spans every variable matching the filter, not
                            just the page on screen — bulk edits are why a project
                            with a thousand variables opens this table at all. */}
                        {canWrite && (
                          <input
                            ref={selectAllRef}
                            type="checkbox"
                            aria-label={`Select all ${matchingVariables.length} matching properties`}
                            checked={allMatchingSelected}
                            onChange={() =>
                              allMatchingSelected
                                ? selection.clear()
                                : selection.selectAll(matchingVariables.map(v => v.id))
                            }
                          />
                        )}
                      </TableHead>
                      {/* Width hints, not fixed widths: `table-layout: auto` left
                          Description ~110px, so a 45-character sentence ran five
                          lines while the values columns — whose chips wrap for free —
                          held the slack. Variable is pinned too, because
                          its pills no longer wrap and would otherwise be squeezed
                          out. Doc/Observed values share whatever is left. */}
                      <TableHead className="w-[24%]">Property</TableHead>
                      {/* Below `md` these four fold into the Property cell
                          (VariablesTableRow), so a phone reads one column
                          instead of a desktop table scrolled under the pinned
                          actions. */}
                      {/* The events a scan has SEEN this variable in (its value
                          contexts), not every event whose template names it:
                          "Events" read as the latter and undercounted. */}
                      <TableHead className="hidden w-[13%] md:table-cell" title="Events a scan observed this property in">Observed in</TableHead>
                      <TableHead className="hidden w-[20%] md:table-cell">Description</TableHead>
                      <TableHead className="hidden md:table-cell">Documented values</TableHead>
                      <TableHead className="hidden md:table-cell">Observed values</TableHead>
                      {/* Pinned to the right edge, so on a phone the row actions
                          are on screen without discovering the sideways
                          scroll. */}
                      <TableHead className={cn(STICKY_ACTIONS_CLASS, 'w-24')}><span className="sr-only">Actions</span></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pageVariables.map((variable) => (
                      <VariablesTableRow
                        key={variable.id}
                        variable={variable}
                        // The schema's own spelling, lowercase mono, as meta
                        // fields and event fields show theirs.
                        typeLabel={variable.variable_type}
                        selected={selectedIds.has(variable.id)}
                        focused={variable.id === focusId}
                        rowRef={variable.id === focusId ? focusRef : undefined}
                        canWrite={canWrite}
                        onToggleSelect={selection.toggle}
                        onEdit={startEdit}
                        onExclude={handleExclude}
                        onDelete={handleDelete}
                        eventHref={eventHref}
                        detailHref={detailHref}
                        listedEventsHref={listedEventsHref}
                      />
                    ))}
                    {pageVariables.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={7} className="py-6 text-center text-body-sm text-fg-tertiary">
                          No properties match “{filterText}”.
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
                {pageCount > 1 && (
                  <div className="flex items-center justify-end gap-2 px-4 py-2">
                    <Button
                      type="button" variant="outline" size="sm"
                      aria-label="Previous page"
                      disabled={currentPage === 0}
                      onClick={() => goToPage(currentPage - 1)}
                    >
                      Previous
                    </Button>
                    <span className="text-body-sm text-fg-tertiary">Page {currentPage + 1} of {pageCount}</span>
                    <Button
                      type="button" variant="outline" size="sm"
                      aria-label="Next page"
                      disabled={currentPage >= pageCount - 1}
                      onClick={() => goToPage(currentPage + 1)}
                    >
                      Next
                    </Button>
                  </div>
                )}
              </>
            ) : usageFilter === 'unused' ? (
              // "No variables" would be a lie here — there are plenty, none of them
              // dead. Say which, since this is the answer the operator came for.
              <div className="px-4 py-8">
                <EmptyState
                  icon={VariableIcon}
                  title="Nothing to retire"
                  // Every reason the backend predicate can keep a row for. The
                  // first version named three of seven, so an operator staring at
                  // an empty list would have been told the wrong thing about why.
                  description="Every property here is kept by something: a field or meta value that names it, observed values, documented values, a value drift, a per-event override, an exclusion from scans, or an edit someone made."
                  action={showAllAction}
                />
              </div>
            ) : usageFilter === 'used' ? (
              <div className="px-4 py-8">
                <EmptyState
                  icon={VariableIcon}
                  title="No properties in use"
                  description="No property here is referenced by an event field value or carries observed values yet."
                  action={showAllAction}
                />
              </div>
            ) : excludedVariables.length > 0 ? (
              // "No variables" over a panel listing some was a contradiction on
              // one screen.
              <div className="px-4 py-8">
                <EmptyState
                  icon={VariableIcon}
                  title="Every property is excluded from scans"
                  description={`${countOf(excludedVariables.length, 'property is', 'properties are')} listed under “Excluded from scans” below. Restore one to put it back in this table.`}
                />
              </div>
            ) : (
              <div className="px-4 py-8">
                <EmptyState
                  icon={VariableIcon}
                  title="No properties yet"
                  description="Scans create them as they find value families, or define one to reuse across event field values."
                  action={canWrite ? (
                    <Button type="button" size="sm" onClick={() => setShowForm(true)}>
                      <Plus className="size-3.5" />Create your first property
                    </Button>
                  ) : undefined}
                />
              </div>
            )}
            {rowActionError && (
              <p role="alert" className="px-4 pb-3 text-body text-destructive">{rowActionError}</p>
            )}
          </>
        )}
      </Panel>

      {excludedVariables.length > 0 && (
        <Panel
          title="Excluded from scans"
          subtitle={`${countOf(excludedVariables.length, 'property', 'properties')} — scans will not re-create these`}
        >
          <ul className="divide-y">
            {excludedVariables.map(v => (
              <li
                key={v.id}
                // The same marking the table row carries, because the diff link
                // that brought the reviewer here neither knows nor cares which
                // of the two lists the variable ended up in.
                ref={v.id === focusId ? excludedFocusRef : undefined}
                data-focused={v.id === focusId || undefined}
                className={`flex items-center justify-between gap-2 px-4 py-2${v.id === focusId ? ' bg-primary/5 outline outline-1 outline-primary/40' : ''}`}
              >
                <div className="min-w-0">
                  <CodeToken>{`\${${v.name}}`}</CodeToken>
                  {(v.bindings ?? []).length > 0 && (
                    <span className="ml-2 truncate font-mono text-micro text-fg-tertiary">{(v.bindings ?? []).join(' · ')}</span>
                  )}
                </div>
                {canWrite && <div className="flex shrink-0 gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Restore property ${v.name}`}
                    disabled={excludeMut.isPending}
                    onClick={() => {
                      deleteMut.reset()
                      excludeMut.mutate({ id: v.id, excluded: false })
                    }}
                  >
                    <RotateCcw className="mr-1 h-3 w-3" aria-hidden="true" />Restore
                  </Button>
                  <IconButton variant="ghost" className="h-7 w-7 text-fg-tertiary hover:text-destructive" label={`Delete property ${v.name}`} onClick={() => handleDelete(v)}>
                    <Trash2 className="h-3 w-3" aria-hidden="true" />
                  </IconButton>
                </div>}
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {canWrite && <VariablesBulkBar
        selectedCount={selectedIds.size}
        isPending={bulkUpdateMut.isPending || bulkDeleteMut.isPending}
        error={bulkError}
        typeLabels={TYPE_LABELS}
        onSetType={handleBulkSetType}
        onSetDescription={description => {
          bulkDeleteMut.reset()
          return bulkUpdateMut.mutateAsync({ description })
        }}
        onAddValues={handleBulkAddValues}
        onDelete={handleBulkDelete}
        onClear={selection.clear}
      />}
    </PageContainer>
  )
}
