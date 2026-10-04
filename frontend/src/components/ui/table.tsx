import * as React from "react"
import { cn } from "@/lib/utils"

function Table({
  className,
  scroll = true,
  ...props
}: React.ComponentProps<"table"> & {
  /** `false` for a table inside a scroll region of its own: that region stays
   *  the one element that scrolls (and the one keyboard users can focus). */
  scroll?: boolean
}) {
  // `tripl-scroll-x` adds the edge fade/shadow that tells the reader the columns
  // continue past the right edge. This container — not the outer
  // `.tripl-table-wrap` — is the element that actually scrolls, and its
  // horizontal scrollbar sits far below the header row on a long table.
  return (
    <div
      data-slot="table-container"
      className={cn("relative w-full", scroll && "tripl-scroll-x overflow-x-auto")}
    >
      <table data-slot="table" className={cn("w-full caption-bottom text-body", className)} {...props} />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return <thead data-slot="table-header" className={cn("[&_tr]:border-b", className)} {...props} />
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return <tbody data-slot="table-body" className={cn("[&_tr:last-child]:border-0", className)} {...props} />
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn("bg-muted/50 border-t font-medium [&>tr]:last:border-b-0", className)}
      {...props}
    />
  )
}

// Rows and cells follow the density setting: --row-h is the row's
// height (a floor, as on any table row) and --cell-px the cell gutter, so
// compact/cozy/comfy change every ui/Table, not only `.tripl-table`. The cell's
// vertical padding stays small so the row height, not the padding, decides.
function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "h-(--row-h) hover:bg-muted/50 data-[state=selected]:bg-muted border-b transition-colors",
        className
      )}
      {...props}
    />
  )
}

// One header typography for every table idiom: the 10.5px semibold uppercase
// caption `.tripl-table th` (index.css) and the settings tables use. This one
// was 12px medium with wide tracking, so a ui/Table and a data table on the
// same page captioned their columns differently.
function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "text-fg-tertiary h-10 px-(--cell-px) text-left align-middle micro-label whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "px-(--cell-px) py-1.5 align-middle [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({ className, ...props }: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("text-fg-tertiary mt-4 text-body", className)}
      {...props}
    />
  )
}

export { Table, TableBody, TableCaption, TableCell, TableFooter, TableHead, TableHeader, TableRow }
