import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export type PageContainerWidth = 'full' | 'narrow'

type PageContainerProps = ComponentProps<'div'> & {
  /**
   * `full` (default): list, dashboard and detail pages, as wide as the shell.
   * `narrow`: forms and settings-style pages, capped at 880px. Both start at
   * the shell's left content edge, so every page lines up with the banner and
   * the top bar above it.
   */
  width?: PageContainerWidth
}

const WIDTH_CLASS: Record<PageContainerWidth, string> = {
  full: '',
  narrow: 'max-w-[880px]',
}

/**
 * The one page wrapper. The app shell (`Layout`) already pads
 * the page (12 / 20 / 32px), so a page adds no padding and no `mx-auto` of its
 * own: detail pages used to wrap themselves in `p-4 sm:p-6` or
 * `mx-auto max-w-[1000px] px-4 sm:px-6` and sat 16-24px further in than the
 * list pages beside them.
 *
 * It gives the list-page rhythm: 24px between the header and each section,
 * and 48px of room under the last one. `min-w-0` lets a wide table scroll
 * inside its card instead of stretching the page.
 */
export function PageContainer({ width = 'full', className, ...props }: PageContainerProps) {
  return (
    <div
      data-slot="page-container"
      data-width={width}
      className={cn('min-w-0 space-y-6 pb-12', WIDTH_CLASS[width], className)}
      {...props}
    />
  )
}
