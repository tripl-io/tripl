import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { planBranchesApi } from '@/api/planBranches'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { countOf } from '@/lib/plural'
import {
  planBranchConflictsKey,
  planBranchCountsKey,
  planBranchDiffKey,
  planBranchUpdatePreviewKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type {
  PlanBranchConflicts,
  PlanBranchSummary,
  PlanDiffEntityType,
  ResolutionChoice,
  UpdateFromMainResolution,
} from '@/types'
import {
  conflictChoiceKey,
  describeUpdateFromMainError,
  entityChangeLines,
  entityChangeTotal,
  isMainMovedRefusal,
  unresolvedUpdateConflicts,
  updateBlockedMessage,
} from './branchDiffModel'
import { invalidateBranchUpdated } from './branchQueryKeys'
import { type BulkScope, type BulkStrategy, bulkChoices } from './conflictModel'
import { ConflictList } from './ConflictsPanel'

interface UpdateFromMainDialogProps {
  slug: string
  branch: PlanBranchSummary
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Update from main": a three-way merge of main INTO the branch. Says
 * what main brings, asks for a side on every overlap, and sends the choices
 * with the update in one call — nothing is written until every overlap has
 * one.
 */
export function UpdateFromMainDialog({ slug, branch, open, onOpenChange }: UpdateFromMainDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        {/* Mounted only while open, so choices and refusals start fresh on
            every opening. */}
        {open ? (
          <UpdateFromMainBody slug={slug} branch={branch} onClose={() => onOpenChange(false)} />
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function UpdateFromMainBody({
  slug,
  branch,
  onClose,
}: {
  slug: string
  branch: PlanBranchSummary
  onClose: () => void
}) {
  const qc = useQueryClient()
  const bodyRef = useRef<HTMLDivElement>(null)
  const preview = useQuery({
    queryKey: planBranchUpdatePreviewKey(slug, branch.id),
    queryFn: () => planBranchesApi.getUpdatePreview(slug, branch.id),
    // Main may have moved since the dialog was last open.
    staleTime: 0,
  })
  // Picks made here, over the choices already stored on the branch.
  const [choices, setChoices] = useState<ReadonlyMap<string, ResolutionChoice>>(new Map())
  // The overlaps as the update's own 409 reported them, when they differ from
  // the preview's (main moved between the two).
  const [refused, setRefused] = useState<PlanBranchConflicts | null>(null)
  const [mainMoved, setMainMoved] = useState(false)
  const [focusUnresolved, setFocusUnresolved] = useState(0)

  const conflicts = refused ?? preview.data?.conflicts ?? null
  const entities = conflicts?.entities ?? []
  const choiceOf = (key: string, stored: ResolutionChoice | null) => choices.get(key) ?? stored

  const resolutions: UpdateFromMainResolution[] = []
  let unresolved = 0
  for (const entity of entities) {
    for (const field of entity.fields) {
      const choice = choiceOf(conflictChoiceKey(entity, field.field), field.choice)
      if (choice) {
        resolutions.push({
          entity_type: entity.entity_type as PlanDiffEntityType,
          entity_name: entity.name,
          field_name: field.field,
          choice,
        })
      } else {
        unresolved += 1
      }
    }
  }

  useEffect(() => {
    if (focusUnresolved === 0) return
    const frame = requestAnimationFrame(() => {
      const row = bodyRef.current?.querySelector<HTMLElement>('[data-unresolved="true"]')
      row?.scrollIntoView?.({ block: 'center' })
      row?.querySelector<HTMLButtonElement>('button')?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [focusUnresolved])

  const updateMut = useMutation({
    // Every refusal is rendered in the dialog, beside the choices.
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      planBranchesApi.updateFromMain(slug, branch.id, {
        expected_main_hash: preview.data?.main_hash ?? null,
        resolutions,
      }),
    onSuccess: (result) => {
      invalidateBranchUpdated(qc, slug, branch.id)
      if (result.updated) {
        toast.success(
          `Branch updated from main — ${countOf(entityChangeTotal(result.applied), 'change', 'changes')} brought in`,
        )
      } else {
        toast.success('This branch already has everything on main.')
      }
      onClose()
    },
    onError: (error) => {
      // The header note and the Conflicts panel read the same overlaps: after
      // a refusal they must not keep the answer from before it.
      void qc.invalidateQueries({ queryKey: planBranchConflictsKey(slug, branch.id) })
      const fresh = unresolvedUpdateConflicts(error)
      if (fresh) {
        setRefused(fresh)
        setFocusUnresolved((n) => n + 1)
        return
      }
      if (isMainMovedRefusal(error)) {
        setRefused(null)
        // Picks were made against main as it was. A row keyed the same may
        // now carry other values, so every one is asked again rather than
        // shown already resolved.
        setChoices(new Map())
        setMainMoved(true)
        void qc.invalidateQueries({ queryKey: planBranchUpdatePreviewKey(slug, branch.id) })
        void qc.invalidateQueries({ queryKey: planBranchDiffKey(slug, branch.id) })
        void qc.invalidateQueries({ queryKey: planBranchCountsKey(slug) })
        return
      }
      if (updateBlockedMessage(error)) {
        // The preview names every blocker; show them as they are now.
        void qc.invalidateQueries({ queryKey: planBranchUpdatePreviewKey(slug, branch.id) })
      }
    },
  })

  const pick = (key: string, choice: ResolutionChoice) => {
    setChoices((prev) => new Map(prev).set(key, choice))
  }
  // Local, like a single pick: the choices go with the update's one POST.
  const pickAll = (strategy: BulkStrategy, scope?: BulkScope) => {
    setChoices((prev) => {
      const next = new Map(prev)
      for (const entity of entities) {
        for (const item of bulkChoices([entity], strategy, scope)) {
          next.set(conflictChoiceKey(entity, item.field), item.choice)
        }
      }
      return next
    })
  }

  const data = preview.data
  const behind = data?.behind === true
  // Something no choice settles: named here, and the update stays disabled.
  const blockers = data?.blockers ?? []
  const updatable = data?.updatable !== false && blockers.length === 0
  const lines = data ? entityChangeLines(data.main_changes) : []
  // The two refusals the dialog answers in place are not errors to print.
  const error =
    updateMut.isError &&
    !unresolvedUpdateConflicts(updateMut.error) &&
    !isMainMovedRefusal(updateMut.error)
      ? describeUpdateFromMainError(updateMut.error)
      : null
  const canSubmit =
    !!data && behind && updatable && unresolved === 0 && !updateMut.isPending && !preview.isFetching

  return (
    <div className="flex min-h-0 flex-col gap-4">
      <DialogHeader>
        <DialogTitle>Update from main</DialogTitle>
        <DialogDescription>
          Bring main’s newer changes into <span className="mono">{branch.name}</span>. Your own
          changes stay; where you and main changed the same thing, choose which to keep.
        </DialogDescription>
      </DialogHeader>
      <DialogBody ref={bodyRef} className="grid gap-4">
        {preview.isPending ? (
          <div aria-hidden="true" className="space-y-2.5" data-testid="update-preview-loading">
            <Skeleton className="h-4 w-3/5" />
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-4 w-2/5" />
          </div>
        ) : preview.isError ? (
          <p role="alert" className="text-body-sm text-danger">
            Could not load what main would bring: {getErrorMessage(preview.error)}
          </p>
        ) : !behind ? (
          <p className="text-body-sm text-fg-tertiary">
            This branch already has everything on main.
          </p>
        ) : (
          <>
            {mainMoved ? (
              <p role="status" className="text-caption text-warning">
                Main changed again — review the new changes.
              </p>
            ) : null}
            {blockers.length > 0 ? (
              <section className="grid gap-1.5" data-testid="update-blockers">
                <h3 className="text-body-sm font-medium text-warning">
                  This update cannot run yet
                </h3>
                <ul className="list-disc space-y-0.5 pl-5 text-body-sm text-fg-secondary">
                  {blockers.map((blocker, index) => (
                    <li key={`${blocker.kind}:${blocker.entity_type ?? ''}:${blocker.name ?? index}`}>
                      {blocker.message}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
            <section className="grid gap-1.5">
              <h3 className="text-body-sm font-medium text-fg">What main brings</h3>
              {lines.length > 0 ? (
                <ul className="space-y-0.5 text-body-sm text-fg-secondary">
                  {lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-body-sm text-fg-tertiary">
                  Only changes this branch also made.
                </p>
              )}
            </section>
            {entities.length > 0 ? (
              <section className="grid gap-1.5">
                <h3 className="text-body-sm font-medium text-fg">
                  Overlaps{' '}
                  <span className="font-normal text-fg-tertiary">
                    ({countOf(entities.length, 'entity', 'entities')})
                  </span>
                </h3>
                <p className="text-caption text-fg-tertiary">
                  You and main both changed these. Choose a side for each.
                </p>
                <ConflictList
                  entities={entities}
                  choiceOf={(entity, field) =>
                    choiceOf(conflictChoiceKey(entity, field.field), field.choice)
                  }
                  pending={updateMut.isPending}
                  onResolve={(entity, field, choice) =>
                    pick(conflictChoiceKey(entity, field.field), choice)
                  }
                  onBulk={pickAll}
                />
              </section>
            ) : null}
            {branch.status === 'approved' ? (
              <p role="note" className="text-caption text-fg-tertiary">
                Existing approvals will need renewing.
              </p>
            ) : null}
          </>
        )}
        {error ? (
          <p role="alert" className="text-body-sm text-danger">
            {error}
          </p>
        ) : null}
      </DialogBody>
      <DialogFooter className="sm:items-center">
        {behind && updatable && unresolved > 0 ? (
          <span className="text-caption text-fg-tertiary sm:mr-auto" aria-live="polite">
            {unresolved} left to choose
          </span>
        ) : null}
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" disabled={!canSubmit} onClick={() => updateMut.mutate()}>
          {updateMut.isPending ? (
            <>
              <Loader2 className="animate-spin" aria-hidden="true" />
              Updating…
            </>
          ) : (
            'Update branch'
          )}
        </Button>
      </DialogFooter>
    </div>
  )
}
