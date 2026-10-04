import { cva } from "class-variance-authority"

export type ChipTone = "neutral" | "accent" | "success" | "warning" | "danger" | "info"
export type ChipVariant = "soft" | "outline" | "solid"
export type ChipSize = "xs" | "sm" | "md"

/*
 * The one badge/pill primitive. `Badge` (ui/badge.tsx) is a thin alias
 * over these classes, so a status reads the same pill everywhere:
 *   - status / lifecycle ("Live", "Failed")  → <Chip tone>            soft pill
 *   - kind / category tag ("SQL", "core")    → <Chip variant="outline">
 *   - count                                  → <CountBadge>
 *   - code value / identifier                → <CodeToken>
 * Classes, not inline styles, so a call site's className can still override
 * one property through cn().
 */
export const chipVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1 whitespace-nowrap rounded-full border font-medium leading-none [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral: "",
        accent: "",
        success: "",
        warning: "",
        danger: "",
        info: "",
      },
      variant: {
        soft: "border-transparent",
        outline: "bg-transparent",
        solid: "border-transparent",
      },
      size: {
        xs: "h-[18px] px-1.5 text-micro [&_svg:not([class*='size-'])]:size-3",
        sm: "h-5 px-[7px] text-caption [&_svg:not([class*='size-'])]:size-3",
        md: "h-6 px-2.5 text-body-sm [&_svg:not([class*='size-'])]:size-3.5",
      },
    },
    compoundVariants: [
      { variant: "soft", tone: "neutral", className: "bg-surface-hover text-fg-muted" },
      { variant: "soft", tone: "accent", className: "bg-accent-soft text-accent" },
      { variant: "soft", tone: "success", className: "bg-success-soft text-success" },
      { variant: "soft", tone: "warning", className: "bg-warning-soft text-warning" },
      { variant: "soft", tone: "danger", className: "bg-danger-soft text-danger" },
      { variant: "soft", tone: "info", className: "bg-info-soft text-info" },
      // Outline keeps the tone's soft fill for a coloured tone (as the old
      // inline-styled Chip did) and drops it only for the neutral tag.
      { variant: "outline", tone: "neutral", className: "border-border text-fg-muted" },
      { variant: "outline", tone: "accent", className: "border-accent bg-accent-soft text-accent" },
      { variant: "outline", tone: "success", className: "border-success bg-success-soft text-success" },
      { variant: "outline", tone: "warning", className: "border-warning bg-warning-soft text-warning" },
      { variant: "outline", tone: "danger", className: "border-danger bg-danger-soft text-danger" },
      { variant: "outline", tone: "info", className: "border-info bg-info-soft text-info" },
      // Solid is for counts and the rare "this demands attention now" flag,
      // never for a plain state.
      { variant: "solid", tone: "neutral", className: "bg-surface-active text-fg" },
      { variant: "solid", tone: "accent", className: "bg-accent-solid text-accent-solid-fg" },
      { variant: "solid", tone: "danger", className: "bg-destructive text-destructive-foreground" },
      // No AA-checked ink exists for a solid success/warning/info fill, so
      // those fall back to the soft pill.
      { variant: "solid", tone: "success", className: "bg-success-soft text-success" },
      { variant: "solid", tone: "warning", className: "bg-warning-soft text-warning" },
      { variant: "solid", tone: "info", className: "bg-info-soft text-info" },
    ],
    defaultVariants: { tone: "neutral", variant: "soft", size: "sm" },
  },
)
