import * as React from "react"
import { cn } from "@/lib/utils"

// One section-card geometry, shared with the settings kit's
// `Panel` and `SCard`: `rounded-card` (10px), a 1px border, the `--surface`
// fill and no shadow; a header bar at `px-4 py-3` over a `--border-subtle`
// rule, a 12.5px semibold title, and a 16px body. The shadcn defaults
// (`rounded-xl`, `py-6` + `px-6`, `shadow-sm`, `gap-6`) stacked about 48px of
// empty band above and below every chart and made this the only raised card.
function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card"
      // --scroll-x-bg: a ui/Table inside the card paints its sideways-scroll
      // fades in the card's colour rather than --bg, which in dark mode drew a
      // darker band across the card (see .tripl-scroll-x in index.css).
      className={cn(
        "flex flex-col rounded-card border border-border bg-surface text-fg [--scroll-x-bg:var(--surface)]",
        className,
      )}
      {...props}
    />
  )
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn(
        "flex flex-col gap-0.5 border-b border-border-subtle px-4 py-3 @container/card-header",
        className,
      )}
      {...props}
    />
  )
}

type CardTitleProps = React.ComponentProps<"h3"> & {
  as?: "h1" | "h2" | "h3" | "h4" | "h5" | "h6"
}

/** The section title: `as="h2"` directly under a page's h1 (the usual case). */
function CardTitle({ className, as: As = "h3", ...props }: CardTitleProps) {
  return (
    <As
      data-slot="card-title"
      className={cn("m-0 text-body-sm leading-[1.4] font-semibold", className)}
      {...props}
    />
  )
}

function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-caption text-fg-subtle", className)}
      {...props}
    />
  )
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="card-content" className={cn("p-(--panel-pad)", className)} {...props} />
}

function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-footer"
      className={cn(
        "flex items-center gap-2.5 rounded-b-card border-t border-border-subtle bg-bg-sunken px-4 py-3",
        className,
      )}
      {...props}
    />
  )
}

export { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle }
