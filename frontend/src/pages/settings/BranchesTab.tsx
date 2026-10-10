import { useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal, Plus, Settings2, Ticket } from 'lucide-react'
import { toast } from 'sonner'

import { planBranchesApi } from '@/api/planBranches'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useBranchContext } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { useUsersById } from '@/hooks/useUsersById'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { planBranchCountsKey, planBranchDiffKey, planBranchesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { PlanBranchList, PlanBranchSummary } from '@/types'
import { DIFF_STALE_MS, rowBadgeCounts } from './branchDiffFanout'
import { TrackerConfigDialog } from './TrackerConfigDialog'
import { BranchList } from './branches/BranchList'
import { CreateBranchDialog, MergePolicyDialog } from './branches/BranchDialogs'
import type { DiffLoad } from './branches/branchDiffModel'
import { BranchDetail } from './branches/FeatureBranchDetail'
import { invalidateBranchCounts } from './branches/branchQueryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * Plan branches: the list, the selected branch's review, and the merge policy.
 *
 * The pieces live in ./branches/ — list, detail, change rows, conflicts,
 * dialogs, and the pure diff model with its own unit tests. This file
 * owns what they share: the selection (from the URL), the branch list and the
 * selected branch's diff.
 *
 * The selected branch lives in the URL (`/p/:slug/branches/:branchId`),
 * not in component state, so a review can be linked to and shared.
 */
export function BranchesTab({ slug, branchId }: { slug: string; branchId?: string }) {
  const qc = useQueryClient()
  // Every branch write is EditorUserDep; a viewer follows the review.
  const canWrite = useCanWriteProject()
  const navigate = useNavigate()
  const { confirm, dialog } = useConfirm()
  const { branchId: activeBranchId, setBranchId } = useBranchContext()
  // `?new=1` opens the create dialog on arrival: the branch switcher's "New
  // branch from main" lands here. Followed on change too, for
  // a switcher click made while this page is already open.
  const [searchParams, setSearchParams] = useSearchParams()
  const wantsNew = searchParams.get('new') === '1'
  const [createOpen, setCreateOpenState] = useState(() => wantsNew && canWrite)
  const [seenNew, setSeenNew] = useState(wantsNew)
  if (seenNew !== wantsNew) {
    setSeenNew(wantsNew)
    if (wantsNew && canWrite) setCreateOpenState(true)
  }
  const setCreateOpen = (open: boolean) => {
    setCreateOpenState(open)
    if (!open && searchParams.has('new')) {
      const next = new URLSearchParams(searchParams)
      next.delete('new')
      setSearchParams(next, { replace: true })
    }
  }
  // On by default: the next thing after creating a branch is working on it,
  // and it used to leave you on main with no hint how to start.
  const [switchAfterCreate, setSwitchAfterCreate] = useState(true)
  const detailRef = useRef<HTMLDivElement>(null)
  const [policyOpen, setPolicyOpen] = useState(false)
  const [trackerOpen, setTrackerOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createDescription, setCreateDescription] = useState('')
  const usersById = useUsersById()

  // The plain list the switcher shares, so the rows render at once...
  const { data, isLoading } = useQuery({
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug),
  })
  // ...and the counted one for the badges, which costs one plan snapshot per
  // open branch and so arrives later. It replaced one 2-3.5 s diff request per
  // row. No badge beats an error toast when it fails.
  const countsQuery = useQuery({
    queryKey: planBranchCountsKey(slug),
    queryFn: () => planBranchesApi.list(slug, { include_diff_counts: true }),
    staleTime: DIFF_STALE_MS,
    meta: SILENT_ERROR_META,
  })

  const selectBranch = (branch: PlanBranchSummary) => {
    navigate(projectPath(currentOrgSlug(), slug, `/branches/${branch.id}`))
    // Below `lg` the list stacks above the review, so a tap changed content
    // off-screen: bring the review into view.
    if (typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 1023px)').matches) {
      requestAnimationFrame(() => detailRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' }))
    }
  }

  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      planBranchesApi.create(slug, { name: createName, description: createDescription }),
    onSuccess: (branch) => {
      // In the list before the route points at it, or the pane would call a
      // branch that was just created "not found" until the refetch lands.
      qc.setQueryData<PlanBranchList>(planBranchesKey(slug), (old) =>
        old ? { items: [...old.items, branch], total: old.total + 1 } : old,
      )
      void qc.invalidateQueries({ queryKey: planBranchesKey(slug) })
      invalidateBranchCounts(qc, slug)
      setCreateOpen(false)
      setCreateName('')
      setCreateDescription('')
      selectBranch(branch)
      if (switchAfterCreate) {
        // The address being left keeps its own branch; the destination (the
        // branch's review) names none, and the provider keeps this choice.
        setBranchId(branch.id, { updateUrl: false })
        toast.success(`Switched to ${branch.name}. Changes you make now stay on this branch until it merges.`)
      } else {
        toast.success(`Branch ${branch.name} created`)
      }
    },
  })

  const items = useMemo(() => data?.items ?? [], [data])
  const mainBranch = items.find((b) => b.kind === 'main')
  // An unknown id in the URL (a deleted or mistyped branch) is said out loud
  // rather than silently swapped for main, which changed the pane under the
  // user with no word why.
  const selected = branchId
    ? (items.find((b) => b.id === branchId) ?? null)
    : (mainBranch ?? items[0] ?? null)
  const notFound = !!branchId && !!data && selected === null

  const selectedDiffQuery = useQuery({
    queryKey: planBranchDiffKey(slug, selected?.id),
    queryFn: () => planBranchesApi.diff(slug, selected!.id),
    enabled: !!selected && selected.kind !== 'main',
    staleTime: DIFF_STALE_MS,
  })
  const selectedDiff = selectedDiffQuery.data
  // The detail pane needs to know the difference between "no diff yet" and "an
  // empty diff": rendering counts from `undefined` drew "+0 ~0 −0 · No changes
  // in this branch" for the 1.3-8.5 s the request takes on production, with
  // Approve live under it. `status` alone carries that — an
  // invalidation after a revert keeps the data and stays `success`, so the
  // loading state shows only when there really is nothing to show.
  const selectedDiffLoad: DiffLoad = {
    status: selectedDiffQuery.status,
    error: selectedDiffQuery.error,
    retry: () => void selectedDiffQuery.refetch(),
  }

  // Until the counted list lands (or if it fails), the plain rows stand in:
  // they carry no counts, so only the selected branch — whose diff is loaded
  // anyway — gets a badge.
  const countsByBranch = useMemo(
    () => rowBadgeCounts(countsQuery.data?.items ?? items, selected?.id ?? null, selectedDiff),
    [countsQuery.data, items, selected?.id, selectedDiff],
  )

  return (
    <>
      {dialog}
      <PageContainer className="space-y-[18px]">
        {/* The shared page header, the same as Plan history's.
            Its actions wrap on a phone: the three buttons are ~400px of
            content, and a non-wrapping header pushed "New branch" off-screen
            at 375px. */}
        <PageHeader
          eyebrow="Plan"
          title="Plan branches"
          description="Propose and review changes to the tracking plan in isolation, then merge to main — version control for your schema."
          actions={
          <>
            {/* The two settings buttons fold into one menu below `sm`, leaving
                New branch — the page's primary action — on the row. */}
            <Button
              size="sm"
              variant="outline"
              className="hidden sm:inline-flex"
              onClick={() => setPolicyOpen(true)}
            >
              <Settings2 className="size-3.5" />
              Merge policy
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="hidden sm:inline-flex"
              onClick={() => setTrackerOpen(true)}
            >
              <Ticket className="size-3.5" />
              Implementation tracker
            </Button>
            {/* Non-modal: a modal menu would still be tearing down its focus
                trap when the dialog it opens mounts. */}
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button
                  size="sm"
                  variant="outline"
                  className="sm:hidden"
                  aria-label="Branch settings"
                >
                  <MoreHorizontal className="size-3.5" aria-hidden="true" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => setPolicyOpen(true)}>
                  <Settings2 className="size-3.5" aria-hidden="true" />
                  Merge policy
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setTrackerOpen(true)}>
                  <Ticket className="size-3.5" aria-hidden="true" />
                  Implementation tracker
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            {canWrite && (
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus className="size-3.5" />
                New branch
              </Button>
            )}
          </>
          }
        />
        {!canWrite && <ReadOnlyNotice />}

        {isLoading ? (
          <SectionSkeleton variant="cards" label="Loading branches…" />
        ) : (
          // `minmax(0,1fr)`, not `1fr`: a bare `1fr` track is `minmax(auto,1fr)`,
          // so its MINIMUM is the detail column's min-content width and the
          // track grows past the viewport rather than the content wrapping. One
          // long event description then widened the whole page, which pushed the
          // panel header's right edge — and the Merge button on it — off-screen.
          // Making the header wrap does not help when the page itself is wider
          // than the window.
          <div className="grid grid-cols-1 items-start gap-3 lg:grid-cols-[300px_minmax(0,1fr)]">
            <BranchList
              items={items}
              selectedId={selected?.id ?? null}
              activeBranchId={activeBranchId}
              countsByBranch={countsByBranch}
              usersById={usersById}
              onSelect={selectBranch}
            />
            <div ref={detailRef} className="min-w-0 scroll-mt-4">
              <BranchDetail
                slug={slug}
                branch={selected}
                notFound={notFound}
                diff={selectedDiff}
                diffLoad={selectedDiffLoad}
                confirm={confirm}
                branches={items}
                onNewBranch={canWrite ? () => setCreateOpen(true) : undefined}
              />
            </div>
          </div>
        )}
      </PageContainer>

      <MergePolicyDialog slug={slug} open={policyOpen} onOpenChange={setPolicyOpen} />

      <TrackerConfigDialog slug={slug} open={trackerOpen} onOpenChange={setTrackerOpen} />

      <CreateBranchDialog
        open={createOpen}
        name={createName}
        description={createDescription}
        pending={createMut.isPending}
        error={createMut.isError ? getErrorMessage(createMut.error) : null}
        existingNames={items.map((b) => b.name)}
        switchAfterCreate={switchAfterCreate}
        onSwitchAfterCreate={setSwitchAfterCreate}
        onName={setCreateName}
        onDescription={setCreateDescription}
        onOpenChange={setCreateOpen}
        onSubmit={() => createMut.mutate()}
      />
    </>
  )
}
