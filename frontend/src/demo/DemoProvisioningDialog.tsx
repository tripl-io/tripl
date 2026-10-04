/**
 * Demo-provisioning progress + outcome dialog.
 *
 * Shows estimated staged progress during the single blocking create and an
 * inline, human error with a Retry action on 500. Accessibility:
 *  - Radix Dialog traps focus and restores it on close;
 *  - an `aria-live` region announces the current phase and the final result;
 *  - every terminal state (success, cancelled, failed) says so visibly and
 *    offers a positive action — never a disabled Cancel under a title that
 *    still claims work is in progress.
 *
 * A create in flight is abandonable, and cancelling is honest about what the
 * server could do: the phase list is labelled an estimate rather than asserting
 * completed work the client cannot observe, and a cancel that
 * arrived too late says the demo is going to appear.
 */

import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { getErrorMessage } from '@/lib/utils'
import { ProvisioningPhaseList } from './ProvisioningPhaseList'
import { DEMO_PROVISION_ESTIMATE, PROVISIONING_PHASES } from './provisioningPhases'
import type { CancelOutcome, ProvisioningStatus } from './useDemoProvisioning'

interface DemoProvisioningDialogProps {
  status: ProvisioningStatus
  phaseIndex: number
  error: unknown
  timedOut: boolean
  cancelOutcome?: CancelOutcome | null
  onRetry: () => void
  onCancel: () => void
  onClose: () => void
}

/**
 * A create the server refused before seeding anything (DEMO-5): a 403 (demo
 * provisioning switched off on this server, or a role that may not create), or
 * a 409 for a creator already at the demo limit. Neither was rolled back —
 * nothing started — and asking again gets the same answer, so neither may
 * claim a rollback or offer "Try again". The 403 has more than one cause, so
 * its copy names none and the server's own reason (in the alert) says which.
 * The other 409, a create cancelled from another tab, never reaches here:
 * useDemoProvisioning reports it as cancelled.
 */
type Refusal = 'forbidden' | 'limit'

function refusalOf(error: unknown): Refusal | null {
  if (!(error instanceof ApiError)) return null
  if (error.status === 403) return 'forbidden'
  if (error.status === 409) return 'limit'
  return null
}

/**
 * A failure that says nothing about what the server did (DEMO-5): the backend
 * could not be reached (the client maps a network error to 503), or a gateway
 * gave up on it (502/504) while the app behind may still be seeding. Only the
 * app's own 500 is its rollback — anything here may well have left a demo
 * behind, so the copy sends the user to the list before another attempt.
 */
function isUnreachable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true
  return error.status === 0 || error.status === 502 || error.status === 503 || error.status === 504
}

/** Title + description for each state, so no state falls through to in-progress copy. */
function copyFor(
  status: ProvisioningStatus,
  timedOut: boolean,
  cancelOutcome: CancelOutcome | null,
  refusal: Refusal | null,
  unreachable: boolean,
): { title: string; description: string } {
  if (status === 'error' && refusal === 'forbidden') {
    return {
      title: 'Demo workspace not available',
      description: 'The server refused to create one, for the reason below. Nothing was created.',
    }
  }
  if (status === 'error' && refusal === 'limit') {
    return {
      title: 'Demo limit reached',
      description: "Nothing was created. Reset or delete one of your demos from its banner's Manage demo menu first.",
    }
  }
  if (status === 'error') {
    return {
      title: 'Demo generation failed',
      description: timedOut
        ? // Honesty: a timeout aborts OUR request; the server may well be
          // seeding still. Promising a rollback here would be a lie.
          'The request took too long and was stopped. The demo may still be finishing on the server — check your projects list before creating another.'
        : unreachable
          ? // Same honesty for a lost connection: the server may have
            // accepted the request and gone on to finish it.
            'The server could not be reached, so the result is unknown. The demo may still be created — check your projects list before trying again.'
          : 'Nothing was left behind — the partial demo was rolled back. You can try again.',
    }
  }
  if (status === 'success') {
    return {
      title: 'Demo workspace is ready',
      description: 'Seeded with synthetic events, metrics and alert rules. Opening it now.',
    }
  }
  if (status === 'cancelling') {
    return {
      title: 'Cancelling demo generation',
      description: 'Asking the server to stop before the workspace is created…',
    }
  }
  if (status === 'cancelled') {
    if (cancelOutcome === 'stopped') {
      return {
        title: 'Demo generation cancelled',
        description: 'The workspace was discarded — nothing was added to your projects.',
      }
    }
    if (cancelOutcome === 'already-finished') {
      return {
        title: 'Too late to cancel',
        description:
          "The demo had already finished generating on the server, so it will appear in your projects list. Delete it from its banner's Manage demo menu if you do not want it.",
      }
    }
    // The server found nothing still seeding — the create may never have
    // reached it, or may have just finished (DEMO-28). Say only that: the
    // likelier case is a finished demo, so a title claiming it "stopped" told
    // the user the opposite of what happened.
    return {
      title: 'Nothing left to cancel',
      description:
        "The server had nothing left to cancel. If the demo finished first it is in your projects list — delete it from its banner's Manage demo menu if you do not want it.",
    }
  }
  return {
    title: 'Generating demo workspace',
    description: `Seeding a fully-populated workspace with synthetic data. This takes ${DEMO_PROVISION_ESTIMATE}.`,
  }
}

export function DemoProvisioningDialog({
  status,
  phaseIndex,
  error,
  timedOut,
  cancelOutcome = null,
  onRetry,
  onCancel,
  onClose,
}: DemoProvisioningDialogProps) {
  const open = status !== 'idle'
  const isProvisioning = status === 'provisioning'
  const isError = status === 'error'
  const isSuccess = status === 'success'
  const showPhases = !isError && status !== 'cancelled'

  const errorMessage = getErrorMessage(error)
  // A support reference the user can quote. The backend echoes the request id on
  // the response header, so ApiError carries it for the demo 500 path.
  const requestId = error instanceof ApiError ? error.requestId : undefined
  const refusal = isError ? refusalOf(error) : null
  const unreachable = isError && !timedOut && refusal === null && isUnreachable(error)
  const { title, description } = copyFor(status, timedOut, cancelOutcome, refusal, unreachable)
  // The server may have finished the create (a timeout, a lost connection):
  // the copy sends the user to the projects list, refreshed behind this
  // dialog, so the dialog must not offer a one-click duplicate that counts
  // towards the demo cap. A retry is the create button, after a look.
  const outcomeUnknown = isError && refusal === null && (timedOut || unreachable)
  const offerRetry = isError && refusal === null && !outcomeUnknown

  // On failure the role="alert" block below is the single live announcer, so the
  // polite status region stays silent — otherwise a screen reader reads the same
  // "failed" sentence twice (once assertive via the alert, once polite here).
  const announcement = isError
    ? null
    : isProvisioning
      ? `Generating demo workspace — ${PROVISIONING_PHASES[phaseIndex]?.label ?? 'Working'}`
      : `${title}. ${description}`

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next) return
        // A create in flight is abandonable, not un-dismissable: a stalled
        // connection used to leave a page reload as the only way out.
        // Escape / the close button / an outside click cancel
        // the request; anything else just closes.
        if (isProvisioning) onCancel()
        else onClose()
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        {/* Polite live region for progress + terminal outcomes; the failure path
            is announced once by the role="alert" block below (never both). */}
        {announcement !== null && (
          <p className="sr-only" aria-live="polite" role="status">
            {announcement}
          </p>
        )}

        {isError ? (
          <div
            role="alert"
            className="space-y-1 rounded-lg border px-3 py-2.5 text-body-sm leading-[1.45] bg-danger-soft border-danger text-fg"
          >
            <p>{errorMessage}</p>
            {requestId ? (
              // --fg-subtle, not --fg-faint: faint falls below AA on the
              // tinted --danger-soft fill (DEMO-24).
              <p className="font-mono text-caption text-fg-tertiary">
                Reference: {requestId}
              </p>
            ) : null}
          </div>
        ) : showPhases ? (
          <ProvisioningPhaseList phaseIndex={phaseIndex} complete={isSuccess} />
        ) : null}

        <DialogFooter>
          {isError ? (
            <>
              <Button type="button" variant={offerRetry ? 'outline' : 'default'} onClick={onClose}>
                Close
              </Button>
              {offerRetry && (
                <Button type="button" onClick={onRetry}>
                  Try again
                </Button>
              )}
            </>
          ) : isProvisioning || status === 'cancelling' ? (
            <Button
              type="button"
              variant="outline"
              onClick={onCancel}
              disabled={status === 'cancelling'}
            >
              {status === 'cancelling' ? 'Cancelling…' : 'Cancel'}
            </Button>
          ) : (
            <Button type="button" onClick={onClose}>
              {isSuccess ? 'Open demo' : 'Done'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
