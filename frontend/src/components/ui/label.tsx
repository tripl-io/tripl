import * as React from "react"
import * as LabelPrimitive from "@radix-ui/react-label"
import { cn } from "@/lib/utils"

function Label({
  className,
  optional = false,
  children,
  ...props
}: React.ComponentProps<typeof LabelPrimitive.Root> & {
  /**
   * Append a muted "(optional)" after the text. Rendered here rather
   * than typed into the label ("From Address (optional)"), so every optional
   * field reads the same and the label text itself stays the field's name.
   */
  optional?: boolean
}) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn(
        "flex items-center gap-1 text-body leading-none font-medium select-none group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:opacity-50 peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
        className
      )}
      {...props}
    >
      {children}
      {/* The space keeps the accessible name "From address (optional)"; as
          flex items the whitespace itself is not rendered. */}
      {optional && " "}
      {optional && (
        <span data-slot="label-optional" className="font-normal text-(--fg-subtle)">
          (optional)
        </span>
      )}
    </LabelPrimitive.Root>
  )
}

export { Label }
