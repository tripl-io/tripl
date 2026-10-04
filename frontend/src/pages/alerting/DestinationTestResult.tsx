import { AlertCircle, CheckCircle2, Loader2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { formatDateTime } from '@/lib/datetime'
import { cn, getErrorMessage } from '@/lib/utils'
import type { AlertDestinationTestResponse } from '@/types'

import { describeTestFailure } from './destinationCardLabels'

interface DestinationTestResultProps {
  pending: boolean
  /** The server's answer; a channel refusal is a 200 with `ok: false`. */
  result: AlertDestinationTestResponse | null
  /** A transport failure of the request itself, not of the channel. */
  error: unknown
  onDismiss: () => void
}

/**
 * The dialog's "Send test" outcome as one inline row: an icon, one
 * plain sentence, the transport's own words behind "Details", and Dismiss.
 * Same wording as the destination card's result, so a test reads the same
 * before and after saving.
 */
export function DestinationTestResult({ pending, result, error, onDismiss }: DestinationTestResultProps) {
  const failed = !pending && !result && !!error
  const refusal = !pending && result && !result.ok ? describeTestFailure(result.error, result) : null
  const tone = pending ? 'pending' : result?.ok ? 'ok' : 'failed'
  return (
    <div
      data-tone={tone}
      className={cn(
        'flex items-start gap-2 rounded-control border px-3 py-2 text-body-sm',
        tone === 'ok' && 'border-success/40 bg-success-soft',
        tone === 'failed' && 'border-destructive/40 bg-danger-soft',
      )}
    >
      {tone === 'pending' ? (
        <Loader2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 animate-spin text-fg-tertiary" />
      ) : tone === 'ok' ? (
        <CheckCircle2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-success" />
      ) : (
        <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-destructive" />
      )}
      <div className="min-w-0 flex-1 space-y-1">
        <p
          role={failed ? 'alert' : 'status'}
          className={tone === 'ok' ? 'text-success' : tone === 'pending' ? 'text-fg-tertiary' : 'text-destructive'}
        >
          {pending && 'Sending a test message…'}
          {!pending && result?.ok && (
            result.sent_at
              ? `Test message reached the channel at ${formatDateTime(result.sent_at)}.`
              : 'Test message reached the channel.'
          )}
          {refusal && (
            refusal.detail
              ? `Test message not delivered. ${refusal.summary}`
              : `The channel refused the test message: ${refusal.summary}`
          )}
          {failed && `Test send failed: ${getErrorMessage(error)}`}
        </p>
        {refusal?.detail && (
          <details className="text-caption text-fg-tertiary">
            <summary className="cursor-pointer">Details</summary>
            <p className="mt-1 whitespace-pre-wrap break-words font-mono">{refusal.detail}</p>
          </details>
        )}
      </div>
      {!pending && (
        <Button
          type="button"
          variant="ghost"
          size="xs"
          className="shrink-0"
          onClick={onDismiss}
          aria-label="Dismiss the test result"
        >
          Dismiss
        </Button>
      )}
    </div>
  )
}
