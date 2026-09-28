import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { platformApi, type PlatformOrg } from '@/api/platform'
import { useAuth } from '@/components/auth-context'
import { ErrorState } from '@/components/error-state'
import { FieldError } from '@/components/forms/FieldError'
import { invalidAria } from '@/components/forms/validation'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgHomePath } from '@/lib/navigation'
import { platformConsoleKey } from '@/lib/queryKeys'
import {
  REASON_MAX,
  STEP_IN_TTL_DEFAULT,
  STEP_IN_TTL_MAX,
  STEP_IN_TTL_MIN,
  reasonError,
  ttlError,
} from './platformModel'

function ReasonField({
  id,
  value,
  onChange,
  error,
  hint,
}: {
  id: string
  value: string
  onChange: (next: string) => void
  error: string | null
  hint: string
}) {
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>Reason</Label>
      <Textarea
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={3}
        maxLength={REASON_MAX}
        aria-required
        {...invalidAria(id, error)}
      />
      {error ? (
        <FieldError inputId={id} message={error} className="mt-0" />
      ) : (
        <p className="m-0 text-body-sm text-fg-tertiary">{hint}</p>
      )}
    </div>
  )
}

/**
 * Suspend an organization (F20). Its members are refused everywhere inside it
 * until it is reinstated; the reason is recorded in its audit log. The page
 * mounts it only while open, so closing it discards the draft.
 */
export function SuspendOrgDialog({ org, onClose }: { org: PlatformOrg; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [submitted, setSubmitted] = useState(false)
  const error = submitted ? reasonError(reason) : null

  const mutation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => platformApi.suspendOrg(org.slug, reason.trim()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: platformConsoleKey() })
      onClose()
    },
  })

  return (
    <Dialog open onOpenChange={(next) => { if (!next && !mutation.isPending) onClose() }}>
      <DialogContent>
        <form
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            setSubmitted(true)
            if (reasonError(reason) || mutation.isPending) return
            mutation.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Suspend {org.name}</DialogTitle>
            <DialogDescription>
              Every member is refused inside this organization until it is reinstated. Its data is
              kept. The reason is recorded in the organization&apos;s audit log.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            <ReasonField
              id="suspend-reason"
              value={reason}
              onChange={setReason}
              error={error}
              hint="Its owners can read this in their audit log."
            />
            {mutation.isError && (
              <ErrorState compact title="Could not suspend the organization" error={mutation.error} />
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={mutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="destructive" disabled={mutation.isPending}>
              {mutation.isPending ? 'Suspending…' : 'Suspend organization'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Start a read-only step-in (F20): the platform admin reads the organization as
 * a viewer of every project until the step-in expires or is ended. Nothing can
 * be changed. The reason and length are recorded in the organization's audit
 * log, where its owners see them.
 */
export function StepInDialog({ org, onClose }: { org: PlatformOrg; onClose: () => void }) {
  const auth = useAuth()
  const navigate = useNavigate()
  const [reason, setReason] = useState('')
  const [ttl, setTtl] = useState(String(STEP_IN_TTL_DEFAULT))
  const [submitted, setSubmitted] = useState(false)
  const reasonProblem = submitted ? reasonError(reason) : null
  const ttlProblem = submitted ? ttlError(ttl) : null

  const mutation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      platformApi.stepIn(org.slug, { reason: reason.trim(), ttl_minutes: Number(ttl) }),
    onSuccess: (stepIn) => {
      // The session now names the step-in; ask for it so the banner shows.
      auth.refresh()
      onClose()
      void navigate(orgHomePath(stepIn.org_slug || org.slug))
    },
  })

  return (
    <Dialog open onOpenChange={(next) => { if (!next && !mutation.isPending) onClose() }}>
      <DialogContent>
        <form
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            setSubmitted(true)
            if (reasonError(reason) || ttlError(ttl) || mutation.isPending) return
            mutation.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Step in to {org.name} (read-only)</DialogTitle>
            <DialogDescription>
              You will see this organization as a viewer of every project. Nothing can be changed
              while stepped in. The reason and length are recorded in its audit log, where its
              owners can see them.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            <ReasonField
              id="step-in-reason"
              value={reason}
              onChange={setReason}
              error={reasonProblem}
              hint="For example, the support ticket you are answering."
            />
            <div className="grid gap-2">
              <Label htmlFor="step-in-ttl">Length (minutes)</Label>
              <Input
                id="step-in-ttl"
                type="number"
                inputMode="numeric"
                min={STEP_IN_TTL_MIN}
                max={STEP_IN_TTL_MAX}
                step={1}
                value={ttl}
                onChange={(event) => setTtl(event.target.value)}
                className="w-32"
                aria-required
                {...invalidAria('step-in-ttl', ttlProblem)}
              />
              {ttlProblem ? (
                <FieldError inputId="step-in-ttl" message={ttlProblem} className="mt-0" />
              ) : (
                <p className="m-0 text-body-sm text-fg-tertiary">
                  {STEP_IN_TTL_MIN} to {STEP_IN_TTL_MAX} minutes. You can end it sooner.
                </p>
              )}
            </div>
            {mutation.isError && (
              <ErrorState compact title="Could not step in" error={mutation.error} />
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={mutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? 'Stepping in…' : 'Step in (read-only)'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
