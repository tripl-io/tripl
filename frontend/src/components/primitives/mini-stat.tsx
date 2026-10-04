import { Children, type CSSProperties, type ReactNode } from 'react'
import { Dot, type DotTone } from '@/components/primitives/dot'
import { cn } from '@/lib/utils'

export type MiniStatTone = 'success' | 'danger' | 'warning' | 'info' | 'accent' | 'neutral'

type MiniStatProps = {
  label: string
  value: ReactNode
  delta?: ReactNode
  /**
   * Colours the `delta` (and its pulse dot). With no delta to ride on it
   * colours the figure instead: it used to show nothing at all, so Overview's
   * Implemented / Needs review / Coverage, the Coverage page and others passed
   * a tone that read as meaningful at the call site and never rendered.
   */
  tone?: MiniStatTone
  /**
   * Colours the figure itself, whether or not there is a delta — for a stat
   * whose delta carries a different tone from its value.
   */
  valueTone?: MiniStatTone
  pulse?: boolean
  /**
   * Rendered inline right after the caption text, e.g. an info icon. Placed
   * beside the whole stat, the icon sat 60–100px from its caption on a wide
   * figure and read as belonging to the next stat.
   */
  labelAddon?: ReactNode
  /**
   * Makes the stat a toggle that filters something (F31): it renders a
   * `<button aria-pressed>` whose name reads "<label> <value>". A `<dl>`
   * cannot sit inside a button, so the pressable stat is spans laid out the
   * same way. `labelAddon` must not be interactive here: it would nest a
   * control inside the button.
   */
  onPress?: () => void
  /** Whether the pressable stat's filter is on. */
  pressed?: boolean
  /** Hover text for the pressable stat, e.g. what pressing it filters. */
  title?: string
}

const TONE_COLOR: Record<MiniStatTone, string> = {
  success: 'var(--success)',
  danger: 'var(--danger)',
  warning: 'var(--warning)',
  info: 'var(--info)',
  accent: 'var(--accent)',
  neutral: 'var(--fg-subtle)',
}

const TONE_DOT: Record<MiniStatTone, DotTone> = {
  success: 'success',
  danger: 'danger',
  warning: 'warning',
  info: 'info',
  accent: 'accent',
  neutral: 'neutral',
}

/**
 * The one KPI idiom: an uppercase 10.5px caption over a 16px sans
 * `tnum` figure, with an optional toned delta. Page stats go in a
 * `MiniStatStrip`; the bordered sentence-case `StatCard` tiles and the
 * Card-wrapped stat grids it replaces are gone.
 *
 * While the figure is still loading, pass `<StatValueSkeleton />` (from
 * `@/components/states`) as `value` and leave `delta` and `tone` off: a `?? 0`
 * fallback flashed "0" and "quiet" before the data arrived, which read as a
 * real answer (#237).
 */
export function MiniStat({
  label,
  value,
  delta,
  tone = 'neutral',
  valueTone,
  pulse = false,
  labelAddon,
  onPress,
  pressed = false,
  title,
}: MiniStatProps) {
  const figureTone = valueTone ?? (delta == null ? tone : undefined)
  const tint = figureTone && figureTone !== 'neutral' ? figureTone : undefined
  const captionClass = 'inline-flex items-center gap-1 micro-label'
  const captionStyle = { color: 'var(--fg-faint)' }
  const figure = (
    <>
      {/* Sans with tabular digits, not mono: a KPI figure ("1h ago",
          "4.4K rows", "92%") is a number, not code. */}
      <span
        data-slot="mini-stat-value"
        className="tnum text-heading font-semibold tracking-[-0.01em]"
        data-tone={tint}
        style={{ color: tint ? TONE_COLOR[tint] : 'var(--fg)' }}
      >
        {value}
      </span>
      {delta != null && (
        <span
          className="inline-flex items-center gap-[3px] text-micro"
          style={{ color: TONE_COLOR[tone] }}
        >
          {pulse && <Dot tone={TONE_DOT[tone]} size={5} pulse />}
          {delta}
        </span>
      )}
    </>
  )

  if (onPress) {
    // The space between caption and figure sits outside both spans, so the
    // button's name reads "Firing 3", not "Firing3". `-mx-2` cancels the hover
    // padding sideways, so the caption lines up with a plain stat's: in the
    // phoneGrid 2x2 the pressable second row sat 8px right of the first.
    return (
      <button
        type="button"
        aria-pressed={pressed}
        title={title}
        onClick={onPress}
        data-slot="mini-stat-pressable"
        className={cn(
          '-mx-2 -my-1 flex cursor-pointer flex-col items-start gap-px rounded-md px-2 py-1 text-left outline-none transition-colors',
          'hover:bg-[var(--surface-hover)] focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
          pressed && 'bg-[var(--surface-hover)]',
        )}
      >
        <span className={captionClass} style={captionStyle}>
          {label}
          {labelAddon}
        </span>{' '}
        <span className="flex items-baseline gap-1.5">{figure}</span>
      </button>
    )
  }

  // A definition list programmatically ties the value (<dd>) to its caption
  // (<dt>) so assistive tech announces "<label>: <value>" together, instead of
  // two unrelated <span>s. (Issue M9.)
  return (
    <dl className="m-0 flex flex-col gap-px">
      <dt className={captionClass} style={captionStyle}>
        {label}
        {labelAddon}
      </dt>
      <dd className="m-0 flex items-baseline gap-1.5">{figure}</dd>
    </dl>
  )
}

/**
 * A wrapping row of stats with a hairline between neighbours.
 *
 * `boxed` gives the one page-KPI container: the sunken
 * `rounded-card border bg-bg-sunken px-4 py-3` box that Overview, Metrics,
 * Anomalies and Coverage each spelled out by hand. Leave it off for a strip
 * that already sits inside a panel or a header.
 *
 * The divider used to be a sibling element between two stats, so when the row
 * wrapped (375px, and 768px beside the sidebar) it stayed at the end of the
 * line with nothing after it ("COVERAGE 58.8% |"). Here every stat but the
 * first carries its own divider in the gap to its left, and the row clips a
 * few pixels outside its content box: a stat that starts a line has its
 * divider out in that clipped margin, so no line ever begins or ends with one.
 * The clip is horizontal only, so focus rings above and below stay whole.
 *
 * `className` / `style` style the outer box (border, background, padding);
 * falsy children are skipped, so a conditional stat needs no divider logic.
 *
 * `phoneGrid` lays a four-stat strip out as a 2×2 grid below `sm`, with no
 * dividers: wrapped as a row, the fourth stat sat alone on its own line at
 * 390px.
 */
export function MiniStatStrip({
  children,
  boxed = false,
  phoneGrid = false,
  className,
  style,
}: {
  children: ReactNode
  boxed?: boolean
  phoneGrid?: boolean
  className?: string
  style?: CSSProperties
}) {
  const items = Children.toArray(children)
  return (
    <div
      data-slot="mini-stat-strip"
      className={cn(boxed && 'rounded-card border border-border bg-bg-sunken px-4 py-3', className)}
      style={style}
    >
      {/* The clip box sits 10px outside the row, enough for the focus ring
          of a pressable stat, whose box reaches 8px past its caption; the
          dividers sit 12px out, in the 24px gap, so they stay clipped. It
          clips sideways only: a divider only ever pokes out at the left, and a
          vertical clip cut the top and bottom of the focus ring of a stat that
          reaches past its row (the Metrics catalog's filter toggles). */}
      <div
        className="-m-2.5 p-2.5"
        style={{ overflowX: 'clip', overflowY: 'visible' }}
        data-slot="mini-stat-clip"
      >
        <div
          data-phone-grid={phoneGrid || undefined}
          className={cn(
            'gap-x-6 gap-y-4',
            phoneGrid
              ? 'grid grid-cols-2 sm:flex sm:flex-wrap sm:items-center'
              : 'flex flex-wrap items-center',
          )}
        >
          {items.map((item, index) => (
            <div key={index} className="relative flex min-w-0 items-center">
              {index > 0 && (
                <span
                  aria-hidden="true"
                  data-slot="mini-stat-divider"
                  className={cn(
                    'absolute -left-3 top-1/2 h-6 w-px -translate-y-1/2',
                    phoneGrid && 'max-sm:hidden',
                    'bg-border',
                  )}
                />
              )}
              {item}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
