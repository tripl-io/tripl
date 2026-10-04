import { useId, type ReactNode } from 'react'
import { CalendarDays } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { DatePicker } from '@/components/ui/date-time-picker'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { formatDate } from '@/lib/datetime'
import { cn } from '@/lib/utils'

export interface DateRangeFilterProps {
  /** The chip's word, as FilterSelect spells its own: "Fired: any". */
  label: string
  /** `YYYY-MM-DD` or '' — the range's two ends, as a date input holds them. */
  from: string
  to: string
  /**
   * Both ends in ONE call, never two: the callers build the next state from
   * the props of the last render, so clearing `from` and then `to` in turn
   * let the second write restore the first.
   */
  onRangeChange: (next: { from: string; to: string }) => void
  /** Labels of the two inputs inside the popover. */
  fromLabel?: string
  toLabel?: string
  /** Earliest day the range may start on, when the list has a floor. */
  min?: string
  /** A caveat about what the range can reach, shown under the inputs. */
  hint?: ReactNode
}

/** "any", "from Sep 3, 2026", "to Sep 9, 2026" or "Sep 3, 2026 – Sep 9, 2026". */
function rangeSummary(from: string, to: string): string {
  const start = from ? formatDate(from) : ''
  const end = to ? formatDate(to) : ''
  if (start && end) return `${start} – ${end}`
  if (start) return `from ${start}`
  if (end) return `to ${end}`
  return 'any'
}

/**
 * A date range as ONE filter chip, the same shape as a FilterSelect — dashed
 * while unset, accent once set — that opens the two date inputs in a popover.
 *
 * Two labelled inputs and a caveat paragraph used to sit in the bar itself, so
 * at 390px the filters stood ~330px tall before the first incident, and the
 * delivery log's bar was a different shape from the inbox's beside it. Both
 * bars now render this, so the two tabs share one idiom.
 */
export function DateRangeFilter({
  label,
  from,
  to,
  onRangeChange,
  fromLabel = 'From',
  toLabel = 'To',
  min,
  hint,
}: DateRangeFilterProps) {
  const fromId = useId()
  const toId = useId()
  const set = !!(from || to)
  const summary = rangeSummary(from, to)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          // The value belongs in the name, as on FilterSelect: a trigger named
          // only "Fired filter" never said what is set.
          aria-label={`${label} filter: ${summary}`}
          data-active={set || undefined}
          className={cn(
            'h-7 w-auto gap-1.5 px-2.5 text-caption font-normal',
            set
              ? 'border-accent bg-accent-soft text-fg'
              : 'border-dashed bg-transparent text-fg-muted',
          )}
        >
          <CalendarDays aria-hidden="true" className="size-3.5" />
          <span className="font-medium">{label}:</span>
          <span>{summary}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto max-w-[min(20rem,calc(100vw-2rem))] space-y-3 p-3">
        <div className="grid grid-cols-2 gap-2">
          <div className="grid gap-1">
            <Label htmlFor={fromId} className="text-caption font-normal text-fg-muted">
              {fromLabel}
            </Label>
            {/* The app's own calendar, not <input type="date">: the native
                control rendered "mm/dd/yyyy" whatever the theme. Same
                YYYY-MM-DD value, so nothing downstream changes. */}
            <DatePicker
              id={fromId}
              label={fromLabel}
              min={min}
              max={to || undefined}
              value={from}
              onChange={next => onRangeChange({ from: next, to })}
              className="w-full"
            />
          </div>
          <div className="grid gap-1">
            <Label htmlFor={toId} className="text-caption font-normal text-fg-muted">
              {toLabel}
            </Label>
            <DatePicker
              id={toId}
              label={toLabel}
              min={from || min}
              value={to}
              onChange={next => onRangeChange({ from, to: next })}
              className="w-full"
            />
          </div>
        </div>
        {hint && <p className="text-caption text-fg-muted">{hint}</p>}
        {set && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onRangeChange({ from: '', to: '' })}
          >
            Clear dates
          </Button>
        )}
      </PopoverContent>
    </Popover>
  )
}
