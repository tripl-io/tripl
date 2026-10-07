import * as React from "react"
import * as TabsPrimitive from "@radix-ui/react-tabs"
import { cn } from "@/lib/utils"
import { CountBadge } from "@/components/primitives/count-badge"
import {
  SEGMENTED_ACTIVE,
  SEGMENTED_TRACK,
  segmentedItemVariants,
} from "@/components/ui/segmented-variants"

type TabsListVariant = "underline" | "segmented"

const TabsVariantContext = React.createContext<{ variant: TabsListVariant; size: "sm" | "md" }>({
  variant: "underline",
  size: "md",
})

function Tabs({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return <TabsPrimitive.Root data-slot="tabs" className={cn("flex flex-col gap-2", className)} {...props} />
}

/*
 * The app's tab idiom, not shadcn's pill: a hairline under the strip and a 2px
 * accent underline on the active tab, as the hand-rolled tablists on the scan,
 * event-type and alerting screens draw it. Those can move onto this primitive
 * (and its arrow-key roving and tabpanel wiring) without changing how they
 * look.
 *
 * `variant="segmented"` draws the same Radix tabs as the app's one segmented
 * control: use it when a small strip switches panels in place
 * (Inbox / Rules / Delivery log inside a card). For a range or view toggle
 * that has no tabpanel, use <SegmentedControl>.
 */
function TabsList({
  className,
  variant = "underline",
  size = "md",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List> & {
  variant?: TabsListVariant
  /** Segmented only: md 32px, sm 28px. */
  size?: "sm" | "md"
}) {
  const context = React.useMemo(() => ({ variant, size }), [variant, size])
  return (
    <TabsVariantContext.Provider value={context}>
      <TabsPrimitive.List
        data-slot="tabs-list"
        data-variant={variant}
        className={cn(
          variant === "segmented"
            ? SEGMENTED_TRACK
            : "text-fg-tertiary flex w-full items-end gap-1 overflow-x-auto border-b border-border",
          className
        )}
        {...props}
      />
    </TabsVariantContext.Provider>
  )
}

function TabsTrigger({
  className,
  children,
  count,
  countUrgent = false,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger> & {
  /**
   * A count after the label ("Inbox 1"), so the strip itself reads as a
   * triage signal. Announced as part of the tab's name.
   */
  count?: number
  /** Solid red count: open incidents, failed deliveries. */
  countUrgent?: boolean
}) {
  const { variant, size } = React.useContext(TabsVariantContext)
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      // The tab's value on the DOM: the demo guide rings the tab that opens a
      // step's section (`?section=monitors` → Rules) by it.
      data-tab-value={props.value}
      className={cn(
        variant === "segmented"
          ? [segmentedItemVariants({ size }), SEGMENTED_ACTIVE]
          : [
              "-mb-px inline-flex cursor-pointer items-center justify-center gap-1.5 whitespace-nowrap border-b-2 border-transparent px-3 py-2 text-body-sm font-medium text-fg-muted outline-none transition-colors hover:text-foreground disabled:pointer-events-none disabled:text-fg-faint data-[state=active]:border-accent data-[state=active]:text-foreground",
              "focus-visible:rounded-t-md focus-visible:ring-ring/50 focus-visible:ring-[3px]",
            ],
        className
      )}
      {...props}
    >
      {children}
      {count !== undefined && (
        <>
          <CountBadge count={count} max={99} urgent={countUrgent && count > 0} />
          {/* The space sits outside the span: accessible-name computation trims
              each element's text, so a space inside it would read "Inbox(2)". */}
          {" "}
          <span className="sr-only">{`(${count})`}</span>
        </>
      )}
    </TabsPrimitive.Trigger>
  )
}

function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      // Radix gives the panel tabIndex=0, so it is a Tab stop and needs its own
      // indicator: the global :focus-visible outline sits in @layer base and
      // loses to `outline-none`.
      className={cn(
        "flex-1 rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
        className
      )}
      {...props}
    />
  )
}

export { Tabs, TabsContent, TabsList, TabsTrigger }
