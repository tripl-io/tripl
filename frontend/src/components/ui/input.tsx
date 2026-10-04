import * as React from "react"
import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      // 32px / 12.5px with the 7px control radius, the same box as a default
      // Button and SelectTrigger, so a form row never changes scale.
      className={cn(
        "file:text-foreground placeholder:text-fg-tertiary selection:bg-primary/20 flex h-8 w-full min-w-0 rounded-control border border-input bg-transparent px-2.5 py-1 text-body-sm shadow-xs outline-none transition-colors file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-body-sm file:font-medium disabled:cursor-not-allowed disabled:border-dashed disabled:border-[var(--border-strong)] disabled:bg-transparent disabled:text-[var(--fg-subtle)]",
        "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
        "aria-invalid:ring-destructive/20 aria-invalid:border-destructive",
        className
      )}
      {...props}
    />
  )
}

export { Input }
