import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

export type ShellBannerTone = 'warning' | 'accent'

/** Each tone's fill, and the ink its icon takes on that fill. */
const TONE: Record<ShellBannerTone, { background: string; ink: string }> = {
  warning: { background: 'var(--warning-soft)', ink: 'text-warning' },
  accent: { background: 'var(--accent-soft)', ink: 'text-accent' },
}

/**
 * One strip across the app shell, in the banner slot under the top bar: an
 * icon, one line of text, and an optional action at its end. The public demo
 * notice uses it, and so can an extension's banner. `role` is `status` for a
 * state the reader is in, `note` for standing information.
 */
export function ShellBanner({
  tone,
  icon: Icon,
  role = 'status',
  action,
  children,
  'data-testid': testId,
}: {
  tone: ShellBannerTone
  icon: LucideIcon
  role?: 'status' | 'note'
  /** After the text, at the end of the line; wraps under it on a narrow screen. */
  action?: ReactNode
  children: ReactNode
  'data-testid'?: string
}) {
  return (
    <div
      role={role}
      data-testid={testId}
      className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-body-sm"
      style={{ background: TONE[tone].background, borderColor: 'var(--border)', color: 'var(--fg)' }}
    >
      <Icon aria-hidden="true" className={cn('size-4 shrink-0', TONE[tone].ink)} />
      <span className="min-w-0 flex-1">{children}</span>
      {action}
    </div>
  )
}
