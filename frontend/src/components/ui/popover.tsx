import * as React from "react"
import * as PopoverPrimitive from "@radix-ui/react-popover"
import { keepOpenForDemoGuide } from "@/components/ui/demo-guide-layer"
import { cn } from "@/lib/utils"

const Popover = PopoverPrimitive.Root
const PopoverTrigger = PopoverPrimitive.Trigger
const PopoverAnchor = PopoverPrimitive.Anchor
const PopoverArrow = PopoverPrimitive.Arrow

function PopoverContent({
  className,
  align = "center",
  sideOffset = 4,
  onInteractOutside,
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        data-slot="popover-content"
        align={align}
        sideOffset={sideOffset}
        // The demo guide coaches what an open popover holds: pressing the
        // guide must not shut it (demo-guide-layer.ts).
        onInteractOutside={keepOpenForDemoGuide(onInteractOutside)}
        className={cn(
          "bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-(--z-popover) w-72 rounded-card border p-4 shadow-md outline-none",
          className
        )}
        {...props}
      />
    </PopoverPrimitive.Portal>
  )
}

export { Popover, PopoverAnchor, PopoverArrow, PopoverContent, PopoverTrigger }
