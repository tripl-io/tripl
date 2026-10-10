import * as React from "react"
import { createPortal } from "react-dom"
import { Search, SlidersHorizontal, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { useMediaQuery } from "@/hooks/useMediaQuery"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"

/*
 * The one filter-bar idiom for list pages: a search box, then filter
 * chips, a "Clear filters" link while anything is set, and the result count
 * on the right. Filters always apply instantly — no Apply button. Segmented
 * controls are for 2-4 exclusive VIEWS, never for filters.
 *
 *   <FilterBar count="42 events" active={hasFilters} onClear={clear}>
 *     <FilterSearch things="events" value={q} onValueChange={setQ} />
 *     <FilterSelect label="Status" value={status} onValueChange={setStatus}
 *       options={[{ value: "live", label: "live" }, ...]} />
 *   </FilterBar>
 *
 * Below 640px the chips fold into one "Filters (n)" button that opens a
 * bottom sheet holding them, n being how many are set; the search box stays in
 * the row. Every FilterSelect folds on its own; wrap any other filter
 * control in <FilterBarItem active={…}> to fold it too.
 */

/** Below `sm` (640px) the chips fold into the sheet. */
const COLLAPSE_QUERY = "(max-width: 639.98px)"

type FilterBarContextValue = {
  collapsed: boolean
  /** The open sheet's body, where folded items render; null while closed. */
  sheetBody: HTMLElement | null
  register: (id: string, active: boolean) => void
  unregister: (id: string) => void
}

const FilterBarContext = React.createContext<FilterBarContextValue | null>(null)

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
}

function FilterBar({
  children,
  count,
  active = false,
  onClear,
  className,
}: {
  children: React.ReactNode
  /** Result count, e.g. "42 events"; shown on the right. */
  count?: React.ReactNode
  /** Whether any filter differs from its default; shows "Clear filters". */
  active?: boolean
  onClear?: () => void
  className?: string
}) {
  const collapsed = useMediaQuery(COLLAPSE_QUERY)
  const [sheetOpen, setSheetOpen] = React.useState(false)
  // Growing past 640px drops an open sheet, so it does not pop back open the
  // next time the bar folds.
  if (!collapsed && sheetOpen) setSheetOpen(false)
  const [sheetBody, setSheetBody] = React.useState<HTMLDivElement | null>(null)
  // Every foldable item and whether it is set: the button's "(n)", and
  // whether there is anything to fold at all.
  const [items, setItems] = React.useState<ReadonlyMap<string, boolean>>(() => new Map())
  const register = React.useCallback((id: string, itemActive: boolean) => {
    setItems(prev => {
      if (prev.get(id) === itemActive) return prev
      const next = new Map(prev)
      next.set(id, itemActive)
      return next
    })
  }, [])
  const unregister = React.useCallback((id: string) => {
    setItems(prev => {
      if (!prev.has(id)) return prev
      const next = new Map(prev)
      next.delete(id)
      return next
    })
  }, [])
  const context = React.useMemo<FilterBarContextValue>(
    () => ({ collapsed, sheetBody: collapsed && sheetOpen ? sheetBody : null, register, unregister }),
    [collapsed, sheetOpen, sheetBody, register, unregister],
  )
  const setCount = [...items.values()].filter(Boolean).length
  const folding = collapsed && items.size > 0

  return (
    <FilterBarContext.Provider value={context}>
    <div
      data-slot="filter-bar"
      className={cn("flex flex-wrap items-center gap-2", className)}
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
        {children}
        {folding && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-haspopup="dialog"
            aria-expanded={sheetOpen}
            onClick={() => setSheetOpen(true)}
            data-active={setCount > 0 || undefined}
            className={cn(setCount > 0 && "border-accent bg-accent-soft")}
          >
            <SlidersHorizontal aria-hidden="true" />
            Filters{setCount > 0 && <> <span className="tnum">({setCount})</span></>}
          </Button>
        )}
        {active && onClear && (
          <Button type="button" variant="ghost" size="sm" onClick={onClear} className="text-fg-muted">
            <X aria-hidden="true" />
            Clear filters
          </Button>
        )}
      </div>
      {/* Mounted even while there is no count: a live region inserted
          together with its first text is not announced by most screen
          readers, so the first "N match" would go unread. */}
      <div className="tnum shrink-0 text-caption text-fg-muted" aria-live="polite">
        {count}
      </div>
    </div>
    {folding && (
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent side="bottom">
          <SheetHeader>
            <SheetTitle>Filters</SheetTitle>
            <SheetDescription>Filters apply as you pick them.</SheetDescription>
          </SheetHeader>
          <SheetBody>
            {/* FilterBarItems render here through a portal while it is open. */}
            <div ref={setSheetBody} className="flex flex-col items-stretch gap-2 py-1" />
          </SheetBody>
          <SheetFooter>
            {count != null && count !== "" && (
              <span className="tnum mr-auto text-caption text-fg-muted">{count}</span>
            )}
            {active && onClear && (
              <Button type="button" variant="ghost" size="sm" onClick={onClear}>
                Clear filters
              </Button>
            )}
            <Button type="button" size="sm" onClick={() => setSheetOpen(false)}>
              Done
            </Button>
          </SheetFooter>
        </SheetContent>
      </Sheet>
    )}
    </FilterBarContext.Provider>
  )
}

/**
 * A filter control that folds into the "Filters (n)" sheet below 640px.
 * `active` is whether it differs from its default, for the button's count.
 * Outside a FilterBar, or on a wide screen, it renders in place.
 */
function FilterBarItem({ active, children }: { active: boolean; children: React.ReactNode }) {
  const bar = React.useContext(FilterBarContext)
  const id = React.useId()
  const register = bar?.register
  const unregister = bar?.unregister
  React.useEffect(() => {
    register?.(id, active)
  }, [register, id, active])
  React.useEffect(() => () => unregister?.(id), [unregister, id])
  if (!bar?.collapsed) return <>{children}</>
  return bar.sheetBody ? createPortal(children, bar.sheetBody) : null
}

/**
 * The bar's search box: 28px like the chips and the sm Clear button, so the
 * row keeps one height; leading icon, "Search {things}…", and "/"
 * focuses it from anywhere outside a text field.
 */
function FilterSearch({
  things,
  value,
  onValueChange,
  className,
  ...props
}: Omit<React.ComponentProps<typeof Input>, "ref" | "value" | "onChange" | "placeholder" | "type"> & {
  /** Plural noun for the placeholder and the accessible name: "events". */
  things: string
  value: string
  onValueChange: (value: string) => void
}) {
  const ref = React.useRef<HTMLInputElement>(null)
  // The box keeps its own text. Callers mostly hold the search in the URL, and
  // a router update lands after the keystroke: a box bound straight to it lost
  // characters typed quickly and threw the caret to the end. While the box has
  // focus it is the source of truth; a change from outside (Clear filters, a
  // saved view, Back) reaches it once focus is elsewhere.
  const [draft, setDraft] = React.useState(value)
  const [focused, setFocused] = React.useState(false)
  const [seen, setSeen] = React.useState(value)
  if (value !== seen) {
    setSeen(value)
    if (!focused) setDraft(value)
  }
  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return
      if (event.defaultPrevented || isTypingTarget(event.target)) return
      if (!ref.current) return
      event.preventDefault()
      ref.current.focus()
    }
    document.addEventListener("keydown", onKeyDown)
    return () => document.removeEventListener("keydown", onKeyDown)
  }, [])
  return (
    <div className={cn("relative w-full min-w-[180px] max-w-[320px] flex-1 sm:w-auto", className)}>
      <Search
        className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-fg-muted"
        aria-hidden="true"
      />
      <Input
        ref={ref}
        type="search"
        aria-label={`Search ${things}`}
        placeholder={`Search ${things}…`}
        className="h-7 pl-8"
        {...props}
        value={draft}
        onChange={event => {
          setDraft(event.target.value)
          onValueChange(event.target.value)
        }}
        onFocus={event => {
          setFocused(true)
          props.onFocus?.(event)
        }}
        onBlur={event => {
          setFocused(false)
          props.onBlur?.(event)
        }}
      />
    </div>
  )
}

export type FilterOption = { value: string; label: string }

/**
 * One filter as a chip that shows its current value: "Status: Any" while
 * unset (dashed, quiet), "Status: live" once set (solid border, accent tint).
 * `anyValue` is the option that means "no filter" (default "any"); `anyLabel`
 * is what the chip says for it, the same on every list page.
 */
function FilterSelect({
  label,
  value,
  onValueChange,
  options,
  anyValue = "any",
  anyLabel = "Any",
  className,
}: {
  label: string
  value: string
  onValueChange: (value: string) => void
  options: ReadonlyArray<FilterOption>
  anyValue?: string
  anyLabel?: string
  className?: string
}) {
  const set = value !== anyValue
  const current = set ? options.find(option => option.value === value)?.label ?? value : anyLabel
  return (
    <FilterBarItem active={set}>
    <Select value={value} onValueChange={onValueChange}>
      <SelectTrigger
        // The value belongs in the name: a button-role combobox exposes no
        // separate value, so "Status filter" alone never said what is set.
        aria-label={`${label} filter: ${current}`}
        data-active={set || undefined}
        className={cn(
          "h-7 w-auto gap-1.5 text-caption",
          set
            ? "border-accent bg-accent-soft text-fg"
            : "border-dashed bg-transparent text-fg-muted",
          className
        )}
      >
        <span className="font-medium">{label}:</span>
        <SelectValue>{current}</SelectValue>
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={anyValue}>{anyLabel}</SelectItem>
        {options
          .filter(option => option.value !== anyValue)
          .map(option => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
      </SelectContent>
    </Select>
    </FilterBarItem>
  )
}

export { FilterBar, FilterBarItem, FilterSearch, FilterSelect }
