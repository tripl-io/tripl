import type { ReactNode } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

/**
 * Loading placeholders shaped like what is coming (#237 group).
 *
 * A route or section used to load as one grey sentence ("Loading page…") in an
 * empty column, and the page then popped in and pushed everything down. These
 * draw the header, stat strip, table or form the page will have, so the layout
 * does not jump when the data lands. Each is ONE `role="status"` region with a
 * screen-reader label; the bars themselves are `aria-hidden`.
 */

type StatusProps = {
  /** What assistive tech hears. Also the region's accessible name. */
  label: string
  className?: string
  children: ReactNode
}

function Status({ label, className, children }: StatusProps) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className={className}>
      <span className="sr-only">{label}</span>
      {children}
    </div>
  )
}

/** Eyebrow + h1 bar (+ optional description line): PageHeader's footprint. */
function HeaderBars({ description = true }: { description?: boolean }) {
  return (
    <div className="space-y-2">
      <Skeleton className="h-2.5 w-16" />
      <Skeleton className="h-7 w-48" />
      {description && <Skeleton className="h-3 w-full max-w-md" />}
    </div>
  )
}

/** The boxed MiniStatStrip footprint: `count` label-over-figure pairs. */
function StatStripBars({ count = 4 }: { count?: number }) {
  return (
    <div className="flex flex-wrap gap-x-8 gap-y-3 rounded-card border border-border bg-bg-sunken px-4 py-3">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="space-y-1.5">
          <Skeleton className="h-2.5 w-14" />
          <Skeleton className="h-4 w-10" />
        </div>
      ))}
    </div>
  )
}

/** A Panel/Card: a header bar over `rows` list rows. */
function CardBars({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('overflow-hidden rounded-card border border-border bg-surface', className)}>
      <div className="border-b border-border-subtle px-4 py-3">
        <Skeleton className="h-3.5 w-32" />
      </div>
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          className="flex h-(--row-h) items-center gap-4 border-b border-border-subtle px-4 last:border-b-0"
        >
          <Skeleton className="h-3 w-1/3" />
          <Skeleton className="h-3 w-1/5" />
          <Skeleton className="ml-auto h-3 w-12" />
        </div>
      ))}
    </div>
  )
}

/** Label-over-control rows, the FormRow footprint (34px controls). */
function FormBars({ rows = 5 }: { rows?: number }) {
  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface">
      <div className="border-b border-border-subtle px-4 py-3">
        <Skeleton className="h-3.5 w-28" />
      </div>
      <div className="space-y-4 p-4">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="space-y-1.5">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-8 w-full" />
          </div>
        ))}
      </div>
    </div>
  )
}

export type PageSkeletonVariant = 'list' | 'dashboard' | 'detail' | 'form' | 'settings'

/**
 * A whole page while its route chunk or first query loads. Pick the variant
 * by the page's shape:
 * - `list` (default): header, stat strip, a table card — Events, Metrics,
 *   Anomalies, Scans, Coverage, Reconciliation.
 * - `dashboard`: header, stat strip, a two-column grid of cards — Overview.
 * - `detail`: header, stat strip, a chart block and a card — monitoring,
 *   metric, scan and monitor detail pages.
 * - `form`: header over a narrow (880px) form card — create/edit pages.
 * - `settings`: header over two setting cards — project settings tabs,
 *   settings-area sections.
 */
export function PageSkeleton({
  variant = 'list',
  label = 'Loading page…',
  className,
}: {
  variant?: PageSkeletonVariant
  label?: string
  className?: string
}) {
  return (
    <Status
      label={label}
      className={cn(
        'min-w-0 space-y-6 pb-12',
        (variant === 'form' || variant === 'settings') && 'max-w-[880px]',
        className,
      )}
    >
      <HeaderBars description={variant !== 'detail'} />
      {variant === 'list' && (
        <>
          <StatStripBars />
          <CardBars rows={8} />
        </>
      )}
      {variant === 'dashboard' && (
        <>
          <StatStripBars count={5} />
          <div className="grid gap-4 lg:grid-cols-2">
            <ChartBlock height={180} />
            <CardBars rows={5} />
            <CardBars rows={4} />
            <CardBars rows={4} />
          </div>
        </>
      )}
      {variant === 'detail' && (
        <>
          <StatStripBars />
          <ChartBlock height={240} />
          <CardBars rows={4} />
        </>
      )}
      {variant === 'form' && <FormBars rows={5} />}
      {variant === 'settings' && (
        <>
          <FormBars rows={3} />
          <FormBars rows={2} />
        </>
      )}
    </Status>
  )
}

export type SectionSkeletonVariant = 'table' | 'cards' | 'form' | 'chart' | 'list' | 'rows'

/**
 * One section of a page (a tab body, a panel) while its chunk or query loads.
 * Lives under the page's own header, which should stay rendered outside the
 * Suspense boundary.
 * - `table`: stat strip + `rows` table rows (default 3) — Monitors, Scans list.
 * - `list`: `rows` rows in one card, no stat strip — rosters, audit, history.
 * - `cards`: two cards side by side — Destinations, Inbox.
 * - `form`: one form card with `rows` fields — a settings section.
 * - `chart`: one chart-shaped block — see also {@link ChartSkeleton}.
 * - `rows`: `rows` table rows (default 4) with NO card of their own, for a
 *   table that already sits inside a Panel — metrics, fact tables, scan runs,
 *   Coverage and Reconciliation (#237). The panel keeps its height
 *   instead of growing from one grey sentence when the rows land.
 */
export function SectionSkeleton({
  variant = 'list',
  rows,
  label = 'Loading…',
  className,
}: {
  variant?: SectionSkeletonVariant
  rows?: number
  label?: string
  className?: string
}) {
  return (
    <Status label={label} className={cn('space-y-4', className)}>
      {variant === 'table' && (
        <>
          <StatStripBars count={3} />
          <CardBars rows={rows ?? 3} />
        </>
      )}
      {variant === 'list' && <CardBars rows={rows ?? 5} />}
      {variant === 'cards' && (
        <div className="grid gap-4 md:grid-cols-2">
          <CardBars rows={rows ?? 3} />
          <CardBars rows={rows ?? 3} />
        </div>
      )}
      {variant === 'form' && <FormBars rows={rows ?? 4} />}
      {variant === 'chart' && <ChartBlock height={220} />}
      {variant === 'rows' && <RowBars rows={rows ?? 4} />}
    </Status>
  )
}

/**
 * Card-less table rows: a name bar over a caption bar, a value, a sparkline
 * block and a chip, each row ruled like the table it stands in for.
 */
function RowBars({ rows }: { rows: number }) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="flex items-center gap-4 border-b border-border-subtle px-4 py-3 last:border-0"
        >
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-40 max-w-full" />
            <Skeleton className="h-3 w-64 max-w-full" />
          </div>
          <Skeleton className="hidden h-3.5 w-14 sm:block" />
          <Skeleton className="hidden h-6 w-20 md:block" />
          <Skeleton className="h-5 w-16 rounded-full" />
        </div>
      ))}
    </div>
  )
}

/** Axis lines under a pulsing area at the chart's height. */
function ChartBlock({ height }: { height: number }) {
  return (
    <div
      className="relative overflow-hidden rounded-card border border-border bg-surface p-4"
      style={{ height }}
    >
      <Skeleton className="absolute inset-x-10 bottom-8 top-4 rounded-sm opacity-60" />
      {/* y axis + x axis */}
      <div className="absolute bottom-8 left-10 top-4 w-px bg-border" aria-hidden="true" />
      <div className="absolute bottom-8 left-10 right-4 h-px bg-border" aria-hidden="true" />
      <div className="absolute bottom-3 left-10 right-4 flex justify-between" aria-hidden="true">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} className="h-2 w-8" />
        ))}
      </div>
    </div>
  )
}

/**
 * A chart that is still loading (its lazy chunk or its series). Replaces the
 * centred "Loading…" / "Loading metrics…" word in a blank box. Pass the
 * chart's own height so nothing moves when it renders.
 */
export function ChartSkeleton({
  height = 220,
  label = 'Loading chart…',
  className,
}: {
  height?: number
  label?: string
  className?: string
}) {
  return (
    <Status label={label} className={className}>
      <ChartBlock height={height} />
    </Status>
  )
}

/**
 * The value slot of a MiniStat whose data has not arrived.
 * A KPI strip must never print "0" or "quiet" before it knows: that is a false
 * all-clear on a monitoring product. Pass it as `value` and omit `delta` and
 * `tone` while pending:
 *
 *   <MiniStat label="Total" value={isPending ? <StatValueSkeleton /> : total} />
 *
 * Inline (`span`) so it sits in the `<dd>`; the strip around it announces the
 * loading, so it is silent itself.
 */
export function StatValueSkeleton({ className }: { className?: string }) {
  return (
    <span
      data-slot="stat-skeleton"
      aria-hidden="true"
      className={cn(
        'inline-block h-4 w-8 animate-pulse rounded-sm bg-surface-hover align-middle motion-reduce:animate-none',
        className,
      )}
    />
  )
}

/**
 * The full-viewport stand-in used before the app shell exists (session check,
 * the settings takeover's first chunk): a rail block and a page skeleton, so a
 * cold load does not show one grey word on a blank screen.
 */
export function ShellSkeleton({ label = 'Loading…' }: { label?: string }) {
  return (
    <div
      className="flex h-screen overflow-hidden bg-background supports-[height:100dvh]:h-dvh"
      data-slot="shell-skeleton"
    >
      <div
        aria-hidden="true"
        className="hidden w-60 shrink-0 flex-col gap-3 border-r border-border p-4 lg:flex"
      >
        <Skeleton className="h-7 w-32" />
        <Skeleton className="mt-4 h-3 w-40" />
        <Skeleton className="h-3 w-36" />
        <Skeleton className="h-3 w-40" />
        <Skeleton className="h-3 w-28" />
      </div>
      <div className="min-w-0 flex-1 p-3 sm:p-5 lg:p-8">
        <PageSkeleton variant="settings" label={label} />
      </div>
    </div>
  )
}
