import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

type PageHeaderProps = {
  title: ReactNode
  /**
   * Small uppercase label above the title. One rule:
   * on a list or dashboard page it is the sidebar nav group ("Plan",
   * "Observe", "Govern", "Settings", "Help & reference"); on a detail or
   * create page it is the group and the parent collection ("Observe · Metric",
   * "Plan · Event"). It is never the project name: the top bar's breadcrumb
   * already carries that.
   */
  eyebrow?: string
  /** A count shown after the title in the muted colour ("Events 17"). */
  count?: ReactNode
  /** Inline after the title and count: badges, identity chips. */
  titleAddon?: ReactNode
  description?: ReactNode
  /** Right-hand slot: buttons. Wraps under the title on phones. */
  actions?: ReactNode
  /**
   * The page's KPI row, under the title block: a `MiniStatStrip` (usually
   * `boxed`). One place for page stats instead of the header's right slot on
   * one page and a loose row of tiles on the next.
   */
  stats?: ReactNode
  /** Above everything, e.g. a back link. */
  back?: ReactNode
  className?: string
}

/**
 * The one page header: one type scale for the
 * eyebrow, the title and the description, so moving between pages, create
 * forms and detail views no longer jumps between 21, 19, 18 and 16px titles.
 *
 * Every routed page renders exactly one, and it is the page's only `<h1>`:
 * sections below it are h2 (`Panel`, `SCard`, `CardTitle as="h2"`). A page
 * that used an `h2 text-lg` or an icon + `text-base` title swaps it for this.
 *
 * Scale: eyebrow 10.5px uppercase, title `text-title` (21px) semibold,
 * description 12.5px, all captions in `--fg-subtle`. The header carries no
 * outer margin; the page's own vertical rhythm (`PageContainer`) spaces it.
 */
export function PageHeader({
  title,
  eyebrow,
  count,
  titleAddon,
  description,
  actions,
  back,
  stats,
  className,
}: PageHeaderProps) {
  return (
    <div className={cn('flex flex-col gap-3', className)}>
      {back && <div className="flex">{back}</div>}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0 flex-1 basis-60">
          {eyebrow && (
            <div
              data-slot="page-eyebrow"
              className="micro-label text-fg-tertiary"
            >
              {eyebrow}
            </div>
          )}
          <div className={cn('flex flex-wrap items-center gap-x-2.5 gap-y-1', eyebrow && 'mt-1')}>
            <h1 className="m-0 min-w-0 break-words text-title font-semibold tracking-[-0.01em]">
              {title}
              {count != null && (
                <>
                  {' '}
                  <span className="tnum font-normal text-fg-tertiary">
                    {count}
                  </span>
                </>
              )}
            </h1>
            {titleAddon}
          </div>
          {description && (
            <div
              className="mt-1.5 max-w-[640px] text-body-sm leading-[1.5] text-fg-tertiary"
            >
              {description}
            </div>
          )}
        </div>
        {actions && <div className="flex max-w-full flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {stats && <div data-slot="page-stats">{stats}</div>}
    </div>
  )
}
