import type { CSSProperties, HTMLAttributes, ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * A caption + control row that stacks when its own width is narrow.
 *
 * Below 560px of ROW width the caption sits above the control at full width;
 * from 560px up it becomes a fixed-width column beside it. Every authoring
 * form used to hard-code the side-by-side layout (a 200px event caption, a
 * 232px scan caption, a 180px event-type caption), which left a 375px screen
 * 40-60px for the control: the Name box showed two letters and a select only
 * its chevron. The settings kit's `Field` stacked correctly; this is that
 * layout, shared.
 *
 * The switch is a container query on the row, not a viewport breakpoint:
 * from `md` the settings rail is pinned and eats 264px, so at a 768px
 * viewport a viewport-`sm` row gave the control ~110px (the AI API key input
 * was a sliver and its button overflowed the card). 560px is the 232px caption
 * + 24px gap + a ~300px control. The caption is also capped at 40% of the row.
 *
 * The outer element is the query container and takes `rest`/`style` (ARIA,
 * borders); `className` goes on the inner flex row, so padding, gap and
 * alignment classes keep working. Row-width variants in `className` /
 * `captionClassName` are written `@min-[560px]:` (e.g. `@min-[560px]:pt-1.5`),
 * not `sm:` — `sm:` would switch on the viewport while the row still stacks.
 *
 * Presentation only: the caller renders the caption (a `<label>`, or a `<span>`
 * naming a group) and owns padding, borders and ARIA on the row.
 */
export function FormRow({
  caption,
  children,
  labelWidth = 232,
  captionClassName,
  className,
  style,
  ...rest
}: {
  caption: ReactNode
  children: ReactNode
  /** The caption column's width once the row is side by side, in px (capped at 40% of the row). */
  labelWidth?: number
  /** Extra classes on the caption column (e.g. `@min-[560px]:pt-1.5` to align with a 34px control). */
  captionClassName?: string
} & HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      data-slot="form-row"
      {...rest}
      className="@container"
      style={{ ...style, '--form-row-label': `${labelWidth}px` } as CSSProperties}
    >
      <div
        className={cn(
          'flex flex-col gap-2 @min-[560px]:flex-row @min-[560px]:items-start @min-[560px]:gap-6',
          className,
        )}
      >
        <div
          data-slot="form-row-caption"
          className={cn(
            'w-full @min-[560px]:w-[min(var(--form-row-label),40%)] @min-[560px]:shrink-0',
            captionClassName,
          )}
        >
          {caption}
        </div>
        <div data-slot="form-row-control" className="min-w-0 flex-1">
          {children}
        </div>
      </div>
    </div>
  )
}
