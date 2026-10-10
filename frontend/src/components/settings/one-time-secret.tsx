import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Copy } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useCopyToClipboard } from '@/hooks/useCopyToClipboard'
import { cn } from '@/lib/utils'

/** How long the Copy button says "Copied" before it offers to copy again. */
const COPIED_RESET_MS = 2000

export interface OneTimeSecretFieldProps {
  /** The value the server returns this once: a token, a secret, a link. */
  value: string
  /** The field's accessible name, and what the status line says was copied. */
  label: string
  /** What the failed-copy line calls the value: "The {noun} above is selected". */
  noun: string
  /** Hears every copy, by the button or by hand with Ctrl/⌘+C. */
  onCopied?: () => void
  className?: string
}

/**
 * A value the server shows once (an API key, a SCIM token, a signing secret,
 * an invite or password reset link): read-only, selected on focus, with a Copy
 * button. With no clipboard (a self-hosted instance on plain HTTP has none) or
 * a refused write, the value is selected for Ctrl/⌘+C and the field says so.
 *
 * "Copied" lasts {@link COPIED_RESET_MS}, so a second click can tell whether it
 * worked again. Anything that must remember a copy past that, such as a "Done"
 * button or a "nobody copied this" warning, listens to `onCopied`. Mount it
 * with `key={value}` so a new value starts out uncopied.
 */
export function OneTimeSecretField({ value, label, noun, onCopied, className }: OneTimeSecretFieldProps) {
  const ref = useRef<HTMLInputElement>(null)
  const { state, copy, reset } = useCopyToClipboard(ref)

  useEffect(() => {
    if (state !== 'copied') return
    const timer = window.setTimeout(reset, COPIED_RESET_MS)
    return () => window.clearTimeout(timer)
  }, [state, reset])

  return (
    <div className={cn('space-y-1.5', className)}>
      <div className="flex items-center gap-2">
        <Input
          ref={ref}
          readOnly
          aria-label={label}
          value={value}
          onFocus={(event) => event.currentTarget.select()}
          // A manual Ctrl/⌘+C (the no-clipboard path) is a copy too.
          onCopy={onCopied}
          className="mono flex-1 text-caption"
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            void copy(value).then((copied) => {
              if (copied) onCopied?.()
            })
          }}
        >
          <Copy aria-hidden="true" className="size-3.5" />
          {state === 'copied' ? 'Copied' : 'Copy'}
        </Button>
      </div>
      {/* Mounted before anything is copied, so the line is announced: the
          button's label change alone never was. */}
      <p role="status" className="sr-only">
        {state === 'copied' ? `${label} copied to the clipboard.` : ''}
      </p>
      {state === 'failed' && (
        <p role="alert" className="m-0 text-caption text-danger">
          Couldn’t reach the clipboard. The {noun} above is selected — press Ctrl/⌘+C to copy it.
        </p>
      )}
    </div>
  )
}

export interface OneTimeSecretDialogProps {
  /** The value to reveal; null keeps the dialog closed. */
  secret: string | null
  title: string
  description: ReactNode
  /** The field's accessible name ("API key"). */
  label: string
  /** What the failed-copy line calls the value ("key"). */
  noun: string
  /** Under the description: which credential this is, while the overlay hides its row. */
  details?: ReactNode
  /** Under the field: how to use the value. */
  children?: ReactNode
  /** The footer button, the only way out. */
  onDone: () => void
}

/**
 * The reveal-once dialog for a freshly minted credential. Only its footer
 * button closes it: Esc or a stray click outside would discard a value the
 * server never returns again. The button says "I’ve saved it" until the value
 * has been copied once, and "Done" from then on.
 */
export function OneTimeSecretDialog({
  secret,
  title,
  description,
  label,
  noun,
  details,
  children,
  onDone,
}: OneTimeSecretDialogProps) {
  // The secret that was copied, not a flag: a new secret starts out uncopied
  // without a reset, and the field's own "Copied" can time out without
  // turning "Done" back into "I’ve saved it".
  const [copiedSecret, setCopiedSecret] = useState<string | null>(null)
  const copied = secret != null && copiedSecret === secret

  return (
    <Dialog open={secret != null}>
      <DialogContent
        showCloseButton={false}
        onEscapeKeyDown={(event) => event.preventDefault()}
        onInteractOutside={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
          {details}
        </DialogHeader>
        {secret != null && (
          <div className="space-y-2 py-2">
            <OneTimeSecretField
              key={secret}
              value={secret}
              label={label}
              noun={noun}
              onCopied={() => setCopiedSecret(secret)}
            />
            {children}
          </div>
        )}
        <DialogFooter>
          <Button onClick={onDone}>
            {/* Until a copy has worked, closing is a claim the reader makes
                about a value that is never shown again. */}
            {copied ? 'Done' : 'I’ve saved it'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
