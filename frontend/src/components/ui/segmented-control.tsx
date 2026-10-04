import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import {
  SEGMENTED_PRESSED,
  SEGMENTED_TRACK,
  segmentedItemVariants,
} from "@/components/ui/segmented-variants"

export type SegmentedOption<T extends string | number> = {
  value: T
  label: ReactNode
  disabled?: boolean
  /** Tooltip, e.g. why an option is unavailable. */
  title?: string
}

/**
 * Two to four mutually exclusive VIEWS of the same content: a time range, a
 * theme, a density. Not for filters (use FilterBar) and not for
 * switching panels (use <Tabs> with <TabsList variant="segmented">, which
 * brings tabpanel wiring and arrow-key roving).
 *
 * A labelled group of toggle buttons with aria-pressed, one pressed at a time.
 */
export function SegmentedControl<T extends string | number>({
  value,
  onChange,
  options,
  size = "md",
  className,
  "aria-label": ariaLabel,
}: {
  value: T
  onChange: (value: T) => void
  options: ReadonlyArray<SegmentedOption<T>>
  size?: "sm" | "md"
  className?: string
  "aria-label": string
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      data-slot="segmented-control"
      className={cn(SEGMENTED_TRACK, className)}
    >
      {options.map(option => (
        <button
          key={String(option.value)}
          type="button"
          aria-pressed={option.value === value}
          disabled={option.disabled}
          title={option.title}
          className={cn(segmentedItemVariants({ size }), SEGMENTED_PRESSED)}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
