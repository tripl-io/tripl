import { useId, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import {
  planBranchesApi,
  type BranchTransferMode,
  type BranchTransferResult,
} from '@/api/planBranches'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useDialogDirty, useDialogLeave } from '@/components/ui/dialog-guard'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { useDirtySinceOpen } from '@/hooks/useUnsavedChangesGuard'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { countOf, pluralize } from '@/lib/plural'
import { planBranchesKey, planBranchTransferPreviewKey } from '@/lib/queryKeys'
import type { PlanBranchSummary, PlanDiffEntry } from '@/types'
import { BRANCH_NAME_HINT, branchNameProblem } from './branchMeta'
import { invalidateBranchUpdated } from './branchQueryKeys'
import {
  countTransferChanges,
  describeBaseMismatch,
  describeTransferConflict,
  describeTransferItem,
  toTransferRefs,
  transferConflicts,
  transferErrorMessage,
} from './branchTransferModel'

/** The target picker's value for "cut a new branch from main". */
const NEW_BRANCH = '__new__'

interface TransferChangesDialogProps {
  slug: string
  /** The branch the rows come from. */
  branch: PlanBranchSummary
  /** The rows to send, rename halves included (`selectionWithRenames`). */
  entries: PlanDiffEntry[]
  /** The diff's renames (`transferRenamePairs`): a rename counts as one change. */
  renamePairs: readonly (readonly [PlanDiffEntry, PlanDiffEntry])[]
  mode: BranchTransferMode
  open: boolean
  onOpenChange: (open: boolean) => void
  /** After a real transfer: the selection is spent. */
  onDone: () => void
}

/**
 * "Move to branch…" / "Copy to branch…": pick an open branch (or a new one),
 * read what the transfer would do — a dry run of the very same call — and
 * confirm. Nothing is written until confirm, and confirm stays off while
 * any row is refused.
 */
export function TransferChangesDialog({
  slug,
  branch,
  entries,
  renamePairs,
  mode,
  open,
  onOpenChange,
  onDone,
}: TransferChangesDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        {/* Mounted only while open: target, mode and refusals start fresh. */}
        {open ? (
          <TransferBody
            slug={slug}
            branch={branch}
            entries={entries}
            renamePairs={renamePairs}
            initialMode={mode}
            onClose={() => onOpenChange(false)}
            onDone={onDone}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function TransferBody({
  slug,
  branch,
  entries,
  renamePairs,
  initialMode,
  onClose,
  onDone,
}: {
  slug: string
  branch: PlanBranchSummary
  entries: PlanDiffEntry[]
  renamePairs: readonly (readonly [PlanDiffEntry, PlanDiffEntry])[]
  initialMode: BranchTransferMode
  onClose: () => void
  onDone: () => void
}) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const groupId = useId()
  const nameId = useId()
  const descriptionId = useId()
  // A closed branch can be copied from, never moved off.
  const canMove = branch.status !== 'closed'
  const [mode, setMode] = useState<BranchTransferMode>(canMove ? initialMode : 'copy')
  const [target, setTarget] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const [newDescription, setNewDescription] = useState('')
  // A branch this dialog created whose transfer was then refused: it stays
  // the selected target, so a retry does not cut a second one.
  const [created, setCreated] = useState<PlanBranchSummary | null>(null)
  // The body mounts with the dialog, so its first render is the baseline. A
  // picked target or a typed branch name asks before Escape, an outside click,
  // the X or Cancel drops it; a finished transfer closes without asking.
  const dirty = useDirtySinceOpen(true, { mode, target, newName, newDescription })
  useDialogDirty(dirty)
  const leave = useDialogLeave()

  const branches = useQuery({
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug),
  })
  const targets = useMemo(
    () => {
      const listed = (branches.data?.items ?? []).filter(
        (other) =>
          other.id !== branch.id &&
          other.kind === 'working' &&
          other.status !== 'merged' &&
          other.status !== 'closed',
      )
      return created && !listed.some((other) => other.id === created.id)
        ? [...listed, created]
        : listed
    },
    [branches.data, branch.id, created],
  )
  const existingNames = (branches.data?.items ?? []).map((other) => other.name)
  const isNew = target === NEW_BRANCH
  const nameProblem = isNew ? branchNameProblem(newName, existingNames) : null
  const refs = useMemo(() => toTransferRefs(entries), [entries])

  // The preview: the real call with `dry_run`, so what it lists is exactly
  // what confirm writes. Against "New branch…" it reads a branch cut from
  // main now (`target_branch_id: null`) and no branch is created for it.
  const previewTarget = isNew ? null : target
  const preview = useQuery({
    queryKey: planBranchTransferPreviewKey(slug, branch.id, previewTarget ?? NEW_BRANCH, mode, refs),
    queryFn: () =>
      planBranchesApi.transfer(slug, branch.id, {
        target_branch_id: previewTarget,
        mode,
        entries: refs,
        dry_run: true,
      }),
    enabled: target !== null && refs.length > 0,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    meta: SILENT_ERROR_META,
  })

  const transferMut = useMutation({
    // Every refusal is rendered in the dialog.
    meta: SILENT_ERROR_META,
    mutationFn: async (): Promise<{ result: BranchTransferResult; targetId: string }> => {
      const send = (targetId: string) =>
        planBranchesApi.transfer(slug, branch.id, {
          target_branch_id: targetId,
          mode,
          entries: refs,
        })
      if (!isNew) {
        if (!target) throw new Error('Pick a branch to send the changes to.')
        return { result: await send(target), targetId: target }
      }
      // The branch is created first, committed, and only then receives the
      // rows: main may move in between, and then the transfer is refused.
      const branchRow = await planBranchesApi.create(slug, {
        name: newName.trim(),
        description: newDescription.trim(),
      })
      void qc.invalidateQueries({ queryKey: planBranchesKey(slug) })
      try {
        return { result: await send(branchRow.id), targetId: branchRow.id }
      } catch (error) {
        // Kept selected with the refusal on screen, so a retry lands on it
        // instead of cutting a second one.
        setCreated(branchRow)
        setTarget(branchRow.id)
        throw error
      }
    },
    onSuccess: ({ result, targetId }) => {
      invalidateBranchUpdated(qc, slug, branch.id)
      invalidateBranchUpdated(qc, slug, targetId)
      const count = countTransferChanges([...result.applied, ...result.carried], renamePairs)
      const verb = result.mode === 'move' ? 'Moved' : 'Copied'
      const path = projectPath(currentOrgSlug(), slug, `/branches/${targetId}`)
      toast.success(
        `${verb} ${countOf(count, 'change', 'changes')} to ${result.target_branch_name ?? 'the branch'}`,
        { action: { label: 'Open', onClick: () => navigate(path) } },
      )
      onDone()
      onClose()
    },
  })

  const refusal = transferMut.error ?? preview.error
  const conflicts = refusal ? transferConflicts(refusal) : null
  const mismatch = refusal ? describeBaseMismatch(refusal) : null
  const otherError = refusal && !conflicts && !mismatch ? transferErrorMessage(refusal) : null
  const data = preview.data
  const canConfirm =
    target !== null &&
    !!data &&
    !preview.isFetching &&
    !preview.isError &&
    !transferMut.isPending &&
    (!isNew || (newName.trim() !== '' && nameProblem === null)) &&
    data.applied.length + data.carried.length + data.skipped.length > 0
  const verb = mode === 'move' ? 'Move' : 'Copy'
  const changeCount = countTransferChanges(entries, renamePairs)
  const changes = `${countOf(changeCount, 'change', 'changes')} ${pluralize(changeCount, 'lands', 'land')}`

  return (
    <div className="flex min-h-0 flex-col gap-4">
      <DialogHeader>
        <DialogTitle>{verb} changes to another branch</DialogTitle>
        <DialogDescription>
          {mode === 'move' ? (
            <>
              The {changes} on the branch you pick and{' '}
              {pluralize(changeCount, 'is', 'are')} undone on{' '}
              <span className="mono">{branch.name}</span>.
            </>
          ) : (
            <>
              The {changes} on the branch you pick;{' '}
              <span className="mono">{branch.name}</span> keeps{' '}
              {pluralize(changeCount, 'it', 'them')} too.
            </>
          )}
        </DialogDescription>
      </DialogHeader>
      <DialogBody className="grid gap-4">
        <SegmentedControl
          aria-label="Move or copy"
          value={mode}
          onChange={(next) => {
            transferMut.reset()
            setMode(next)
          }}
          options={[
            {
              value: 'move',
              label: 'Move',
              disabled: !canMove,
              title: canMove ? undefined : 'A closed branch can only be copied from.',
            },
            { value: 'copy', label: 'Copy' },
          ]}
        />

        <fieldset className="grid gap-1.5" aria-describedby={groupId}>
          <legend className="text-body-sm font-medium text-fg">Target branch</legend>
          <p id={groupId} className="text-caption text-fg-tertiary">
            Open branches only. Both must have been cut from the same main.
          </p>
          {branches.isPending ? (
            <Skeleton className="h-4 w-2/5" />
          ) : (
            <div className="grid gap-1">
              {targets.map((other) => (
                <label key={other.id} className="flex items-center gap-2 text-body-sm">
                  <input
                    type="radio"
                    name="transfer-target"
                    value={other.id}
                    checked={target === other.id}
                    onChange={() => {
                      transferMut.reset()
                      setTarget(other.id)
                    }}
                  />
                  <span className="mono">{other.name}</span>
                </label>
              ))}
              <label className="flex items-center gap-2 text-body-sm">
                <input
                  type="radio"
                  name="transfer-target"
                  value={NEW_BRANCH}
                  checked={isNew}
                  onChange={() => {
                    transferMut.reset()
                    setTarget(NEW_BRANCH)
                  }}
                />
                New branch…
              </label>
            </div>
          )}
        </fieldset>

        {isNew ? (
          <div className="grid gap-2">
            <div className="grid gap-1">
              <Label htmlFor={nameId}>Branch name</Label>
              <Input
                id={nameId}
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                aria-invalid={nameProblem ? true : undefined}
                placeholder="PROJ-4770"
              />
              <p className={nameProblem ? 'text-caption text-danger' : 'text-caption text-fg-tertiary'}>
                {nameProblem ?? BRANCH_NAME_HINT}
              </p>
            </div>
            <div className="grid gap-1">
              <Label htmlFor={descriptionId}>Description</Label>
              <Textarea
                id={descriptionId}
                value={newDescription}
                onChange={(event) => setNewDescription(event.target.value)}
                rows={2}
              />
            </div>
          </div>
        ) : null}

        {target === null ? null : preview.isPending ? (
          <div aria-hidden="true" className="space-y-2.5" data-testid="transfer-preview-loading">
            <Skeleton className="h-4 w-3/5" />
            <Skeleton className="h-4 w-2/5" />
          </div>
        ) : data ? (
          <TransferPreview result={data} />
        ) : null}

        {conflicts ? (
          <section className="grid gap-1.5" data-testid="transfer-conflicts">
            <h3 className="text-body-sm font-medium text-warning">This transfer cannot run yet</h3>
            <ul className="list-disc space-y-0.5 pl-5 text-body-sm text-fg-secondary">
              {conflicts.map((conflict, index) => (
                <li key={`${conflict.entity_type}:${conflict.name}:${conflict.field ?? ''}:${index}`}>
                  {describeTransferConflict(conflict)}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {mismatch ? (
          <div role="alert" className="grid gap-1 text-body-sm text-warning">
            <p>{mismatch.message}</p>
            <p className="flex flex-wrap gap-3">
              {mismatch.behindBranchIds.map((id) => (
                <Link
                  key={id}
                  to={projectPath(currentOrgSlug(), slug, `/branches/${id}`)}
                  className="text-accent hover:underline"
                  onClick={(event) => {
                    // A modified click opens another tab and leaves this dialog as it is.
                    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return
                    event.preventDefault()
                    leave(() => {
                      onClose()
                      navigate(projectPath(currentOrgSlug(), slug, `/branches/${id}`))
                    })
                  }}
                >
                  Open {id === branch.id ? branch.name : (targets.find((t) => t.id === id)?.name ?? 'the branch')} to update it
                </Link>
              ))}
            </p>
          </div>
        ) : null}
        {otherError ? (
          <p role="alert" className="text-body-sm text-danger">
            {otherError}
          </p>
        ) : null}
        {created && transferMut.isError ? (
          <p className="text-caption text-fg-tertiary">
            The branch <span className="mono">{created.name}</span> was created and stays empty;
            delete it if you do not need it.
          </p>
        ) : null}
      </DialogBody>
      <DialogFooter>
        {/* A close request like Escape, so it asks first when something is picked. */}
        <DialogClose asChild>
          <Button type="button" variant="outline">
            Cancel
          </Button>
        </DialogClose>
        <Button type="button" disabled={!canConfirm} onClick={() => transferMut.mutate()}>
          {transferMut.isPending ? (
            <>
              <Loader2 className="animate-spin" aria-hidden="true" />
              {mode === 'move' ? 'Moving…' : 'Copying…'}
            </>
          ) : (
            `${verb} changes`
          )}
        </Button>
      </DialogFooter>
    </div>
  )
}

function TransferPreview({ result }: { result: BranchTransferResult }) {
  const verb = result.mode === 'move' ? 'Will move' : 'Will copy'
  const warnings = result.warnings ?? []
  return (
    <div className="grid gap-3" data-testid="transfer-preview">
      <ItemList title={verb} items={result.applied.map(describeTransferItem)} />
      <ItemList title="Also carried" items={result.carried.map(describeTransferItem)} />
      <ItemList
        title="Skipped (already on target)"
        items={result.skipped.map(describeTransferItem)}
      />
      {warnings.length > 0 ? (
        <section className="grid gap-1">
          <h3 className="text-body-sm font-medium text-fg">Good to know</h3>
          <ul className="list-disc space-y-0.5 pl-5 text-caption text-fg-secondary">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}

function ItemList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <section className="grid gap-1" aria-label={title}>
      <h3 className="text-body-sm font-medium text-fg">{title}</h3>
      <ul className="space-y-0.5 text-body-sm text-fg-secondary">
        {items.map((item, index) => (
          <li key={`${item}:${index}`} className="mono">
            {item}
          </li>
        ))}
      </ul>
    </section>
  )
}
