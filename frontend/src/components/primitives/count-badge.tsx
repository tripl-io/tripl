import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

/**
 * A count on a nav item, tab or bell. Solid red only when `urgent`
 * (unread alerts, open signals); every other count is a quiet neutral pill,
 * so red keeps meaning "look now". Sans + tabular figures, never mono.
 * `max` caps the figure ("9+"); the full number belongs in the owner's
 * accessible name, since this renders aria-hidden by default.
 */
export function CountBadge({
  count,
  max,
  urgent = false,
  className,
  ...props
}: Omit<ComponentProps<"span">, "children"> & {
  count: number | string
  max?: number
  urgent?: boolean
}) {
  const label =
    typeof count === "number" && max !== undefined && count > max ? `${max}+` : String(count)
  return (
    <span
      data-slot="count-badge"
      aria-hidden="true"
      className={cn(
        "tnum inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full px-1 text-micro font-semibold leading-none",
        urgent ? "bg-destructive text-destructive-foreground" : "bg-surface-active text-fg-muted",
        className,
      )}
      {...props}
    >
      {label}
    </span>
  )
}
