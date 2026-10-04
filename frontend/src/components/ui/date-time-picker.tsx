import * as React from "react"
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { IconButton } from "@/components/ui/icon-button"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { formatDate } from "@/lib/datetime"
import { cn } from "@/lib/utils"

/*
 * A date + time picker in the design system's own controls: a button that opens
 * a small calendar grid in a popover, and a time field beside it. It replaces the
 * native `datetime-local` input, whose picker looks different in every browser
 * and ignores the app's theme.
 *
 * The value keeps the `datetime-local` wire format, `YYYY-MM-DDTHH:mm` in the
 * viewer's local time, so a form that used the native input keeps parsing it
 * the same way.
 */

const DATE_PART = /^(\d{4})-(\d{2})-(\d{2})$/
const TIME_PART = /^\d{2}:\d{2}$/

/** ISO weeks: the grid starts on Monday. */
const WEEK_START = 1
/** 2024-01-01 was a Monday — any known Monday names the weekday columns. */
const WEEKDAYS = Array.from({ length: 7 }, (_, index) => new Date(2024, 0, 1 + index))

function pad(value: number): string {
  return String(value).padStart(2, "0")
}

function toDateKey(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function parseDateKey(key: string): Date | null {
  const match = DATE_PART.exec(key)
  if (!match) return null
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
}

function splitValue(value: string): { date: string; time: string } {
  const [date = "", time = ""] = value.split("T")
  return {
    date: DATE_PART.test(date) ? date : "",
    time: TIME_PART.test(time.slice(0, 5)) ? time.slice(0, 5) : "",
  }
}

function addDays(date: Date, days: number): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days)
}

/** The same day `months` away, clamped to that month's last day (Jan 31 → Feb 28). */
function addMonths(date: Date, months: number): Date {
  const lastDay = new Date(date.getFullYear(), date.getMonth() + months + 1, 0).getDate()
  return new Date(date.getFullYear(), date.getMonth() + months, Math.min(date.getDate(), lastDay))
}

function sameDay(a: Date, b: Date): boolean {
  return toDateKey(a) === toDateKey(b)
}

/** Outside the `[min, max]` day keys; either end may be absent. */
function outOfRange(day: Date, min: string | undefined, max: string | undefined): boolean {
  const key = toDateKey(day)
  return (!!min && key < min) || (!!max && key > max)
}

/** `day` moved inside `[min, max]`, so the keyboard never lands on a disabled day. */
function clampDay(day: Date, min: string | undefined, max: string | undefined): Date {
  const low = min ? parseDateKey(min) : null
  const high = max ? parseDateKey(max) : null
  if (low && toDateKey(day) < toDateKey(low)) return low
  if (high && toDateKey(day) > toDateKey(high)) return high
  return day
}

/** The month's days in week rows; `null` pads the first and last week. */
function monthWeeks(month: Date): (Date | null)[][] {
  const first = new Date(month.getFullYear(), month.getMonth(), 1)
  const daysInMonth = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  const lead = (first.getDay() - WEEK_START + 7) % 7
  const cells: (Date | null)[] = Array.from({ length: lead }, () => null)
  for (let day = 1; day <= daysInMonth; day += 1) {
    cells.push(new Date(month.getFullYear(), month.getMonth(), day))
  }
  while (cells.length % 7 !== 0) cells.push(null)
  const weeks: (Date | null)[][] = []
  for (let index = 0; index < cells.length; index += 7) weeks.push(cells.slice(index, index + 7))
  return weeks
}

function CalendarGrid({
  selected,
  focused,
  onFocusedChange,
  onSelect,
  focusRef,
  min,
  max,
}: {
  selected: Date | null
  focused: Date
  onFocusedChange: (date: Date) => void
  onSelect: (date: Date) => void
  /** Receives the button of the focused day, so the popover can focus it on open. */
  focusRef: React.RefObject<HTMLButtonElement | null>
  /** `YYYY-MM-DD` bounds; days outside them are disabled. */
  min?: string
  max?: string
}) {
  const today = new Date()
  const monthLabel = focused.toLocaleDateString(undefined, { month: "long", year: "numeric" })
  // Set by a key press, so the effect below moves DOM focus only when the
  // keyboard moved the roving day — not on the popover's first render.
  const moveFocus = React.useRef(false)

  React.useEffect(() => {
    if (!moveFocus.current) return
    moveFocus.current = false
    focusRef.current?.focus()
  }, [focused, focusRef])

  const onKeyDown = (event: React.KeyboardEvent<HTMLTableElement>) => {
    const weekday = (focused.getDay() - WEEK_START + 7) % 7
    const next = (() => {
      switch (event.key) {
        case "ArrowLeft": return addDays(focused, -1)
        case "ArrowRight": return addDays(focused, 1)
        case "ArrowUp": return addDays(focused, -7)
        case "ArrowDown": return addDays(focused, 7)
        case "Home": return addDays(focused, -weekday)
        case "End": return addDays(focused, 6 - weekday)
        case "PageUp": return addMonths(focused, event.shiftKey ? -12 : -1)
        case "PageDown": return addMonths(focused, event.shiftKey ? 12 : 1)
        default: return null
      }
    })()
    if (!next) return
    event.preventDefault()
    moveFocus.current = true
    onFocusedChange(clampDay(next, min, max))
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <IconButton
          type="button"
          variant="ghost"
          className="size-7"
          label="Previous month"
          onClick={() => onFocusedChange(addMonths(focused, -1))}
        >
          <ChevronLeft aria-hidden="true" className="size-4" />
        </IconButton>
        <div aria-live="polite" className="text-body font-medium">
          {monthLabel}
        </div>
        <IconButton
          type="button"
          variant="ghost"
          className="size-7"
          label="Next month"
          onClick={() => onFocusedChange(addMonths(focused, 1))}
        >
          <ChevronRight aria-hidden="true" className="size-4" />
        </IconButton>
      </div>
      {/* A grid, not a list of buttons: arrow keys move by day and week, Page
          Up/Down by month, and only the focused day is in the tab order. */}
      <table role="grid" aria-label={monthLabel} className="w-full border-collapse" onKeyDown={onKeyDown}>
        <thead>
          <tr>
            {WEEKDAYS.map(day => (
              <th
                key={day.getDay()}
                scope="col"
                abbr={day.toLocaleDateString(undefined, { weekday: "long" })}
                className="text-fg-tertiary pb-1 text-center text-caption font-normal"
              >
                {day.toLocaleDateString(undefined, { weekday: "narrow" })}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {monthWeeks(focused).map((week, weekIndex) => (
            <tr key={weekIndex}>
              {week.map((day, dayIndex) => {
                if (!day) return <td key={dayIndex} />
                const isSelected = !!selected && sameDay(day, selected)
                const isFocused = sameDay(day, focused)
                const isDisabled = outOfRange(day, min, max)
                return (
                  <td key={dayIndex} aria-selected={isSelected} className="p-0 text-center">
                    <button
                      ref={isFocused ? focusRef : undefined}
                      type="button"
                      tabIndex={isFocused ? 0 : -1}
                      aria-label={day.toLocaleDateString(undefined, {
                        weekday: "long",
                        month: "long",
                        day: "numeric",
                        year: "numeric",
                      })}
                      aria-current={sameDay(day, today) ? "date" : undefined}
                      disabled={isDisabled}
                      onClick={() => onSelect(day)}
                      className={cn(
                        "inline-flex size-8 items-center justify-center rounded-md text-body outline-none transition-colors",
                        "hover:bg-surface-hover focus-visible:ring-ring/50 focus-visible:ring-[3px]",
                        "disabled:pointer-events-none disabled:text-fg-faint disabled:opacity-50",
                        sameDay(day, today) && !isSelected && "font-semibold text-primary",
                        isSelected && "bg-accent-solid text-accent-solid-fg hover:bg-accent-solid/90",
                      )}
                    >
                      {day.getDate()}
                    </button>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export interface DateTimePickerProps {
  /** `YYYY-MM-DDTHH:mm` in local time, or '' for no value. */
  value: string
  onChange: (value: string) => void
  /** Id of the date button, so a `<Label htmlFor>` can point at the control. */
  id?: string
  /** Names the whole control; the date button and time field extend it. */
  label: string
  "aria-describedby"?: string
  disabled?: boolean
  className?: string
}

export function DateTimePicker({
  value,
  onChange,
  id,
  label,
  "aria-describedby": describedBy,
  disabled,
  className,
}: DateTimePickerProps) {
  const { date, time } = splitValue(value)
  const selected = parseDateKey(date)
  const [open, setOpen] = React.useState(false)
  const [focused, setFocused] = React.useState<Date>(() => selected ?? new Date())
  const focusRef = React.useRef<HTMLButtonElement | null>(null)

  const onOpenChange = (next: boolean) => {
    // Each visit starts on the chosen day (or today), not where the last one left off.
    if (next) setFocused(selected ?? new Date())
    setOpen(next)
  }

  const selectDate = (day: Date) => {
    onChange(`${toDateKey(day)}T${time || "00:00"}`)
    setOpen(false)
  }

  const changeTime = (nextTime: string) => {
    // A cleared time field has nothing to send; the last full value stands.
    if (!TIME_PART.test(nextTime)) return
    onChange(`${date || toDateKey(new Date())}T${nextTime}`)
  }

  return (
    <div role="group" aria-label={label} className={cn("flex items-center gap-1.5", className)}>
      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger asChild>
          <Button
            id={id}
            type="button"
            variant="outline"
            disabled={disabled}
            aria-describedby={describedBy}
            // An explicit name, not an sr-only prefix: name computation trims
            // each inline child and joins them without a space, which read the
            // prefix and the date as one word ("date:Jan 14, 2026").
            aria-label={`${label}, date: ${date ? formatDate(date) : "none picked"}`}
            className="h-8 justify-start gap-1.5 px-2.5 text-body font-normal"
          >
            <CalendarDays aria-hidden="true" className="size-3.5 text-fg-tertiary" />
            {date ? formatDate(date) : <span className="text-fg-tertiary">Pick a date</span>}
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-auto p-3"
          aria-label={`${label}: choose a date`}
          onOpenAutoFocus={event => {
            // Straight onto the chosen day, as a date picker's keyboard users expect.
            event.preventDefault()
            focusRef.current?.focus()
          }}
        >
          <CalendarGrid
            selected={selected}
            focused={focused}
            onFocusedChange={setFocused}
            onSelect={selectDate}
            focusRef={focusRef}
          />
        </PopoverContent>
      </Popover>
      <Input
        type="time"
        aria-label={`${label}, time`}
        aria-describedby={describedBy}
        value={time}
        disabled={disabled}
        onChange={event => changeTime(event.target.value)}
        className="h-8 w-[104px] text-body md:text-body"
      />
    </div>
  )
}


export interface DatePickerProps {
  /** `YYYY-MM-DD`, or '' for no value. */
  value: string
  onChange: (value: string) => void
  /** Id of the date button, so a `<Label htmlFor>` can point at the control. */
  id?: string
  /** Names the control; the button's name adds the picked day. */
  label: string
  /** `YYYY-MM-DD` bounds; days outside them cannot be picked. */
  min?: string
  max?: string
  "aria-describedby"?: string
  disabled?: boolean
  className?: string
}

/**
 * The date half of {@link DateTimePicker} on its own, for a day filter: the
 * value is the native `type="date"` wire format, `YYYY-MM-DD`, so a caller
 * that read a date input keeps parsing it the same way (F27).
 */
export function DatePicker({
  value,
  onChange,
  id,
  label,
  min,
  max,
  "aria-describedby": describedBy,
  disabled,
  className,
}: DatePickerProps) {
  const date = DATE_PART.test(value) ? value : ""
  const selected = parseDateKey(date)
  const [open, setOpen] = React.useState(false)
  const [focused, setFocused] = React.useState<Date>(() => clampDay(selected ?? new Date(), min, max))
  const focusRef = React.useRef<HTMLButtonElement | null>(null)

  const onOpenChange = (next: boolean) => {
    if (next) setFocused(clampDay(selected ?? new Date(), min, max))
    setOpen(next)
  }

  const selectDate = (day: Date) => {
    if (outOfRange(day, min, max)) return
    onChange(toDateKey(day))
    setOpen(false)
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          disabled={disabled}
          aria-describedby={describedBy}
          aria-label={`${label}: ${date ? formatDate(date) : "none picked"}`}
          className={cn("h-8 justify-start gap-1.5 px-2.5 text-body font-normal", className)}
        >
          <CalendarDays aria-hidden="true" className="size-3.5 text-fg-tertiary" />
          {date ? formatDate(date) : <span className="text-fg-tertiary">Pick a date</span>}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-auto p-3"
        aria-label={`${label}: choose a date`}
        onOpenAutoFocus={event => {
          event.preventDefault()
          focusRef.current?.focus()
        }}
      >
        <CalendarGrid
          selected={selected}
          focused={focused}
          onFocusedChange={setFocused}
          onSelect={selectDate}
          focusRef={focusRef}
          min={min}
          max={max}
        />
      </PopoverContent>
    </Popover>
  )
}
