import { useEffect, useId, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, X } from 'lucide-react'

import { planBranchesApi } from '@/api/planBranches'
import { usersApi } from '@/api/users'
import { Button } from '@/components/ui/button'
import { NativeSelect } from '@/components/settings/kit'
import { displayUser } from '@/hooks/useUsersById'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectCandidates } from '@/lib/projectCandidates'
import { getErrorMessage } from '@/lib/utils'
import type { PlanBranchApproval, PlanBranchDetail, PlanBranchSummary } from '@/types'
import { planBranchDetailKey, projectMembersQueryOptions, usersKey } from '@/lib/queryKeys'

/**
 * Why the reviewer picker is open: `add` is the "+ Reviewer" button; `submit`
 * is "Submit for review" clicked with nobody assigned, where the picker asks
 * who should review before the branch is sent (JR-14).
 */
export type ReviewerPickerIntent = 'add' | 'submit' | null

interface BranchReviewSummaryProps {
  slug: string
  branch: PlanBranchSummary
  detail: PlanBranchDetail | undefined
  usersById: Map<string, string>
  /** Assigning reviewers writes to the branch; a viewer only reads them. */
  canWrite: boolean
  /** The picker's state, owned by the detail page so its Submit can open it. */
  picker: ReviewerPickerIntent
  onPickerChange: (next: ReviewerPickerIntent) => void
  /** Sends the branch for review: after "Add and submit", or without a reviewer. */
  onSubmitForReview: () => void
}

/**
 * What the branch is for and who is reviewing it (PLAN-17).
 *
 * The create dialog asks for a description and the detail response carries the
 * reviewers and every approval, yet the pane showed none of them: only an
 * "Approvals n/N" count, with no way to tell who had approved, whose approval
 * had gone stale, or to ask anyone to review at all — `addReviewer` had no UI.
 */
export function BranchReviewSummary({
  slug,
  branch,
  detail,
  usersById,
  canWrite,
  picker,
  onPickerChange,
  onSubmitForReview,
}: BranchReviewSummaryProps) {
  const qc = useQueryClient()
  const pickerId = useId()
  const [picked, setPicked] = useState('')
  // The picker opens on request: the roster is long, and most visits to a
  // branch are to read it, not to staff it. The detail page owns whether it is
  // open, so "Submit for review" on an unstaffed branch can open it too.
  const picking = picker !== null
  const selectRef = useRef<HTMLSelectElement>(null)
  // Opened from Submit, the picker sits above the button that was clicked, so
  // focus follows it there rather than staying on a button that did not send.
  useEffect(() => {
    if (picker === 'submit') selectRef.current?.focus()
  }, [picker])
  const open = branch.status !== 'merged' && branch.status !== 'closed'
  const reviewers = detail?.reviewers ?? []
  const approvals = (detail?.approvals ?? []).filter(
    (a): a is PlanBranchApproval & { user_id: string } => a.user_id !== null,
  )
  const description = branch.description.trim()

  // Only the project's members, and the instance owners who see every project
  // without a member row, can review: anyone else cannot see the project, and
  // the server refuses them (tripl-vefw).
  const { data: members } = useQuery({
    ...projectMembersQueryOptions(slug),
    enabled: canWrite && open,
  })
  const { data: users } = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
    enabled: canWrite && open,
  })
  const assigned = new Set(reviewers.map((r) => r.user_id))
  const candidates = projectCandidates(members, users).filter((m) => !assigned.has(m.user_id))

  const refresh = () => qc.invalidateQueries({ queryKey: planBranchDetailKey(slug, branch.id) })
  const addMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (userId: string) => planBranchesApi.addReviewer(slug, branch.id, userId),
    onSuccess: () => {
      setPicked('')
      onPickerChange(null)
      // Awaited, so a submit that follows toasts the reviewer just added.
      return refresh()
    },
  })
  const removeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (userId: string) => planBranchesApi.removeReviewer(slug, branch.id, userId),
    onSuccess: refresh,
  })
  const error = addMut.error ?? removeMut.error
  const showPicker = canWrite && open && candidates.length > 0
  const pending = addMut.isPending || removeMut.isPending

  if (!description && reviewers.length === 0 && approvals.length === 0 && !showPicker) {
    return null
  }

  return (
    <div
      className="flex flex-col gap-2 border-t px-4 py-3 text-caption border-border-subtle"
    >
      {description ? (
        <p className="whitespace-pre-line text-fg">
          {description}
        </p>
      ) : null}
      {approvals.length > 0 ? (
        <p className="text-fg-tertiary">
          Approved by{' '}
          {approvals.map((approval, index) => (
            <span key={approval.user_id}>
              {index > 0 ? ', ' : null}
              <span style={{ color: approval.stale ? 'var(--fg-subtle)' : 'var(--fg)' }}>
                {displayUser(usersById, approval.user_id)}
              </span>
              {approval.stale ? (
                <span
                  className="text-warning"
                  title="The branch changed after this approval, so it no longer counts."
                >
                  {' '}
                  (stale)
                </span>
              ) : null}
            </span>
          ))}
        </p>
      ) : null}
      {reviewers.length > 0 || showPicker ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-fg-tertiary">Reviewers</span>
          {reviewers.length === 0 ? (
            <span className="text-fg-tertiary">none assigned</span>
          ) : (
            <ul className="contents">
              {reviewers.map((reviewer) => {
                const name = displayUser(usersById, reviewer.user_id)
                return (
                  <li
                    key={reviewer.id}
                    className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 border-border-subtle text-fg"
                  >
                    {name}
                    {canWrite && open ? (
                      <button
                        type="button"
                        disabled={pending}
                        onClick={() => removeMut.mutate(reviewer.user_id)}
                        aria-label={`Remove reviewer ${name}`}
                        className="rounded-full p-0.5 hover:bg-[var(--surface-hover)] disabled:opacity-50"
                      >
                        <X className="size-3" aria-hidden="true" />
                      </button>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          )}
          {/* Inline after the chips, as an outlined control: the ghost button
              floated far right read as a heading (PL-27). */}
          {showPicker && !picking ? (
            <Button
              type="button"
              size="xs"
              variant="outline"
              aria-label="Add reviewer"
              onClick={() => onPickerChange('add')}
            >
              <Plus aria-hidden="true" />
              Reviewer
            </Button>
          ) : null}
          {showPicker && picking ? (
            <form
              className="flex flex-wrap items-center gap-1.5"
              onSubmit={(event) => {
                event.preventDefault()
                if (!picked) return
                const thenSubmit = picker === 'submit'
                addMut.mutate(picked, {
                  onSuccess: () => {
                    if (thenSubmit) onSubmitForReview()
                  },
                })
              }}
            >
              {picker === 'submit' ? (
                <label htmlFor={pickerId} className="font-medium text-fg">
                  Who should review this?
                </label>
              ) : (
                <label htmlFor={pickerId} className="sr-only">
                  Reviewer to add
                </label>
              )}
              <NativeSelect
                ref={selectRef}
                id={pickerId}
                size="sm"
                value={picked}
                onChange={setPicked}
                options={[
                  { value: '', label: 'Choose a person…' },
                  ...candidates.map((member) => ({
                    value: member.user_id,
                    label: member.name || member.email,
                  })),
                ]}
              />
              <Button
                type="submit"
                size="sm"
                variant={picker === 'submit' ? 'default' : 'outline'}
                disabled={!picked || pending}
              >
                {picker === 'submit' ? 'Add and submit' : 'Add'}
              </Button>
              {picker === 'submit' ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={pending}
                  onClick={() => {
                    onPickerChange(null)
                    onSubmitForReview()
                  }}
                >
                  Submit without a reviewer
                </Button>
              ) : null}
              <Button type="button" size="sm" variant="ghost" onClick={() => onPickerChange(null)}>
                Cancel
              </Button>
            </form>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="text-danger">
          {getErrorMessage(error)}
        </p>
      ) : null}
    </div>
  )
}
