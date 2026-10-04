import type { ReactNode } from 'react'
import { fieldErrorId } from '@/lib/fieldErrors'
import { cn } from '@/lib/utils'

/**
 * The one inline validation message under a control.
 *
 * Forms used to validate three ways: native browser bubbles (first field only,
 * gone on the next click), red text in some dialogs and amber text in others.
 * This is the red line the kit `Field` already renders, for forms that lay
 * their rows out by hand. Always `--danger`: red means "blocks Save"; amber
 * stays for advisories that do not.
 *
 * Link the control to it with `invalidAria(inputId, message)` from
 * `./validation` (sets `aria-invalid`, which draws the danger outline, and
 * `aria-describedby`). Renders nothing while `message` is empty.
 */
export function FieldError({
  inputId,
  id,
  message,
  announce = false,
  className,
}: {
  /** The control's DOM id; the message gets `${inputId}-error` (lib/fieldErrors). */
  inputId?: string
  /** An explicit id instead, when the control's `aria-describedby` already names one. */
  id?: string
  message: ReactNode
  /**
   * Announce the message as it appears (`role="alert"`). Leave off when an
   * error summary next to Save already announces every problem at once.
   */
  announce?: boolean
  className?: string
}) {
  if (message === undefined || message === null || message === false || message === '') return null
  return (
    <p
      id={id ?? (inputId !== undefined ? fieldErrorId(inputId) : undefined)}
      role={announce ? 'alert' : undefined}
      data-slot="field-error"
      className={cn('mt-1.5 text-body-sm leading-[1.45] text-(--danger)', className)}
    >
      {message}
    </p>
  )
}
