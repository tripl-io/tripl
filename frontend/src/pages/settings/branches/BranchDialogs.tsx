import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { branchSettingsApi } from '@/api/branchSettings'
import { ErrorState } from '@/components/error-state'
import { ReadOnlyDefinition, ReadOnlyNotice } from '@/components/states'
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
import { Checkbox } from '@/components/ui/checkbox'
import { FieldError } from '@/components/forms/FieldError'
import { REQUIRED_MESSAGE, focusFirstInvalid, invalidAria } from '@/components/forms/validation'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { ownerOnlyReason, useIsOwner } from '@/lib/permissions'
import { getErrorMessage } from '@/lib/utils'
import type { ProjectBranchSettings } from '@/types'
import { parseMinApprovals } from './branchDiffModel'
import {
  BRANCH_NAME_HINT,
  branchNameProblem,
  suggestBranchName,
} from './branchMeta'
import { branchSettingsKey } from '@/lib/queryKeys'

interface MergePolicyDialogProps {
  slug: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function MergePolicyDialog({ slug, open, onOpenChange }: MergePolicyDialogProps) {
  const settingsQuery = useQuery({
    queryKey: branchSettingsKey(slug),
    queryFn: () => branchSettingsApi.get(slug),
    enabled: open,
    // Rendered in the dialog, with a retry, instead of "Loading policy…"
    // forever.
    meta: SILENT_ERROR_META,
  })
  const settings = settingsQuery.data

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Merge policy</DialogTitle>
        </DialogHeader>
        {settings ? (
          // Keyed by updated_at so a re-open after an external change re-seeds
          // the form from the fresh server state.
          <MergePolicyForm
            key={settings.updated_at ?? 'defaults'}
            slug={slug}
            settings={settings}
            onClose={() => onOpenChange(false)}
          />
        ) : settingsQuery.isError ? (
          <ErrorState
            compact
            className="my-4"
            title="Could not load the merge policy"
            error={settingsQuery.error}
            onRetry={() => void settingsQuery.refetch()}
          />
        ) : (
          // The two rows' shape, not a sentence (#237).
          <div role="status" className="space-y-3 py-4">
            <span className="sr-only">Loading the merge policy…</span>
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-4 w-40" />
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

interface MergePolicyFormProps {
  slug: string
  settings: ProjectBranchSettings
  onClose: () => void
}

function MergePolicyForm({ slug, settings, onClose }: MergePolicyFormProps) {
  const qc = useQueryClient()
  // The PATCH is OwnerUserDep; everyone else reads the policy, as the
  // sibling tracker dialog does.
  const canEdit = useIsOwner()
  const minApprovalsId = useId()
  const minApprovalsHintId = useId()
  const minApprovalsErrorId = useId()
  const blockSelfId = useId()
  const [minApprovals, setMinApprovals] = useState(String(settings.min_approvals))
  const [blockSelf, setBlockSelf] = useState(settings.block_self_approval)
  const parsedMinApprovals = parseMinApprovals(minApprovals)
  const minApprovalsInvalid = parsedMinApprovals === null

  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (min: number) =>
      branchSettingsApi.update(slug, {
        min_approvals: min,
        block_self_approval: blockSelf,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: branchSettingsKey(slug) })
      onClose()
    },
  })

  // Everyone but an owner reads the policy as values, not as a form of
  // disabled controls (#237).
  if (!canEdit) {
    return (
      <div className="grid gap-4 py-4">
        <ReadOnlyNotice>{ownerOnlyReason('change the merge policy')}</ReadOnlyNotice>
        <ReadOnlyDefinition
          items={[
            {
              label: 'Required approvals',
              value:
                settings.min_approvals === 0
                  ? 'None: a branch can merge without approval'
                  : String(settings.min_approvals),
            },
            {
              label: 'Block self-approval',
              value: settings.block_self_approval
                ? 'On: authors cannot approve their own branch'
                : 'Off',
            },
          ]}
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </div>
    )
  }

  return (
    <form
      noValidate
      onSubmit={(event) => {
        event.preventDefault()
        if (parsedMinApprovals !== null) saveMut.mutate(parsedMinApprovals)
      }}
    >
      <div className="grid gap-4 py-4">
        <div className="grid gap-2">
          <Label htmlFor={minApprovalsId}>Required approvals</Label>
          <Input
            id={minApprovalsId}
            type="number"
            inputMode="numeric"
            min={0}
            max={100}
            step={1}
            value={minApprovals}
            onChange={(event) => setMinApprovals(event.target.value)}
            aria-invalid={minApprovalsInvalid || undefined}
            aria-describedby={
              minApprovalsInvalid ? `${minApprovalsErrorId} ${minApprovalsHintId}` : minApprovalsHintId
            }
          />
          {minApprovalsInvalid ? (
            <p id={minApprovalsErrorId} className="text-body-sm text-danger">
              Enter a whole number from 0 to 100.
            </p>
          ) : null}
          <p id={minApprovalsHintId} className="text-body-sm text-fg-tertiary">
            Distinct approvals a branch needs before it can merge. 0 disables the quota.
          </p>
        </div>
        <div className="flex items-center justify-between gap-3">
          <div>
            <Label htmlFor={blockSelfId}>Block self-approval</Label>
            <p className="mt-1 text-body-sm text-fg-tertiary">
              Branch authors cannot approve their own branch.
            </p>
          </div>
          <Switch
            id={blockSelfId}
            checked={blockSelf}
            onCheckedChange={setBlockSelf}
          />
        </div>
        {saveMut.isError && (
          <p className="text-body text-danger">
            {getErrorMessage(saveMut.error)}
          </p>
        )}
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={saveMut.isPending || minApprovalsInvalid}>
          Save
        </Button>
      </DialogFooter>
    </form>
  )
}

interface CreateBranchDialogProps {
  open: boolean
  name: string
  description: string
  pending: boolean
  error: string | null
  /** Names already taken, checked before submit. */
  existingNames: readonly string[]
  /** "Switch to this branch now". */
  switchAfterCreate: boolean
  onSwitchAfterCreate: (value: boolean) => void
  onName: (value: string) => void
  onDescription: (value: string) => void
  onOpenChange: (open: boolean) => void
  onSubmit: () => void
}

export function CreateBranchDialog({
  open,
  name,
  description,
  pending,
  error,
  existingNames,
  switchAfterCreate,
  onSwitchAfterCreate,
  onName,
  onDescription,
  onOpenChange,
  onSubmit,
}: CreateBranchDialogProps) {
  const nameId = useId()
  const nameHintId = useId()
  const descriptionId = useId()
  const switchId = useId()
  // "Required" under an empty name once Create was pressed, instead of the
  // browser's bubble. A malformed or taken name is said while typing:
  // "Bad name with spaces!!" used to be accepted.
  const [submitted, setSubmitted] = useState(false)
  const nameProblem = branchNameProblem(name, existingNames)
  const nameError = nameProblem ?? (submitted && !name.trim() ? REQUIRED_MESSAGE : null)
  const suggestion = nameProblem ? suggestBranchName(name) : null
  const usableSuggestion =
    suggestion !== null && branchNameProblem(suggestion, existingNames) === null ? suggestion : null
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setSubmitted(false)
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-w-lg">
        <form
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            setSubmitted(true)
            if (!name.trim() || nameProblem) {
              const form = event.currentTarget
              requestAnimationFrame(() => focusFirstInvalid(form))
              return
            }
            onSubmit()
          }}
        >
          <DialogHeader>
            <DialogTitle>New branch</DialogTitle>
            {/* What is about to happen, before anyone commits to it. */}
            <DialogDescription>
              A branch is a private copy of the plan as it is now. Edit events on it, ask for a
              review, then merge to make the changes live.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor={nameId}>Name</Label>
              <Input
                id={nameId}
                aria-required
                value={name}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => onName(event.target.value)}
                placeholder="e.g. checkout/paywall-copy"
                className="mono"
                {...invalidAria(nameId, nameError)}
                aria-describedby={nameError ? `${nameId}-error ${nameHintId}` : nameHintId}
              />
              <FieldError inputId={nameId} message={nameError} />
              {usableSuggestion ? (
                <button
                  type="button"
                  onClick={() => onName(usableSuggestion)}
                  className="w-fit text-caption font-medium underline underline-offset-2 text-accent"
                >
                  Use <span className="mono">{usableSuggestion}</span>
                </button>
              ) : null}
              <p id={nameHintId} className="text-caption text-fg-tertiary">
                {BRANCH_NAME_HINT}
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor={descriptionId} optional>Description</Label>
              <Textarea
                id={descriptionId}
                value={description}
                rows={3}
                onChange={(event) => onDescription(event.target.value)}
                placeholder="What is this branch for?"
              />
            </div>
            <div className="flex items-center gap-2">
              <Checkbox
                id={switchId}
                checked={switchAfterCreate}
                onCheckedChange={(value) => onSwitchAfterCreate(value === true)}
              />
              <Label htmlFor={switchId} className="font-normal">
                Switch to this branch now
              </Label>
            </div>
            {error && <p role="alert" className="text-body text-danger">{error}</p>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={pending}>
              Create branch
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
