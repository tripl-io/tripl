import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import type { OffsetPaging } from '@/hooks/useOffsetPaging'
import { cn } from '@/lib/utils'

interface OffsetPagerProps {
  /** Where the list is, as useOffsetPaging reads it. */
  paging: Pick<OffsetPaging, 'hasPrev' | 'hasNext' | 'isPaging'>
  /** The range line: "Showing 51–100 of 254 entries." */
  caption: ReactNode
  /** "Newer" / "Older" on a newest-first log, "Previous" / "Next" on a list. */
  prevLabel: string
  nextLabel: string
  onPrev: () => void
  onNext: () => void
  /** Names the pager for assistive tech: "Audit log pages". */
  label: string
  className?: string
  /** On the two buttons, e.g. a taller tap target on phones. */
  buttonClassName?: string
}

/**
 * The range line and the two paging buttons under an offset-paged list.
 *
 * The rows do not change while a page is in flight (the query keeps the
 * previous page on screen), so the click gets an "Updating…" word, and both
 * buttons are held shut for that window: a second click moved the query key
 * again and the page in flight was dropped unrendered — 0 → 50 → 100, with
 * rows 51–100 never shown. When to show the pager at all is the caller's
 * call.
 */
export function OffsetPager({
  paging,
  caption,
  prevLabel,
  nextLabel,
  onPrev,
  onNext,
  label,
  className,
  buttonClassName,
}: OffsetPagerProps) {
  return (
    <nav aria-label={label} className={cn('flex flex-wrap items-center justify-between gap-2', className)}>
      <p className="text-body-sm text-fg-tertiary">{caption}</p>
      <div className="flex items-center gap-2">
        {paging.isPaging && <span className="text-body-sm text-fg-tertiary">Updating…</span>}
        <Button
          type="button"
          variant="outline"
          size="sm"
          className={buttonClassName}
          disabled={!paging.hasPrev || paging.isPaging}
          onClick={onPrev}
        >
          {prevLabel}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className={buttonClassName}
          disabled={!paging.hasNext || paging.isPaging}
          onClick={onNext}
        >
          {nextLabel}
        </Button>
      </div>
    </nav>
  )
}
