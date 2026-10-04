import * as React from "react"
import * as CheckboxPrimitive from "@radix-ui/react-checkbox"
import { Check, Minus } from "lucide-react"
import { cn } from "@/lib/utils"

function Checkbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        // Unchecked border uses --fg-faint (not --input/--border): on a dark
        // sunken table row the old border token sat too close to the background
        // and the box was effectively invisible. --fg-faint stays muted but
        // reads as a real control outline in both themes.
        // `hit-target-24` keeps the 16px box but pads the pointer target out to
        // the WCAG 2.2 minimum — these are the row-select boxes on every table.
        // Indeterminate ("some rows selected") gets the same filled box as
        // checked, with a minus instead of a tick: an unfilled box with a grey
        // tick read as "all selected".
        "group/checkbox hit-target-24 peer border-[var(--fg-faint)] data-[state=checked]:bg-accent-solid data-[state=checked]:text-accent-solid-fg data-[state=checked]:border-accent-solid data-[state=indeterminate]:bg-accent-solid data-[state=indeterminate]:text-accent-solid-fg data-[state=indeterminate]:border-accent-solid focus-visible:border-ring focus-visible:ring-ring/50 aria-invalid:ring-destructive/20 aria-invalid:border-destructive size-4 shrink-0 rounded-sm border shadow-xs outline-none transition-shadow focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer",
        className
      )}
      {...props}
    >
      {/* Both glyphs render and the Root's data-state picks one, so an
          uncontrolled `defaultChecked="indeterminate"` draws the minus too;
          reading `props.checked` only saw the controlled state. */}
      <CheckboxPrimitive.Indicator className="flex items-center justify-center text-current transition-none">
        <Minus className="hidden size-3.5 group-data-[state=indeterminate]/checkbox:block" aria-hidden="true" />
        <Check className="hidden size-3.5 group-data-[state=checked]/checkbox:block" aria-hidden="true" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
