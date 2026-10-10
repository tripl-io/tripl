import { useId, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bookmark, ChevronRight, Download, GitBranch, GitMerge, History, Plus } from 'lucide-react'

import { planBranchesApi } from '@/api/planBranches'
import { planExportApi } from '@/api/planExport'
import { planRevisionsApi } from '@/api/planRevisions'
import { Chip } from '@/components/primitives/chip'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { OffsetPager } from '@/components/ui/offset-pager'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useOffsetPaging } from '@/hooks/useOffsetPaging'
import { displayUser, useUsersById } from '@/hooks/useUsersById'
import type {
  PlanDiff,
  PlanDiffEntityType,
  PlanDiffEntry,
  PlanDiffKind,
  PlanRevisionKind,
  PlanRevisionSummary,
} from '@/types'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import { formatDateTime, formatTimestamp } from '@/lib/datetime'
import { countOf } from '@/lib/plural'
import { KIND_META } from './branches/branchMeta'
import { PlanFieldChangeList } from './PlanFieldChangeList'
import { downloadJson, planSchemaFilename } from './planSchemaDownload'
import {
  planBranchesKey,
  planRevisionDiffKey,
  planRevisionsKey,
  projectPlanRevisionsKey,
} from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

// One page of revisions. It used to be the ONLY page: the list asked for 50 and
// ignored `total`, so anything older was unreachable.
const PAGE_SIZE = 50

/**
 * What wrote a revision comes from its stored `kind`: a snapshot
 * someone saved, the merge base captured when a branch opened, or the plan
 * right after a merge. It used to be parsed out of `summary`, which is free
 * text — a snapshot a user titled "Merged branch 'x'" read as a merge.
 */
const KIND_ROW: Record<PlanRevisionKind, { icon: typeof GitMerge; label: string }> = {
  merge: { icon: GitMerge, label: 'Merge' },
  branch_base: { icon: GitBranch, label: 'Branch opened' },
  snapshot: { icon: Bookmark, label: 'Snapshot' },
}

/**
 * The branch name to show for a branch revision. The live branch's name wins
 * (it follows a rename); a deleted branch — `branch_id` is null then — keeps
 * the name the backend wrote into the summary. Display only: nothing is
 * classified or linked from this text.
 */
function branchLabel(rev: PlanRevisionSummary, liveName: string | null): string | null {
  if (rev.kind === 'snapshot') return null
  if (liveName) return liveName
  return /'(.+)'$/.exec(rev.summary)?.[1] ?? null
}

export function HistoryTab({ slug }: { slug: string }) {
  const qc = useQueryClient()
  const [snapshotOpen, setSnapshotOpen] = useState(false)
  const [summaryText, setSummaryText] = useState('')
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  // null = follow the default: branch bases stay folded away unless the page
  // has nothing else to show.
  const [showBasesPicked, setShowBasesPicked] = useState<boolean | null>(null)
  const usersById = useUsersById()
  // A branch revision links to its branch's review, labelled with its live name.
  const branchesQuery = useQuery({
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug),
    enabled: !!slug,
    meta: SILENT_ERROR_META,
  })
  const branchNameById = useMemo(
    () => new Map((branchesQuery.data?.items ?? []).map((b) => [b.id, b.name])),
    [branchesQuery.data],
  )

  const listQuery = useQuery({
    // Under the ['planRevisions', slug] prefix, so a new snapshot still
    // refreshes every page.
    queryKey: planRevisionsKey(slug, offset),
    // One row past the page: the base the page's LAST revision diffs against.
    // Without it the 50th row found no `idx + 1` and called itself "the oldest
    // revision" whenever older ones existed.
    queryFn: () => planRevisionsApi.list(slug, { offset, limit: PAGE_SIZE + 1 }),
    enabled: !!slug,
    // The page being left stays on screen until the next one lands. Without
    // it the list blanked to a skeleton on every step, and the pager under it
    // read "Showing 51–50 of 0 revisions" for the length of the round trip.
    placeholderData: keepPreviousData,
    // Rendered in the list card, with a retry.
    meta: SILENT_ERROR_META,
  })
  const fetched = useMemo(() => listQuery.data?.items ?? [], [listQuery.data])
  const revisions = useMemo(() => fetched.slice(0, PAGE_SIZE), [fetched])
  const total = listQuery.data?.total ?? 0
  // Counted off the offset the VISIBLE rows came from; see useOffsetPaging.
  const paging = useOffsetPaging({ query: listQuery, offset, shown: revisions.length, total })
  const { rangeStart, rangeEnd, hasPrev: hasNewer, hasNext: hasOlder } = paging
  // Nothing recorded at all, as the server says it: not loading, not failed.
  const historyEmpty = listQuery.isSuccess && total === 0

  const baseCount = revisions.filter((r) => r.kind === 'branch_base').length
  const showBases = showBasesPicked ?? baseCount === revisions.length
  const shownRevisions = showBases
    ? revisions
    : revisions.filter((r) => r.kind !== 'branch_base')

  // Default the diff selection to the latest revision on the page once data
  // lands. A pick that is not among the fetched rows — made on the page that
  // was still on screen while the next one loaded — falls back the same way,
  // or the diff card would call it "the oldest revision" for want of a row
  // after it.
  const pickedOnPage =
    selectedRevisionId !== null && fetched.some((r) => r.id === selectedRevisionId)
      ? selectedRevisionId
      : null
  const effectiveSelected = pickedOnPage ?? shownRevisions[0]?.id ?? null
  const selectedRevision = fetched.find((r) => r.id === effectiveSelected) ?? null
  const compareTo = useMemo(() => {
    if (!effectiveSelected) return null
    const idx = fetched.findIndex((r) => r.id === effectiveSelected)
    if (idx < 0) return null
    // Compare against the next-older revision (i.e. idx + 1, since the list is
    // sorted newest-first) — for the page's last row that is the extra one.
    return fetched[idx + 1]?.id ?? null
  }, [effectiveSelected, fetched])

  const goToOffset = (next: number) => {
    setOffset(next)
    // A selection from the page being left would not be on the next one.
    setSelectedRevisionId(null)
  }

  const diffQuery = useQuery<PlanDiff>({
    queryKey: planRevisionDiffKey(slug, effectiveSelected, compareTo),
    queryFn: () => planRevisionsApi.diff(slug, effectiveSelected!, compareTo!),
    enabled: !!effectiveSelected && !!compareTo,
    // "Failed to load diff" is rendered in the diff card.
    meta: SILENT_ERROR_META,
  })

  const createMut = useMutation({
    mutationFn: (summary: string) => planRevisionsApi.create(slug, { summary }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: projectPlanRevisionsKey(slug) })
      setSnapshotOpen(false)
      setSummaryText('')
    },
  })

  // The plan as JSON Schema, one schema per live event (GH #262, F09). It
  // follows the branch picker like every other plan read, so a branch's
  // pending events can be validated before it merges.
  const activeBranchId = useActiveBranchId()
  const exportMut = useMutation({
    mutationFn: () => planExportApi.jsonSchema(slug, activeBranchId),
    // The file is named after the branch the bundle says it was read from.
    onSuccess: (bundle) => downloadJson(planSchemaFilename(slug, bundle.branch), bundle),
    // Reported under the header, next to the button that failed.
    meta: SILENT_ERROR_META,
  })

  return (
    <PageContainer className="space-y-4">
      {/* The shared page header, the same as Plan branches':
          no inline icon, one description size, actions on the right. */}
      {/* "Snapshot now" is secondary: revisions are recorded on their own
          when a branch merges or opens, and a manual checkpoint is the
          exception, with its reason in the dialog. */}
      <PageHeader
        eyebrow="Plan"
        title="Plan history"
        // No "Select a revision" over an empty history: there is none to select.
        description={
          historyEmpty
            ? 'Every merge, every branch opened and every snapshot you save.'
            : 'Every merge, every branch opened and every snapshot you save. Select a revision to see what changed since the one before.'
        }
        actions={
          <>
            {/* The schema bundle for validators and codegen outside tripl
                (`tripl export --format jsonschema` writes the same). */}
            <Button
              size="sm"
              variant="outline"
              onClick={() => exportMut.mutate()}
              disabled={exportMut.isPending}
            >
              <Download className="size-3.5" />
              {exportMut.isPending ? 'Exporting…' : 'Export JSON Schema'}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setSnapshotOpen(true)}
              disabled={createMut.isPending}
            >
              <Plus className="size-3.5" />
              Snapshot now
            </Button>
          </>
        }
      />
      {exportMut.isError && (
        <p role="alert" className="text-body-sm text-destructive">
          Couldn't export the plan as JSON Schema: {getErrorMessage(exportMut.error)}
        </p>
      )}

      {historyEmpty ? (
        // One empty state across both cards. The two-card layout used to say
        // "No revisions yet" on the left and "Pick a revision to view its diff"
        // on the right, with nothing to pick. Revisions come from branches —
        // a snapshot is the exception — so the way forward is a branch.
        <EmptyState
          icon={History}
          title="No revisions yet"
          description="A revision is recorded when a branch opens or merges, and when you save a snapshot. Each one shows what changed since the one before."
          action={
            <Button asChild size="sm" variant="outline">
              <Link to={projectPath(currentOrgSlug(), slug, '/branches')}>Open plan branches</Link>
            </Button>
          }
        />
      ) : (
      // 2:3 from `lg`, not 1:2. A revision's identity is its summary —
      // product-generated ones read "Base snapshot for branch '<name>'"
      // (~300px) — and at 1fr the list card was ~250px, clipping the branch
      // name mid-word while the diff card next to it held one empty-state
      // sentence in ~590px. Below `lg` one column, held to the screen by
      // `grid-cols-1` (minmax(0, 1fr)): the implicit `auto` track grew to the
      // revision meta line's nowrap width and pushed both cards past a phone's
      // edge, clipped mid-word with no ellipsis.
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <Card>
          <CardContent className="p-0">
            {listQuery.isError && listQuery.data !== undefined && (
              // A failed refresh keeps the list on screen.
              <p role="alert" className="px-3 py-2 text-body-sm text-destructive">
                Couldn't refresh plan history: {getErrorMessage(listQuery.error)}
              </p>
            )}
            {listQuery.isPending ? (
              <SectionSkeleton variant="list" rows={6} label="Loading plan history…" className="p-3" />
            ) : listQuery.isError && listQuery.data === undefined ? (
              // A failed load is not "No revisions yet".
              <div className="p-3">
                <ErrorState
                  compact
                  title="Couldn't load plan history"
                  error={listQuery.error}
                  onRetry={() => { void listQuery.refetch() }}
                  retryLabel="Retry"
                />
              </div>
            ) : revisions.length === 0 ? (
              // Only reachable with rows elsewhere — an empty history renders
              // the page-wide empty state instead — or with a failed refresh
              // of an empty page, whose error is said just above.
              <div className="p-4 text-body text-fg-tertiary">
                {total > 0 ? 'Nothing on this page.' : 'No revisions yet.'}
              </div>
            ) : (
              <>
                {baseCount > 0 && baseCount < revisions.length ? (
                  <div className="flex items-center justify-between gap-2 border-b px-3 py-2 text-caption text-fg-tertiary">
                    <span>
                      {showBases
                        ? 'Showing every revision'
                        : `Merges and snapshots · ${countOf(baseCount, 'branch opening', 'branch openings')} hidden`}
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      aria-pressed={showBases}
                      onClick={() => setShowBasesPicked(!showBases)}
                    >
                      {showBases ? 'Hide branch openings' : 'Show branch openings'}
                    </Button>
                  </div>
                ) : null}
                <ul className="divide-y">
                  {shownRevisions.map((rev) => (
                    <RevisionRow
                      key={rev.id}
                      rev={rev}
                      author={rev.created_by ? displayUser(usersById, rev.created_by) : null}
                      selected={rev.id === effectiveSelected}
                      onSelect={() => setSelectedRevisionId(rev.id)}
                    />
                  ))}
                </ul>
              </>
            )}
            {(hasNewer || hasOlder) && (
              <OffsetPager
                label="Plan history pages"
                className="border-t px-3 py-2"
                paging={paging}
                caption={
                  revisions.length === 0
                    ? `Past the end of ${countOf(total, 'revision', 'revisions')}.`
                    : `Showing ${rangeStart}–${rangeEnd} of ${countOf(total, 'revision', 'revisions')}.`
                }
                prevLabel="Newer"
                nextLabel="Older"
                onPrev={() => goToOffset(Math.max(0, offset - PAGE_SIZE))}
                onNext={() => goToOffset(offset + PAGE_SIZE)}
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="space-y-3">
            {selectedRevision ? (
              <RevisionHeader
                slug={slug}
                rev={selectedRevision}
                branchName={branchLabel(
                  selectedRevision,
                  selectedRevision.branch_id
                    ? (branchNameById.get(selectedRevision.branch_id) ?? null)
                    : null,
                )}
              />
            ) : null}
            <DiffPanel
              effectiveSelected={effectiveSelected}
              compareTo={compareTo}
              diff={diffQuery.data ?? null}
              isLoading={diffQuery.isLoading}
              isError={diffQuery.isError}
              error={diffQuery.error}
            />
          </CardContent>
        </Card>
      </div>
      )}

      <Dialog open={snapshotOpen} onOpenChange={setSnapshotOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Snapshot plan</DialogTitle>
            <DialogDescription>
              Save a named checkpoint of the plan as it is now, e.g. before a release.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-2">
            <Label htmlFor="snapshot-summary" optional>Summary</Label>
            <Input
              id="snapshot-summary"
              placeholder="e.g. Before launching v2 onboarding"
              value={summaryText}
              onChange={(e) => setSummaryText(e.target.value)}
            />
            {createMut.isError && (
              <p role="alert" className="text-body-sm text-destructive">
                Could not save the snapshot: {getErrorMessage(createMut.error)}
              </p>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSnapshotOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => createMut.mutate(summaryText)}
              disabled={createMut.isPending}
            >
              {createMut.isPending ? 'Snapshotting…' : 'Create snapshot'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageContainer>
  )
}

function RevisionRow({
  rev,
  author,
  selected,
  onSelect,
}: {
  rev: PlanRevisionSummary
  /** Who wrote it; null for a system revision with no author. */
  author: string | null
  selected: boolean
  onSelect: () => void
}) {
  const { kind } = rev
  const KindIcon = KIND_ROW[kind].icon
  const metaLine = [
    formatDateTime(rev.created_at),
    ...(author ? [`by ${author}`] : []),
    `${rev.entity_counts.event_types ?? 0} types`,
    `${rev.entity_counts.fields ?? 0} fields`,
    `${rev.entity_counts.events ?? 0} events`,
  ].join(' · ')

  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? 'true' : undefined}
        className={`flex w-full items-start gap-3 px-3 py-2 text-left transition-colors ${
          selected ? 'bg-muted/60' : 'hover:bg-muted/30'
        }`}
      >
        {/* A merge, a branch opening and a saved snapshot each have their own
            icon, so the list is scannable. */}
        <KindIcon
          className="mt-0.5 size-3.5 shrink-0"
          style={{ color: kind === 'merge' ? 'var(--accent)' : 'var(--fg-subtle)' }}
          aria-label={KIND_ROW[kind].label}
          role="img"
        />
        <div className="min-w-0 flex-1">
          {/* Wraps instead of truncating. Widening the card to 2fr was not
              enough on its own: at 1512px the row is ~355px and the
              product-generated summary "Base snapshot for branch '<name>'"
              still overran it, so one `truncate` line clipped to "Base snapshot
              for branch 'feature/checkout-f…" — losing the branch name, which
              is the only identity the row carries and the only thing on the
              page that names it. Two lines hold roughly 90
              characters; `break-words` keeps an unbroken branch name inside the
              card, and the tooltip stays as the fallback for a summary longer
              than that. */}
          <div
            className="line-clamp-2 break-words text-body-sm font-medium"
            title={rev.summary || undefined}
          >
            {rev.summary || <span className="text-fg-tertiary">(no summary)</span>}
          </div>
          {/* One `truncate` line rather than a wrapping one: as flowing text the
              metadata broke mid-list and left a dangling "·" as the last glyph
              of a line, which reads as a formatting fault. At
              ~10px the whole string is ~280px and fits; the ellipsis is the
              fallback, and `title` keeps it readable either way. */}
          <div className="truncate text-micro text-fg-tertiary tnum" title={metaLine}>
            {metaLine}
          </div>
        </div>
        {selected && <ChevronRight className="mt-1 size-3 text-fg-tertiary" aria-hidden="true" />}
      </button>
    </li>
  )
}

function DiffPanel({
  effectiveSelected,
  compareTo,
  diff,
  isLoading,
  isError,
  error,
}: {
  effectiveSelected: string | null
  compareTo: string | null
  diff: PlanDiff | null
  isLoading: boolean
  isError: boolean
  error: unknown
}) {
  if (!effectiveSelected) {
    return <p className="text-body text-fg-tertiary">Pick a revision to view its diff.</p>
  }
  if (!compareTo) {
    return (
      <p className="text-body text-fg-tertiary">
        This is the oldest revision, so there is nothing to compare it with. The next merge
        or snapshot will show what changed.
      </p>
    )
  }
  if (isLoading) {
    return (
      <div role="status" className="space-y-2">
        <span className="sr-only">Loading changes…</span>
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    )
  }
  // Through ErrorState, so a 401 under the session-expired dialog reads as
  // paused rather than as red text.
  if (isError || !diff) {
    return <ErrorState compact headingLevel={3} title="Failed to load diff" error={error} />
  }
  if (diff.entries.length === 0) {
    return (
      <p className="text-body text-fg-tertiary">
        No schema changes between these two revisions.
      </p>
    )
  }
  return <HistoryDiff diff={diff} />
}

/** The branch review's order and words for the kinds. */
const KIND_ORDER: PlanDiffKind[] = ['changed', 'added', 'removed']

const GROUP_ORDER: PlanDiffEntityType[] = [
  'event_type',
  'event',
  'field_definition',
  'variable',
  'meta_field',
  'relation',
]

const GROUP_LABEL: Record<PlanDiffEntityType, string> = {
  event_type: 'Event types',
  event: 'Events',
  field_definition: 'Fields',
  variable: 'Properties',
  meta_field: 'Meta fields',
  relation: 'Relations',
}

/** Rows start open only when there are few of them; a revision diff can hold
 * hundreds of entries. */
const OPEN_ALL_BELOW = 6

/**
 * A revision's diff: grouped by entity type under sticky subheaders, one line
 * per entry that opens to its field changes, and the kind counts as toggles
 * that filter the list. It used to be one bordered card per entry, always
 * expanded — 187 of them for one diff.
 */
function HistoryDiff({ diff }: { diff: PlanDiff }) {
  const [kinds, setKinds] = useState<ReadonlySet<PlanDiffKind>>(() => new Set(KIND_ORDER))
  const counts: Record<PlanDiffKind, number> = {
    added: diff.summary.added ?? 0,
    removed: diff.summary.removed ?? 0,
    changed: diff.summary.changed ?? 0,
  }
  const shown = diff.entries.filter((entry) => kinds.has(entry.kind))
  const groups = GROUP_ORDER.map((type) => ({
    type,
    entries: shown.filter((entry) => entry.entity_type === type),
  })).filter((group) => group.entries.length > 0)
  // Anything the fixed order does not know still shows, last.
  const known = new Set<string>(GROUP_ORDER)
  const other = shown.filter((entry) => !known.has(entry.entity_type))
  const openAll = diff.entries.length < OPEN_ALL_BELOW

  const toggleKind = (kind: PlanDiffKind) =>
    setKinds((prev) => {
      const next = new Set(prev)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      // Never filter everything away: turning off the last kind shows all.
      return next.size === 0 ? new Set(KIND_ORDER) : next
    })

  return (
    <>
      <div role="group" aria-label="Filter changes by kind" className="flex flex-wrap items-center gap-1.5 text-body-sm">
        {KIND_ORDER.map((kind) => (
          <button
            key={kind}
            type="button"
            aria-pressed={kinds.has(kind)}
            disabled={counts[kind] === 0}
            onClick={() => toggleKind(kind)}
            className="rounded-full disabled:opacity-60"
          >
            <Chip
              tone={kinds.has(kind) && counts[kind] > 0 ? KIND_META[kind].tone : 'neutral'}
              variant={kinds.has(kind) ? 'soft' : 'outline'}
              size="sm"
            >
              {KIND_META[kind].sym}
              {counts[kind]} {KIND_META[kind].label.toLowerCase()}
            </Chip>
          </button>
        ))}
        <span className="text-fg-tertiary">
          {countOf(diff.entries.length, 'entry', 'entries')}
        </span>
      </div>
      <div className="max-h-[70vh] overflow-y-auto">
        {[...groups, ...(other.length > 0 ? [{ type: null, entries: other }] : [])].map((group) => (
          <section key={group.type ?? 'other'} aria-label={group.type ? GROUP_LABEL[group.type] : 'Other'}>
            <h2 className="sticky top-0 z-10 border-b bg-surface py-1.5 micro-label text-fg-tertiary">
              {group.type ? GROUP_LABEL[group.type] : 'Other'} · {group.entries.length}
            </h2>
            <ul>
              {group.entries.map((entry, idx) => (
                <HistoryEntryRow
                  key={`${entry.entity_type}:${entry.parent ?? ''}:${entry.name}:${idx}`}
                  entry={entry}
                  defaultOpen={openAll}
                />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </>
  )
}

function HistoryEntryRow({ entry, defaultOpen }: { entry: PlanDiffEntry; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const detailId = useId()
  const meta = KIND_META[entry.kind]
  const hasDetail = (entry.field_changes?.length ?? 0) > 0 || (entry.changes?.length ?? 0) > 0
  return (
    <li className="border-b last:border-b-0 border-border-subtle">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={hasDetail ? open : undefined}
        aria-controls={hasDetail && open ? detailId : undefined}
        disabled={!hasDetail}
        className="flex w-full min-w-0 items-center gap-2 py-2 text-left text-body-sm hover:bg-[var(--surface-hover)] disabled:cursor-default disabled:hover:bg-transparent"
      >
        <ChevronRight
          className="size-3 shrink-0 transition-transform"
          style={{
            color: 'var(--fg-faint)',
            transform: open ? 'rotate(90deg)' : 'none',
            visibility: hasDetail ? 'visible' : 'hidden',
          }}
          aria-hidden="true"
        />
        <span className="w-3 shrink-0 text-center font-semibold" style={{ color: `var(--${meta.tone})` }} aria-hidden="true">
          {meta.sym}
        </span>
        <span className="mono min-w-0 truncate">
          {entry.parent ? `${entry.parent} / ` : ''}
          {entry.name}
        </span>
        <span className="flex-1" />
        {/* The branch review's words for the same kinds — history used to
            say "changed" where the review says "Modified". */}
        <Chip tone={meta.tone} size="xs">
          {meta.label}
        </Chip>
      </button>
      {open && hasDetail ? (
        <div id={detailId} className="pb-2 pl-7">
          {/* Before and after, as the branch review shows them. The bare field
              names are the fallback for an entry that carries no structured
              changes (a snapshot older than their capture). */}
          {(entry.field_changes?.length ?? 0) > 0 ? (
            <PlanFieldChangeList changes={entry.field_changes ?? []} />
          ) : (
            <ul className="space-y-0.5 text-caption text-fg-tertiary">
              {(entry.changes ?? []).map((change) => (
                <li key={change} className="font-mono">{change}</li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </li>
  )
}

/** The selected revision: what wrote it, who, and the branch it came from
 * as a link to that branch's review. */
function RevisionHeader({
  slug,
  rev,
  branchName: branch,
}: {
  slug: string
  rev: PlanRevisionSummary
  /** Display name of the branch behind a merge or branch opening. */
  branchName: string | null
}) {
  const { kind, branch_id: branchId } = rev
  return (
    <div className="flex flex-wrap items-center gap-2 border-b pb-2 text-body-sm">
      <Chip variant="outline" size="xs">
        {KIND_ROW[kind].label}
      </Chip>
      {branch ? (
        branchId ? (
          <Link
            to={projectPath(currentOrgSlug(), slug, `/branches/${branchId}`)}
            className="mono font-medium hover:underline text-accent"
          >
            {branch}
          </Link>
        ) : (
          <span className="mono font-medium">{branch}</span>
        )
      ) : (
        <span className="font-medium">{rev.summary || 'Snapshot'}</span>
      )}
      {/* The browser's zone, named on hover: on screen it is a bare time. */}
      <span className="text-caption text-fg-tertiary" title={formatTimestamp(rev.created_at, { zone: true })}>
        {formatDateTime(rev.created_at)}
      </span>
    </div>
  )
}
