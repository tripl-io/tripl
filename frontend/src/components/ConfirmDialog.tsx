import { Suspense, useRef, type ReactNode } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { ConfirmDialogMessage } from './ConfirmDialogMessage'

// The typed / in-place-pending variant is rare (deleting a project) and this
// dialog sits on the first load through useConfirm, so its body is its own chunk.
const ConfirmDialogGuardedContent = lazyWithReload(() => import('./ConfirmDialogGuardedContent'))

interface Props {
  open: boolean
  title: string
  /** Plain text, or rich content (emphasis, a list) for the body. */
  message: ReactNode
  confirmLabel?: string
  /**
   * The safe answer's label. Defaults to "Cancel"; a leave-without-saving
   * confirm names it "Keep editing", so the form's own Cancel does not meet a
   * second Cancel that means the opposite.
   */
  cancelLabel?: string
  variant?: 'danger' | 'primary'
  onConfirm: () => void
  onCancel: () => void
  /**
   * Typed confirmation: Confirm arms only once exactly this text is typed —
   * for the actions that cannot be undone.
   */
  requireText?: string
  /**
   * Confirm does not close the dialog: the caller runs the action and closes
   * it on success, meanwhile `pending` and `error` render in place.
   */
  stayOpen?: boolean
  pending?: boolean
  /** The failed attempt's error, shown under the message. */
  error?: unknown
  /** Lead-in for the error line, e.g. "Could not delete the project". */
  errorPrefix?: string
  /** Confirm's label while `pending`. */
  pendingLabel?: string
}

export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'danger',
  onConfirm,
  onCancel,
  requireText,
  stayOpen = false,
  pending = false,
  error,
  errorPrefix,
  pendingLabel,
}: Props) {
  // Set by Confirm for the close its own click triggers. AlertDialogAction
  // closes the dialog, and that close reaches `onOpenChange(false)` right
  // AFTER `onConfirm` — which used to run `onCancel` as well, so any caller with
  // a side effect on cancel had both run for one confirm.
  const confirming = useRef(false)
  if (requireText !== undefined || stayOpen) {
    return (
      <AlertDialog
        open={open}
        onOpenChange={v => {
          // Esc and the overlay come through here too; a request in flight is
          // not abandoned by closing the dialog over it.
          if (!v && !pending) onCancel()
        }}
      >
        {open && (
          <Suspense fallback={null}>
            <ConfirmDialogGuardedContent
              title={title}
              message={message}
              confirmLabel={confirmLabel}
              cancelLabel={cancelLabel}
              variant={variant}
              requireText={requireText}
              pending={pending}
              error={error}
              errorPrefix={errorPrefix}
              pendingLabel={pendingLabel}
              onConfirm={onConfirm}
            />
          </Suspense>
        )}
      </AlertDialog>
    )
  }
  return (
    <AlertDialog
      open={open}
      onOpenChange={v => {
        if (v) return
        // Cancel, Esc and the overlay all arrive here, once each; Cancel no
        // longer calls onCancel itself as well.
        if (confirming.current) confirming.current = false
        else onCancel()
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <ConfirmDialogMessage message={message} />
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{cancelLabel}</AlertDialogCancel>
          <AlertDialogAction
            onClick={() => {
              confirming.current = true
              onConfirm()
            }}
            variant={variant === 'danger' ? 'destructive' : 'default'}
          >
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

