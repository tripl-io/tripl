import { useEffect, useId, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowUpRight,
  Check,
  GitCompare,
  GitMerge,
  History,
  Info,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { toast } from 'sonner'

import { branchSettingsApi } from '@/api/branchSettings'
import { metaFieldsApi } from '@/api/metaFields'
import { planBranchesApi } from '@/api/planBranches'
import { useAuth } from '@/components/auth-context'
import { usePageTitle } from '@/components/shell-chrome-context'
import { EmptyState } from '@/components/empty-state'
import { Chip, type ChipTone } from '@/components/primitives/chip'
import { Panel } from '@/components/settings/kit'
import { DisabledReason, EntityNotFound, disabledReasonAria } from '@/components/states'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Skeleton } from '@/components/ui/skeleton'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { useBranchContext, useBranchLinkProps } from '@/hooks/useBranch'
import type { useConfirm } from '@/hooks/useConfirm'
import { displayUser, useUsersById } from '@/hooks/useUsersById'
import { branchTicket } from '@/lib/branchTicket'
import { formatRelativeTime } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { countOf } from '@/lib/plural'
import {
  branchSettingsKey,
  planBranchConflictsKey,
  planBranchDetailKey,
  planBranchDiffKey,
  planBranchesKey,
  projectMetaFieldsKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type {
  PlanBranchApproval,
  PlanBranchDiffSummary,
  PlanBranchStatus,
  PlanBranchSummary,
  PlanBranchTransitionAction,
  PlanDiffEntry,
} from '@/types'
import {
  type ConfirmPrompt,
  type DiffLoad,
  INCOMPLETE_BASE_MESSAGE,
  behindNote,
  describeBranchActionError,
  diffView,
  entryKey,
  entryRevertPrompt,
  entryRowKey,
  fieldRevertPrompt,
  isConflictRefusal,
  mergePrompt,
  pairedDiffCounts,
  revertOutcome,
} from './branchDiffModel'
import {
  ACTION_LABEL,
  ALLOWED_TRANSITIONS,
  DIFF_VERDICTS,
  RENAMED_META,
  STATUS_LABEL,
  STATUS_TONE,
  branchAuthor,
  isLandedBranch,
} from './branchMeta'
import {
  invalidateBranchCounts,
  invalidateBranchPlan,
  invalidateBranchReview,
  invalidateMainPlan,
} from './branchQueryKeys'
import { BranchReviewSummary, type ReviewerPickerIntent } from './BranchReviewers'
import { CommentsPanel, ImplementationTicketsPanel } from './BranchSidePanels'
import { ChangeRow, HousekeepingFold } from './ChangeRow'
import { ConflictsPanel } from './ConflictsPanel'
import { BranchImpactPanel } from './BranchImpactPanel'
import { UpdateFromMainDialog } from './UpdateFromMainDialog'

type Confirm = ReturnType<typeof useConfirm>['confirm']

interface BranchDetailProps {
  slug: string
  branch: PlanBranchSummary | null
  /** The route named a branch the list does not have — deleted, or never
   * existed. Said out loud rather than silently showing main (PLAN-9). */
  notFound: boolean
  diff: PlanBranchDiffSummary | undefined
  diffLoad: DiffLoad
  confirm: Confirm
  /** Every branch of the project, for main's pane: what merged last and what
   * waits for review. */
  branches: PlanBranchSummary[]
  /** Opens the New branch dialog; omitted for a viewer. */
  onNewBranch?: () => void
}

export function BranchDetail({
  slug,
  branch,
  notFound,
  diff,
  diffLoad,
  confirm,
  branches,
  onNewBranch,
}: BranchDetailProps) {
  if (notFound) {
    // The shared not-found state (#237 SH-33): no red, no retry, one way back.
    return (
      <EntityNotFound
        title="Branch not found"
        description="This branch no longer exists: it may have been deleted."
        back={{ to: `/p/${slug}/branches`, label: 'Back to main' }}
        className="rounded-card border border-border bg-surface"
      />
    )
  }

  if (!branch) {
    return (
      <Panel title="Branch">
        <p className="px-4 py-7 text-center text-body-sm text-fg-tertiary">
          Select a branch to review its diff.
        </p>
      </Panel>
    )
  }

  if (branch.kind === 'main') {
    return <MainBranchPane slug={slug} main={branch} branches={branches} onNewBranch={onNewBranch} />
  }

  // Keyed by branch so mutation error state doesn't leak across selections.
  return (
    <FeatureBranchDetail
      key={branch.id}
      slug={slug}
      branch={branch}
      diff={diff}
      diffLoad={diffLoad}
      confirm={confirm}
    />
  )
}

interface FeatureBranchDetailProps {
  slug: string
  branch: PlanBranchSummary
  diff: PlanBranchDiffSummary | undefined
  diffLoad: DiffLoad
  confirm: Confirm
}

function FeatureBranchDetail({ slug, branch, diff, diffLoad, confirm }: FeatureBranchDetailProps) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const canWrite = useCanWriteProject()
  const { user } = useAuth()
  const usersById = useUsersById()
  // The top bar's last crumb: "Plan › Plan branches › <name>" (PL-17).
  usePageTitle(branch.name)
  const branchLink = useBranchLinkProps()
  const branchCtx = useBranchContext()
  // A branch that is merged, closed or gone can no longer be worked in, so the
  // shell must not keep sending every request to it (SHELL-18).
  const leaveEndedBranch = () => {
    if (branchCtx.branchId === branch.id) branchCtx.setBranchId(null)
  }
  // When this session merged the branch — the ticket panel waits for the
  // tracker ticket the merge worker writes a moment later (PLAN-10).
  const [mergedAt, setMergedAt] = useState<number | null>(null)
  // The reviewer picker in the review summary. Lifted here so "Submit for
  // review" on a branch with nobody assigned opens it instead of sending the
  // branch to no one (JR-14).
  const [reviewerPicker, setReviewerPicker] = useState<ReviewerPickerIntent>(null)
  // "Update from main" (PL-8): opened from the behind note and, when the merge
  // would refuse, from the merge button.
  const [updateOpen, setUpdateOpen] = useState(false)
  // The ticket a branch is named after, linked through the meta field that
  // links event values to the tracker (tripl-kjhi.14). Main's fields: the
  // template is project-wide and a branch copy carries the same one.
  const metaFieldsQuery = useQuery({
    queryKey: projectMetaFieldsKey(slug),
    queryFn: () => metaFieldsApi.list(slug),
  })
  const ticket = branchTicket(branch.name, metaFieldsQuery.data ?? [])
  const { notifyStepCompleted } = useDemoScenarioActions()

  // Opening the seeded branch's detail completes open-branch, whether the user
  // clicked the list row or followed a deep link. Inert outside the demo's
  // branches chapter — the reducer drops everything but the current step.
  useEffect(() => {
    if (branch.name === SCENARIO_SEEDED.branchName) {
      notifyStepCompleted('branches/open-branch')
    }
  }, [branch.id, branch.name, notifyStepCompleted])

  // Approvals and reviewers live on the detail response; the required quota
  // on the project's merge policy. Together they drive the "Approvals n/N" chip.
  const { data: detail } = useQuery({
    queryKey: planBranchDetailKey(slug, branch.id),
    queryFn: () => planBranchesApi.get(slug, branch.id),
  })
  const { data: policy } = useQuery({
    queryKey: branchSettingsKey(slug),
    queryFn: () => branchSettingsApi.get(slug),
  })
  // The Conflicts panel's own query (same key, so no second request): the
  // "main has moved on" note says whether that actually touches this branch
  // (PL-8).
  const branchOpen = branch.status !== 'merged' && branch.status !== 'closed'
  const { data: conflicts } = useQuery({
    queryKey: planBranchConflictsKey(slug, branch.id),
    queryFn: () => planBranchesApi.getConflicts(slug, branch.id),
    enabled: branchOpen,
  })
  const unresolvedConflicts = conflicts?.unresolved_count ?? 0
  const reasonIdBase = useId()

  // One mutation for transitions AND merge: each new action replaces the
  // previous error state, so a stale merge failure can't outlive a later
  // successful transition (or mask its error).
  const actionMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (action: PlanBranchTransitionAction | 'merge') =>
      action === 'merge'
        ? planBranchesApi.merge(slug, branch.id)
        : planBranchesApi.transition(slug, branch.id, action),
    onSuccess: (_data, action) => {
      // An approval counts as review feedback, exactly like a posted comment.
      if (action === 'approve') notifyStepCompleted('branches/comment')
      // Submitting used to change only a chip; say who it went to, or that
      // nobody will know to look yet (JR-14).
      if (action === 'submit') {
        const names = (detail?.reviewers ?? []).map((r) => displayUser(usersById, r.user_id))
        toast.success(
          names.length > 0
            ? `Sent for review to ${names.join(', ')}`
            : 'Sent for review. Add a reviewer so someone knows to look.',
        )
      }
      if (action === 'merge' || action === 'close') leaveEndedBranch()
      invalidateBranchReview(qc, slug, branch.id)
      // A status change moves no count, and the counted list is a snapshot
      // per open branch: only a merge (main moved, so every "behind" may
      // change) or a reopen (the branch rejoins the counted set) pays for it.
      if (action === 'merge' || action === 'reopen') invalidateBranchCounts(qc, slug)
      if (action === 'merge') {
        setMergedAt(Date.now())
        invalidateMainPlan(qc, slug)
      }
    },
    onError: (error) => {
      // "Resolve the field conflicts below" has to find them below: the panel
      // may be holding an empty answer from before main moved (PLAN-4).
      if (isConflictRefusal(error)) {
        void qc.invalidateQueries({ queryKey: planBranchConflictsKey(slug, branch.id) })
      }
    },
  })

  const deleteMut = useMutation({
    // Rendered in the panel below the actions (PLAN-9).
    meta: SILENT_ERROR_META,
    mutationFn: () => planBranchesApi.delete(slug, branch.id),
    onSuccess: () => {
      leaveEndedBranch()
      toast.success(`Branch “${branch.name}” deleted`)
      // Leave the deleted id's URL before the list refetches, so the pane
      // does not flash "Branch not found" for a branch the user just removed.
      navigate(`/p/${slug}/branches`)
      qc.removeQueries({ queryKey: planBranchDetailKey(slug, branch.id) })
      qc.removeQueries({ queryKey: planBranchDiffKey(slug, branch.id) })
      return qc.invalidateQueries({ queryKey: planBranchesKey(slug) })
    },
  })

  // Undo one diff entry — the whole entity, or one field of it — back to the
  // state the plan was in when the branch was opened.
  const revertMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ entry, field }: { entry: PlanDiffEntry; field?: string }) =>
      planBranchesApi.revert(slug, branch.id, {
        entity_type: entry.entity_type,
        name: entry.name,
        parent: entry.parent,
        field: field ?? null,
        entity_id: entry.entity_id ?? null,
      }),
    onSuccess: (nextDiff) => {
      // The endpoint answers with the resulting diff; refetching it instead
      // cost a second multi-second request after every revert (PLAN-16).
      qc.setQueryData(planBranchDiffKey(slug, branch.id), nextDiff)
      void qc.invalidateQueries({ queryKey: planBranchesKey(slug) })
      void qc.invalidateQueries({ queryKey: planBranchDetailKey(slug, branch.id) })
      void qc.invalidateQueries({ queryKey: planBranchConflictsKey(slug, branch.id) })
      invalidateBranchPlan(qc, slug, branch.id)
      invalidateBranchCounts(qc, slug)
    },
  })

  const view = useMemo(() => diffView(diff), [diff])
  const { visibleEntries, housekeepingEntries, renamedTo, renamedEntityId, removedVariables } =
    view
  const entries = useMemo(() => diff?.entries ?? [], [diff])
  // Removals the revert endpoint would refuse as ambiguous renames: the row
  // says so and offers no revert, instead of a dialog offering "Try anyway"
  // (PLAN-18).
  const revertBlockedBy = useMemo(() => {
    const blocked = new Map<string, PlanDiffEntry[]>()
    for (const entry of visibleEntries) {
      const outcome = revertOutcome(entries, entry)
      if (outcome.kind === 'ambiguous') blocked.set(entryRowKey(entry), outcome.among)
    }
    return blocked
  }, [entries, visibleEntries])

  const handleRevert = async (entry: PlanDiffEntry, field?: string) => {
    // The dialog describes what the BUTTON does, so it asks `revertOutcome` —
    // the branch-only question the revert endpoint asks — and not `renames`,
    // which is the merge's pairing and consults main. The two genuinely
    // disagree on a branch rename a->b that main independently grew its own b:
    // the merge refuses to pair it, the revert renames the branch's b back to a
    // regardless, and the dialog was reading the merge's "no" as a promise to
    // restore a deletion that the button never performs (tripl-amnn). The row's
    // chip and label are left reading `renames`, because those describe the
    // merge.
    let prompt: ConfirmPrompt
    if (field) {
      prompt = fieldRevertPrompt(entry, field)
    } else {
      const outcome = revertOutcome(entries, entry)
      // The row offers no revert for this one; see `revertBlockedBy`.
      if (outcome.kind === 'ambiguous') return
      prompt = entryRevertPrompt(entry, outcome)
    }
    const ok = await confirm(prompt)
    if (ok) revertMut.mutate({ entry, field })
  }

  const handleDelete = async () => {
    const ok = await confirm({
      title: 'Delete branch',
      message: `Delete branch "${branch.name}"? Its working copy of the plan, its review and its comments are discarded. This cannot be undone.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate()
  }

  const behind = diff?.behind_base === true
  // Through the same paired view `visibleEntries` renders and the list row's
  // ahead badge counts, so the strip cannot contradict the rows under it.
  const counts = pairedDiffCounts(diff)

  // Every merge asks first, with what is about to land (PLAN-8).
  const handleMerge = async () => {
    const ok = await confirm(
      mergePrompt(
        counts,
        removedVariables,
        behind,
        visibleEntries.map((entry) => entry.name),
        unresolvedConflicts,
      ),
    )
    if (ok) actionMut.mutate('merge')
  }

  const handleAction = async (action: PlanBranchTransitionAction) => {
    if (action === 'close') {
      const ok = await confirm({
        title: 'Close branch',
        message: `Close "${branch.name}" without merging? Its changes stay on the branch and it can be reopened later.`,
        confirmLabel: 'Close branch',
        variant: 'primary',
      })
      if (!ok) return
    }
    // Nobody assigned yet: ask who should review first, when there is someone
    // other than you to ask. The picker then submits ("Add and submit") or
    // lets the author send it anyway.
    if (
      action === 'submit' &&
      (detail?.reviewers.length ?? 0) === 0 &&
      [...usersById.keys()].some((id) => id !== user?.id)
    ) {
      setReviewerPicker('submit')
      return
    }
    actionMut.mutate(action)
  }

  // Mirror the backend gate: distinct non-null approvers, minus the author
  // when self-approval is blocked — a raw row count can show a green quota
  // that the merge endpoint would still reject.
  //
  // `stale` is the half that used to be missing, and it is the half that made
  // this screen lie: an approval the branch has since moved past does not count
  // (plan_branch_merge_service._load_fresh_approver_ids), so a branch with one
  // outdated approval rendered a green "Approvals 1/1" and then failed to merge
  // reporting current=0. Anyone reading the chip concluded the Approve button
  // was broken, because from the outside it was indistinguishable from one.
  const countedApprovals = (detail?.approvals ?? []).filter(
    (a): a is PlanBranchApproval & { user_id: string } =>
      a.user_id !== null && !(policy?.block_self_approval && a.user_id === branch.created_by),
  )
  const approvalsCount = new Set(countedApprovals.filter((a) => !a.stale).map((a) => a.user_id))
    .size
  const staleApprovals = new Set(countedApprovals.filter((a) => a.stale).map((a) => a.user_id)).size
  const requiredApprovals = policy?.min_approvals ?? 0
  // The author's own Approve is refused by the backend under this policy, so
  // it is not offered as a live button (PLAN-12).
  const selfApprovalBlocked =
    policy?.block_self_approval === true && !!user && user.id === branch.created_by
  const actionError = actionMut.isError ? describeBranchActionError(actionMut.error) : null
  // While the diff is in flight there is nothing on screen to review, and the
  // merge's variable-deletion warning reads `removedVariables` from that same
  // diff, so a Merge clicked now would skip it. Hold the verdict buttons
  // (approve, request changes) and Merge until the diff has settled; the
  // housekeeping transitions (submit, reopen, close) do not read it and stay
  // live. An error is not "pending": the backend is the authority on the
  // merge, so a diff that failed to load must not lock the branch (tripl-kjhi.2).
  const diffLoading = diffLoad.status === 'pending'
  // Approve is the one action that can change nothing visible: on an already
  // `approved` branch it only restamps the approval's plan_hash, so the status
  // chip, the counts and the button row all render identically before and
  // after. A 200 that repaints zero pixels is indistinguishable from a dead
  // button — which is how it was reported. Say it happened.
  const actionSuccess =
    actionMut.isSuccess && actionMut.variables === 'approve'
      ? 'Approved against the branch as it stands now.'
      : null
  const landed = branch.status === 'merged' || branch.status === 'closed'
  // What main's newer changes mean for this branch, from the conflicts answer
  // (PL-8): nothing, safe, overlapping, or a merge that would refuse.
  const note = landed ? ({ kind: 'none' } as const) : behindNote(conflicts, behind)
  const neutralNote = note.kind === 'safe' || note.kind === 'moved'
  // Not for a base older than complete merge baselines: the update always
  // refuses those, so offering it would be a dead end (the note says why).
  const canUpdateFromMain =
    canWrite && !landed && conflicts?.behind === true && conflicts.updatable !== false
  // The merge refuses non-field conflicts outright; updating from main is
  // what clears them, so the merge button leads there instead (PL-8).
  const mergeNeedsUpdate =
    canUpdateFromMain &&
    (conflicts?.merge_blocked === true ||
      (conflicts?.entities ?? []).some((entity) => entity.entity_type !== 'event_type'))
  const onThisBranch = branchCtx.branchId === branch.id
  // Your own approval that the branch has since moved past: "Approve" again
  // refreshes it (PL-7).
  const myApproval = user ? (detail?.approvals ?? []).find((a) => a.user_id === user.id) : undefined
  // A review of nothing wastes a reviewer's time (PL-28). Only from a settled
  // diff: an unloaded one is not "empty".
  const emptyBranch = diffLoad.status === 'success' && counts.total === 0
  const reasons: Partial<Record<PlanBranchTransitionAction, string>> = {}
  if (selfApprovalBlocked) reasons.approve = "Authors can't approve their own branch (merge policy)."
  if (emptyBranch) reasons.submit = 'Make at least one change on this branch first.'
  // One primary per state, and it is the next step (PL-7 / JR-14): Submit on a
  // draft, Approve while in review, Merge once approved. Once your own approval
  // stands, approving is not your next step, so nothing is primary until the
  // quota fills; a stale one still makes "Approve again" the primary.
  const approvedByMe = !!myApproval && !myApproval.stale
  const primaryAction: PlanBranchTransitionAction | 'merge' | null =
    branch.status === 'approved'
      ? 'merge'
      : branch.status === 'ready_for_review'
        ? approvedByMe
          ? null
          : 'approve'
        : branch.status === 'draft' || branch.status === 'changes_requested'
          ? 'submit'
          : null
  const actionLabel = (action: PlanBranchTransitionAction): string => {
    // "Reopen" on an approved branch read as "open it again"; it moves it back
    // to draft (PL-6).
    if (action === 'reopen' && branch.status !== 'closed') return 'Move back to draft'
    if (action === 'approve' && myApproval?.stale) return 'Approve again'
    return ACTION_LABEL[action]
  }
  const transitions = ALLOWED_TRANSITIONS[branch.status]
  const reviewerCount = detail?.reviewers.length ?? 0

  return (
    // min-w-0 completes the `minmax(0,1fr)` on the parent grid track: capping
    // the TRACK's minimum lets the column be narrower than its content, but a
    // grid ITEM still defaults to `min-width:auto` and would overflow the track
    // instead of shrinking. Both are needed for the page to stop widening.
    <div className="flex min-w-0 flex-col gap-3">
      <Panel
        // The branch is what this page is about, so its name reads as the
        // page's heading in mono, not as a 12.5px card title (PL-17).
        title={<span className="mono text-heading">{branch.name}</span>}
        subtitle={`Opened by ${branchAuthor(branch, usersById)} · updated ${formatRelativeTime(branch.updated_at)}`}
        right={
          <>
            {ticket ? (
              <a
                href={ticket.href}
                target="_blank"
                rel="noreferrer"
                className="mono inline-flex items-center gap-0.5 text-caption hover:underline text-accent"
                title={`Open ${ticket.key} in ${ticket.field.display_name}`}
              >
                {ticket.key}
                <ArrowUpRight className="size-3" aria-hidden />
              </a>
            ) : null}
            {requiredApprovals > 0 && branch.status !== 'merged' ? (
              <Chip
                tone={
                  approvalsCount >= requiredApprovals
                    ? 'success'
                    : staleApprovals > 0
                      ? 'warning'
                      : 'neutral'
                }
                size="xs"
                title={
                  staleApprovals > 0
                    ? `${staleApprovals} approval(s) no longer match this branch's contents and do not count. Approve again to refresh the review.`
                    : `${countOf(requiredApprovals, 'approval', 'approvals')} required by the merge policy`
                }
              >
                Approvals {approvalsCount}/{requiredApprovals}
                {staleApprovals > 0 ? ` · ${staleApprovals} stale` : ''}
              </Chip>
            ) : null}
            <Chip tone={STATUS_TONE[branch.status]} size="xs">
              {STATUS_LABEL[branch.status]}
            </Chip>
          </>
        }
      >
        {/* The header keeps identity only (name, approvals, status); the
            lifecycle and the next step lead the body, so "how do I get this
            live" has an answer on the page (PL-6). */}
        <ReviewProgress
          status={branch.status}
          next={nextStepText({
            status: branch.status,
            required: requiredApprovals,
            approvals: approvalsCount,
            blockSelf: policy?.block_self_approval === true,
            reviewers: reviewerCount,
            empty: emptyBranch,
          })}
        />
        {/* Authoring on the branch you are reviewing, as one explicit action
            that says it switches you onto the branch (PL-11); the sidebar
            switcher is a different surface. Hidden once the branch is merged or
            closed, for the same reason its rows lose their Edit action. On a
            phone the actions get their own full-width row instead of wrapping
            around the header chips (PL-17). */}
        {!landed || (canWrite && branch.status !== 'merged') ? (
          <div
            className="flex flex-wrap items-center gap-2 border-t px-4 py-2.5 border-border-subtle"
          >
            {!landed ? (
              <>
                {onThisBranch ? (
                  <Chip tone="info" icon={<Check className="size-3" aria-hidden="true" />}>
                    You’re on this branch
                  </Chip>
                ) : null}
                <Button asChild variant="outline" size="sm" className="max-sm:flex-1">
                  <Link {...branchLink(`/p/${slug}/events`, branch.id)}>
                    <GitCompare className="size-3.5" aria-hidden="true" />
                    {onThisBranch ? 'Go to events' : canWrite ? 'Work on this branch' : 'View events on this branch'}
                  </Link>
                </Button>
                {canWrite && (
                  <Button asChild variant="ghost" size="sm" className="max-sm:flex-1">
                    <Link
                      {...branchLink(`/p/${slug}/events/all/new`, branch.id)}
                      aria-label="New event on this branch"
                    >
                      <Plus className="size-3.5" aria-hidden="true" />
                      New event on branch
                    </Link>
                  </Button>
                )}
              </>
            ) : null}
            {/* Not on a merged branch: deleting it throws away the review
                history, the comments and the ticket link of work that is on
                main now (PLAN-9). */}
            {canWrite && branch.status !== 'merged' && (
              <IconButton
                variant="ghost"
                className="ml-auto text-fg-tertiary hover:text-[var(--danger)]"
                onClick={handleDelete}
                disabled={deleteMut.isPending}
                label="Delete branch"
              >
                <Trash2 className="size-3.5" aria-hidden="true" />
              </IconButton>
            )}
          </div>
        ) : null}
        <div
          className="flex flex-wrap items-center gap-x-[18px] gap-y-2 border-t px-4 py-3 border-border-subtle"
        >
          {diffLoad.status !== 'success' ? (
            // Never the zero counts: "+0 ~0 −0" over an unloaded diff reads as
            // an empty branch, which is the false state measured on
            // production (tripl-kjhi.2). The strip carries the live region;
            // the Changes card below shows the same wait for the eye only.
            <DiffLoadNotice load={diffLoad} live />
          ) : (
            <>
              {/* Non-zero kinds first; a zero stays in its place but quiet, so
                  "+0" and "−0" no longer pull the eye like the real change
                  (PL-19). */}
              {(
                [
                  { tone: 'warning', sym: '~', n: counts.changed, label: 'modified' },
                  { tone: 'success', sym: '+', n: counts.added, label: 'added' },
                  { tone: 'danger', sym: '−', n: counts.removed, label: 'removed' },
                ] as const
              )
                .slice()
                .sort((a, b) => Number(b.n > 0) - Number(a.n > 0))
                .map((c) => (
                  <SummaryCount key={c.label} tone={c.tone} sym={c.sym} n={c.n} label={c.label} />
                ))}
              {/* Shown only when there is one, and shown rather than left implicit:
                  a rename subtracts itself from "added" and from "removed", and a
                  reviewer watching two counts drop with no new label beside them is
                  owed the word that explains where the rows went (tripl-amnn). */}
              {counts.renamed > 0 ? (
                <SummaryCount
                  tone={RENAMED_META.tone}
                  sym={RENAMED_META.sym}
                  n={counts.renamed}
                  label="renamed"
                />
              ) : null}
            </>
          )}
        </div>
        {/* `behind_base` is a yes/no, not a distance: it used to print "↓ 1
            behind main" as if main were one change ahead (PLAN-14). Amber only
            when main's newer changes overlap this branch's or the merge would
            refuse; otherwise neutral — and in every case with the action that
            brings main in, rather than advice to recreate the branch (PL-8). */}
        {(diffLoad.status === 'success' || conflicts?.behind !== undefined) && note.kind !== 'none' ? (
          <div
            role="note"
            className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t px-4 py-2.5 text-caption"
            style={{
              borderColor: 'var(--border-subtle)',
              color: neutralNote ? 'var(--fg-subtle)' : 'var(--warning)',
            }}
          >
            <span className="flex min-w-0 flex-1 items-start gap-1.5">
              {neutralNote ? (
                <Info className="mt-[2px] size-3 shrink-0" aria-hidden="true" />
              ) : (
                <AlertTriangle className="mt-[2px] size-3 shrink-0" aria-hidden="true" />
              )}
              {note.kind === 'overlap' ? (
                <a href="#branch-conflicts" className="underline underline-offset-2">
                  {countOf(note.count, 'entity you changed was', 'entities you changed were')}{' '}
                  also changed on main — resolve below.
                </a>
              ) : note.kind === 'blocked' ? (
                <span>
                  Main changed entities this branch also changes. Update from main to bring them in
                  before merging.
                </span>
              ) : note.kind === 'legacy' ? (
                <span>{INCOMPLETE_BASE_MESSAGE}</span>
              ) : note.kind === 'moved' ? (
                <span>Main has newer changes since this branch was created.</span>
              ) : (
                <span>Main has newer changes to other entities — safe to merge.</span>
              )}
            </span>
            {canUpdateFromMain ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="max-sm:w-full"
                onClick={() => setUpdateOpen(true)}
              >
                <RefreshCw className="size-3.5" aria-hidden="true" />
                Update from main
              </Button>
            ) : null}
          </div>
        ) : null}
        <BranchReviewSummary
          slug={slug}
          branch={branch}
          detail={detail}
          usersById={usersById}
          canWrite={canWrite}
          picker={reviewerPicker}
          onPickerChange={setReviewerPicker}
          onSubmitForReview={() => actionMut.mutate('submit')}
        />
        {/* Every status change, Merge included, in one row with exactly one
            primary: the next step (PL-7). The reason a button is disabled is
            written under the row, where a `title` on a disabled button was
            never shown (#237 DA-9). */}
        {canWrite && (transitions.length > 0 || branch.status === 'approved') && (
          <div
            className="flex flex-col gap-2 border-t px-4 py-3 border-border-subtle"
          >
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:flex-wrap">
              {branch.status === 'approved' ? (
                mergeNeedsUpdate ? (
                  <Button
                    size="sm"
                    className="max-sm:h-10 max-sm:w-full"
                    disabled={actionMut.isPending}
                    onClick={() => setUpdateOpen(true)}
                  >
                    <RefreshCw className="size-3.5" aria-hidden="true" />
                    Update from main first
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    className="max-sm:h-10 max-sm:w-full"
                    disabled={actionMut.isPending || diffLoading}
                    onClick={handleMerge}
                  >
                    <GitMerge className="size-3.5" aria-hidden="true" />
                    Merge to main
                  </Button>
                )
              ) : null}
              {transitions
                .slice()
                .sort((a, b) => Number(b === primaryAction) - Number(a === primaryAction))
                .map((action) => {
                  const reason = reasons[action] ?? null
                  const id = `${reasonIdBase}-${action}`
                  return (
                    <Button
                      key={action}
                      size="sm"
                      className="max-sm:h-10 max-sm:w-full"
                      variant={action === primaryAction && !reason ? 'default' : 'outline'}
                      disabled={
                        actionMut.isPending ||
                        !!reason ||
                        (diffLoading && DIFF_VERDICTS.has(action))
                      }
                      {...disabledReasonAria(id, reason)}
                      onClick={() => void handleAction(action)}
                    >
                      {actionLabel(action)}
                    </Button>
                  )
                })}
            </div>
            {transitions.map((action) => (
              <DisabledReason
                key={action}
                id={`${reasonIdBase}-${action}`}
                reason={reasons[action] ?? null}
                tone="muted"
              />
            ))}
          </div>
        )}
        {actionError ? (
          <p
            role="alert"
            className="border-t px-4 py-2.5 text-caption border-border-subtle text-danger"
          >
            {actionError}
          </p>
        ) : null}
        {deleteMut.isError ? (
          <p
            role="alert"
            className="border-t px-4 py-2.5 text-caption border-border-subtle text-danger"
          >
            Could not delete the branch: {getErrorMessage(deleteMut.error)}
          </p>
        ) : null}
        {actionSuccess ? (
          <p
            className="border-t px-4 py-2.5 text-caption border-border-subtle text-success"
          >
            {actionSuccess}
          </p>
        ) : null}
      </Panel>

      {/* Mounted for any open branch a writer sees, not only while it is
          behind: the update's own success makes it not behind, and the dialog
          must not vanish under the click that did it. */}
      {canWrite && !landed ? (
        <UpdateFromMainDialog
          slug={slug}
          branch={branch}
          open={updateOpen}
          onOpenChange={setUpdateOpen}
        />
      ) : null}

      <ImplementationTicketsPanel slug={slug} branch={branch} mergedAt={mergedAt} />

      {/* Straight under the summary, where the "main has moved on" note that
          links here sits, rather than below every change row (PL-20). */}
      <div id="branch-conflicts" className="scroll-mt-4">
        <ConflictsPanel slug={slug} branch={branch} />
      </div>

      {/* What the branch's deletes, deprecations and renames touch downstream
          (#257), next to the conflicts. Warn only; it never gates the merge. */}
      {!landed && (
        <div id="branch-impact" className="scroll-mt-4">
          <BranchImpactPanel slug={slug} branchId={branch.id} />
        </div>
      )}

      <Panel
        title="Changes"
        subtitle={
          diffLoad.status === 'success'
            ? countOf(visibleEntries.length, 'change', 'changes')
            : diffLoad.status === 'pending'
              ? 'loading'
              : 'unavailable'
        }
        subtitleTone={diffLoad.status === 'error' ? 'danger' : undefined}
      >
        {diffLoad.status !== 'success' ? (
          // The empty state is a settled answer, so it waits for one: while the
          // request is in flight the card says so instead (tripl-kjhi.2).
          diffLoad.status === 'pending' ? (
            // Bars, not a second "Loading changes…": the strip above carries
            // the words and the live region (PL-30).
            <div aria-hidden="true" className="space-y-2.5 px-4 py-4">
              <Skeleton className="h-4 w-3/5" />
              <Skeleton className="h-4 w-4/5" />
              <Skeleton className="h-4 w-2/5" />
            </div>
          ) : (
            <DiffLoadNotice load={diffLoad} className="px-4 py-7 text-center text-body-sm" />
          )
        ) : visibleEntries.length === 0 ? (
          <div className="px-4 py-7 text-center text-body-sm text-fg-tertiary">
            <p>No changes in this branch.</p>
            {/* The way forward from an empty branch (PL-4 / PL-28). */}
            {!landed && canWrite ? (
              <p className="mt-1 text-caption">
                Work on this branch and edit events: each change appears here for review.
              </p>
            ) : null}
          </div>
        ) : (
          <div>
            {visibleEntries.map((entry) => {
              const key = entryKey(entry.entity_type, entry.parent, entry.name)
              return (
                <ChangeRow
                  key={entryRowKey(entry)}
                  slug={slug}
                  branchId={branch.id}
                  entry={entry}
                  renamedTo={renamedTo.get(key)}
                  renamedEntityId={renamedEntityId.get(key)}
                  // A viewer's Edit only led to a form that refuses to save
                  // (PL-14); "Open event" stays.
                  editable={!landed && canWrite}
                  onRevert={canWrite ? handleRevert : undefined}
                  revertBlockedBy={revertBlockedBy.get(entryRowKey(entry))}
                  reverting={revertMut.isPending}
                />
              )
            })}
          </div>
        )}
        {diffLoad.status === 'success' && housekeepingEntries.length > 0 ? (
          <HousekeepingFold entries={housekeepingEntries} />
        ) : null}
        {revertMut.isError ? (
          <p
            role="alert"
            className="border-t px-4 py-2.5 text-caption border-border-subtle text-danger"
          >
            {getErrorMessage(revertMut.error)}
          </p>
        ) : null}
      </Panel>

      <CommentsPanel slug={slug} branchId={branch.id} usersById={usersById} />
    </div>
  )
}

function SummaryCount({
  tone,
  sym,
  n,
  label,
}: {
  tone: ChipTone
  sym: string
  n: number
  label: string
}) {
  const quiet = n === 0
  return (
    <div className="flex items-baseline gap-1.5">
      <span
        className={quiet ? 'tnum text-body' : 'tnum text-body font-semibold'}
        style={{ color: quiet ? 'var(--fg-faint)' : `var(--${tone})` }}
      >
        {sym}
        {n}
      </span>
      <span className="text-caption" style={{ color: quiet ? 'var(--fg-faint)' : 'var(--fg-subtle)' }}>
        {label}
      </span>
    </div>
  )
}

const PROGRESS_STEPS: { label: string; reached: readonly PlanBranchStatus[] }[] = [
  { label: 'Draft', reached: ['draft', 'changes_requested', 'ready_for_review', 'approved', 'merged'] },
  { label: 'In review', reached: ['ready_for_review', 'approved', 'merged'] },
  { label: 'Approved', reached: ['approved', 'merged'] },
  { label: 'Merged', reached: ['merged'] },
]

const CURRENT_STEP: Partial<Record<PlanBranchStatus, string>> = {
  draft: 'Draft',
  changes_requested: 'Draft',
  ready_for_review: 'In review',
  approved: 'Approved',
}

/** The sentence under the lifecycle: what happens next, from the status and
 * the merge policy (PL-6). Exported through the component only. */
function nextStepText({
  status,
  required,
  approvals,
  blockSelf,
  reviewers,
  empty,
}: {
  status: PlanBranchStatus
  required: number
  approvals: number
  blockSelf: boolean
  reviewers: number
  empty: boolean
}): string {
  const quota =
    required > 0
      ? ` Merging needs ${countOf(required, 'approval', 'approvals')}${blockSelf ? ' from someone other than the author' : ''}.`
      : ''
  switch (status) {
    case 'draft':
      return empty
        ? `Make changes on this branch, then submit it for review.${quota}`
        : `Submit for review when your changes are ready.${quota}`
    case 'changes_requested':
      return 'A reviewer asked for changes. Edit on this branch, then submit it for review again.'
    case 'ready_for_review': {
      const missing = Math.max(0, required - approvals)
      if (missing === 0) return 'Waiting for a reviewer to approve.'
      return `Waiting for ${countOf(missing, 'approval', 'approvals')}.${reviewers === 0 ? ' No reviewer assigned yet: add one below.' : ''}`
    }
    case 'approved': {
      const missing = Math.max(0, required - approvals)
      return missing === 0
        ? 'Approved: ready to merge to main.'
        : `Approved, but ${countOf(missing, 'more approval is', 'more approvals are')} needed before the merge.`
    }
    case 'merged':
      return 'Merged: these changes are part of the live plan.'
    case 'closed':
      return 'Closed without merging. Reopen it to continue the work.'
  }
}

/** Draft · In review · Approved · Merged, with the current step emphasised,
 * and the next-step sentence. A landed branch shows only the sentence: its
 * status chip already says where it ended. */
function ReviewProgress({ status, next }: { status: PlanBranchStatus; next: string }) {
  const current = CURRENT_STEP[status]
  return (
    <div className="flex flex-col gap-1.5 px-4 py-3">
      {current ? (
        <ol aria-label="Review progress" className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-caption">
          {PROGRESS_STEPS.map((step, index) => {
            const isCurrent = step.label === current
            const reached = step.reached.includes(status)
            return (
              <li
                key={step.label}
                aria-current={isCurrent ? 'step' : undefined}
                className="flex items-center gap-1.5"
                style={{
                  color: isCurrent ? 'var(--fg)' : reached ? 'var(--fg-secondary)' : 'var(--fg-faint)',
                  fontWeight: isCurrent ? 600 : undefined,
                }}
              >
                {index > 0 ? (
                  <span aria-hidden="true" className="text-fg-tertiary">
                    ·
                  </span>
                ) : null}
                {step.label}
              </li>
            )
          })}
        </ol>
      ) : null}
      <p className="text-body-sm text-fg-secondary">
        {next}
      </p>
    </div>
  )
}

/** Main's pane. With no working branch it teaches the flow and offers the
 * first one; otherwise it says what merged last and what waits for review,
 * and links the plan history (PL-16). */
function MainBranchPane({
  slug,
  main,
  branches,
  onNewBranch,
}: {
  slug: string
  main: PlanBranchSummary
  branches: PlanBranchSummary[]
  onNewBranch?: () => void
}) {
  const working = branches.filter((b) => b.kind !== 'main' && !isLandedBranch(b))
  const awaiting = working.filter((b) => b.status === 'ready_for_review').length
  const lastMerged = branches
    .filter((b) => b.kind !== 'main' && b.status === 'merged' && b.merged_at)
    .sort((a, b) => (b.merged_at ?? '').localeCompare(a.merged_at ?? ''))[0]
  if (working.length === 0) {
    return (
      <Panel title={main.name} subtitle="The live production plan">
        <EmptyState
          icon={GitCompare}
          title="Propose plan changes safely"
          description="Create a branch, edit events on it, get a review, then merge: every change merges here, into the live plan."
          action={
            onNewBranch ? (
              <Button size="lg" onClick={onNewBranch}>
                <Plus aria-hidden="true" />
                Create a branch
              </Button>
            ) : undefined
          }
        />
      </Panel>
    )
  }
  return (
    <Panel title={main.name} subtitle="The live production plan">
      <div className="flex flex-col gap-2 px-4 py-4 text-body-sm text-fg-tertiary">
        <p>This is the default branch: every change merges here. Select a branch to review its changes.</p>
        <ul className="flex flex-col gap-1 text-caption">
          {lastMerged ? (
            <li>
              Last merged:{' '}
              <Link
                to={`/p/${slug}/branches/${lastMerged.id}`}
                className="mono hover:underline text-fg"
              >
                {lastMerged.name}
              </Link>{' '}
              · {formatRelativeTime(lastMerged.merged_at!)}
            </li>
          ) : null}
          <li>
            {countOf(working.length, 'open branch', 'open branches')}
            {awaiting > 0 ? `, ${awaiting} awaiting review` : ''}
          </li>
        </ul>
        <Link
          to={`/p/${slug}/history`}
          className="inline-flex w-fit items-center gap-1 text-caption font-medium hover:underline text-accent"
        >
          <History className="size-3" aria-hidden="true" />
          View plan history
        </Link>
      </div>
    </Panel>
  )
}

/** What the summary strip and the Changes card show in place of counts and
 * rows until the diff request settles (tripl-kjhi.2). `live` marks the one
 * copy that announces to assistive tech; the other is for the eye only, so a
 * screen reader hears the change once. The Retry button rides with the error
 * wherever the notice is rendered — the reviewer should not have to look for it. */
function DiffLoadNotice({
  load,
  live,
  className,
}: {
  load: DiffLoad
  live?: boolean
  className?: string
}) {
  if (load.status === 'pending') {
    return (
      <div
        role={live ? 'status' : undefined}
        aria-live={live ? 'polite' : undefined}
        className={className ?? 'flex items-center gap-3'}
      >
        <span className="sr-only">Loading changes…</span>
        <Skeleton aria-hidden="true" className="h-4 w-16" />
        <Skeleton aria-hidden="true" className="h-4 w-16" />
        <Skeleton aria-hidden="true" className="h-4 w-16" />
      </div>
    )
  }
  return (
    <p
      role={live ? 'alert' : undefined}
      className={className ?? 'text-caption'}
      style={{ color: 'var(--danger)' }}
    >
      Could not load the changes: {getErrorMessage(load.error)}{' '}
      <button type="button" onClick={load.retry} className="font-medium underline">
        Retry
      </button>
    </p>
  )
}
