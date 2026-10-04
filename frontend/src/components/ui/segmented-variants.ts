import { cva } from "class-variance-authority"

/*
 * The one segmented-control look, shared by <SegmentedControl>
 * and <TabsList variant="segmented">: a sunken track with the selected option
 * raised on the surface. Not a solid accent fill: on a small 7d/30d/90d picker
 * that was the loudest thing in a chart header, louder than the page's CTA.
 * The md track is 32px tall, sm 28px, matching Button/Input/Select;
 * md options grow to 32px on phones for a usable tap target.
 */
export const SEGMENTED_TRACK =
  "inline-flex w-fit max-w-full items-center gap-0.5 overflow-x-auto rounded-control border border-border bg-bg-sunken p-0.5"

export const segmentedItemVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 whitespace-nowrap rounded-sm font-medium text-fg-muted outline-none transition-colors hover:text-fg focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:text-fg-faint [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      size: {
        sm: "h-[22px] px-2 text-caption",
        md: "h-[26px] px-2.5 text-body-sm max-sm:h-8",
      },
    },
    defaultVariants: { size: "md" },
  },
)

/** Selected option, keyed on aria-pressed (SegmentedControl buttons). */
export const SEGMENTED_PRESSED = "aria-pressed:bg-surface aria-pressed:text-fg aria-pressed:shadow-sm"

/** Selected option, keyed on Radix's data-state (segmented TabsTrigger). */
export const SEGMENTED_ACTIVE =
  "data-[state=active]:bg-surface data-[state=active]:text-fg data-[state=active]:shadow-sm"
