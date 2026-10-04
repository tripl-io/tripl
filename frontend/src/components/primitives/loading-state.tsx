import type { CSSProperties } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

type LoadingStateProps = {
  /** What assistive tech hears; also the visible text when `rows` is 0. */
  label?: string
  /** Skeleton rows instead of text — the table idiom. */
  rows?: number
  /** `span` where the state sits inside inline content. */
  as?: 'div' | 'span'
  className?: string
  style?: CSSProperties
}

/**
 * The one loading state: announced as a status, in the subtle caption
 * colour. Loading used to be plain "Loading…" text with no live region on most
 * surfaces, a spinner on some and skeletons on others. Panels use the text
 * form; tables pass `rows` for skeleton rows.
 */
export function LoadingState({
  label = 'Loading…',
  rows = 0,
  as: Tag = 'div',
  className,
  style,
}: LoadingStateProps) {
  if (rows > 0) {
    return (
      <div role="status" className={cn('flex flex-col gap-2', className)} style={style}>
        <span className="sr-only">{label}</span>
        {Array.from({ length: rows }, (_, index) => (
          <Skeleton key={index} className="h-4 w-full" />
        ))}
      </div>
    )
  }
  return (
    <Tag role="status" className={className} style={{ color: 'var(--fg-subtle)', ...style }}>
      {label}
    </Tag>
  )
}
