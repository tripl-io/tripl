import * as React from "react"
import { cn } from "@/lib/utils"
import {
  chipVariants,
  type ChipSize,
  type ChipTone,
  type ChipVariant,
} from "@/components/primitives/chip-variants"

/*
 * Badge is a thin alias over the Chip classes: one pill geometry for
 * every status. The old names keep working, mapped onto the taxonomy:
 *   - no variant / `default` / `secondary` / `neutral` → neutral soft pill.
 *     `default` used to be a solid brand block, so a Badge that simply forgot
 *     its variant was the loudest thing in its row.
 *   - `outline` → neutral outlined tag (kind / category)
 *   - `success` / `warning` / `info` / `danger` / `accent` → soft tone
 *   - `destructive` → solid red, reserved for counts that demand attention; a
 *     red STATE ("Failed") is the soft `danger`
 *   - `solid` → solid brand fill, only when a brand block is really meant
 * New code should use <Chip tone variant> directly.
 */
type BadgeVariant =
  | "default"
  | "neutral"
  | "secondary"
  | "outline"
  | "destructive"
  | "solid"
  | "accent"
  | "success"
  | "warning"
  | "info"
  | "danger"

const BADGE_TO_CHIP: Record<BadgeVariant, { tone: ChipTone; variant: ChipVariant }> = {
  default: { tone: "neutral", variant: "soft" },
  neutral: { tone: "neutral", variant: "soft" },
  secondary: { tone: "neutral", variant: "soft" },
  outline: { tone: "neutral", variant: "outline" },
  destructive: { tone: "danger", variant: "solid" },
  solid: { tone: "accent", variant: "solid" },
  accent: { tone: "accent", variant: "soft" },
  success: { tone: "success", variant: "soft" },
  warning: { tone: "warning", variant: "soft" },
  info: { tone: "info", variant: "soft" },
  danger: { tone: "danger", variant: "soft" },
}

function Badge({
  className,
  variant,
  size = "sm",
  ...props
}: React.ComponentProps<"span"> & {
  variant?: BadgeVariant | null
  size?: ChipSize
}) {
  const chip = BADGE_TO_CHIP[variant ?? "default"]
  return (
    <span
      data-slot="badge"
      data-tone={chip.tone}
      className={cn(chipVariants({ ...chip, size }), className)}
      {...props}
    />
  )
}

export { Badge }
export type { BadgeVariant }
