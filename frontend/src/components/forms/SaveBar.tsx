import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

const STATUS_COLOR = {
  muted: 'text-(--fg-subtle)',
  danger: 'text-(--danger)',
  warning: 'text-(--warning)',
  success: 'text-(--success)',
} as const

/**
 * The sticky action row of a long form: Cancel/Discard and
 * Save, plus one line saying why Save is blocked or what just happened.
 *
 * Forms were 2,500-3,600px tall with Save only at the bottom, so fixing one
 * field near the top meant scrolling the whole form again, and the blocking
 * reason next to the button was never on screen. Render it as the LAST child
 * of the form, inside the scrolling container; `sticky` keeps it pinned to the
 * viewport edge while the form scrolls under it and lets it settle in place at
 * the form's end.
 *
 * - `placement="bottom"` (default): authoring forms (event, metric, fact table,
 *   scan, field editor, bulk edit).
 * - `placement="top"`: settings pages, through the kit's SettingsSaveBar
 *   (Discard + Save changes). It clears SettingsLayout's 52px phone header.
 *   One save model per settings page: no per-card Save buttons beside it.
 *
 * Actions are the caller's `Button`s (`size="sm"` or default; the bar raises
 * them to 40px under `sm` for touch). Put the secondary action first.
 */
export function SaveBar({
  children,
  status,
  statusTone = 'muted',
  onStatusClick,
  error,
  placement = 'bottom',
  className,
}: {
  /** The actions, secondary first: `<Button variant="outline">Cancel</Button><Button>Save</Button>`. */
  children: ReactNode
  /**
   * One line on the left: the blocking reason ("Fill in: Name"), a count
   * ("3 fields need attention"), or a note ("Saved"). Lives in a polite live
   * region that is always mounted, so a change is announced.
   */
  status?: ReactNode
  /** `danger` for anything that blocks Save; `warning` only for advisories. */
  statusTone?: keyof typeof STATUS_COLOR
  /** Make the status a button, e.g. to `focusFirstInvalid(form)`. */
  onStatusClick?: () => void
  /** A failed save (the mutation's message). Announced as an alert. */
  error?: ReactNode
  placement?: 'bottom' | 'top'
  className?: string
}) {
  const hasStatus = status !== undefined && status !== null && status !== false && status !== ''
  return (
    <div
      data-slot="save-bar"
      data-placement={placement}
      className={cn(
        'sticky z-10 flex flex-wrap items-center gap-x-3 gap-y-2 py-3 backdrop-blur-sm',
        'bg-[color-mix(in_oklab,var(--bg)_95%,transparent)]',
        'max-sm:[&_[data-slot=button]]:h-10',
        placement === 'bottom'
          ? 'bottom-0 border-t border-(--border) pb-[max(0.75rem,env(safe-area-inset-bottom))]'
          : 'top-[52px] border-b border-(--border-subtle) md:top-0',
        className,
      )}
    >
      <div role="status" className={cn('min-w-0 flex-1 basis-48 text-body-sm leading-[1.5]', STATUS_COLOR[statusTone])}>
        {hasStatus &&
          (onStatusClick ? (
            <button
              type="button"
              onClick={onStatusClick}
              className="cursor-pointer text-left underline decoration-dotted underline-offset-2 hover:decoration-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--ring)"
            >
              {status}
            </button>
          ) : (
            status
          ))}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {error !== undefined && error !== null && error !== false && error !== '' && (
          <span role="alert" className="text-body-sm text-(--danger)">
            {error}
          </span>
        )}
        {children}
      </div>
    </div>
  )
}
