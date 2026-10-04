import { useCallback, useState, type ReactNode } from 'react'
import { Check, Filter, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { TableHead } from '@/components/ui/table'
import { cn } from '@/lib/utils'

export type ColumnFilterType = 'text' | 'enum' | 'boolean'

export function ColumnFilter({
  label,
  type,
  value,
  options,
  onChange,
}: {
  label: string
  type: ColumnFilterType
  value: string
  options?: readonly string[]
  onChange: (v: string) => void
}) {
  const [open, setOpen] = useState(false)
  const active = value !== ''

  const clear = useCallback(() => {
    onChange('')
    setOpen(false)
  }, [onChange])

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Filter ${label}`}
          title={active ? `Filter: ${value}` : `Filter ${label}`}
          className={cn(
            'tripl-col-filter inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-sm transition-opacity',
            active
              ? 'opacity-100 text-[color:var(--accent)]'
              // Hover-revealed only where there is hover: on a touch screen
              // an invisible control cannot be found at all.
              : 'opacity-0 pointer-coarse:opacity-100 text-fg-tertiary hover:text-foreground',
            open && 'opacity-100',
          )}
          onClick={(e) => e.stopPropagation()}
        >
          <Filter
            className="h-3 w-3"
            fill={active ? 'currentColor' : 'none'}
          />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="bottom"
        sideOffset={6}
        className="w-56 p-2"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1.5 px-1 micro-label text-fg-tertiary">
          {label}
        </div>
        {type === 'text' && (
          <Input
            // eslint-disable-next-line jsx-a11y/no-autofocus -- filter popover opened by explicit user action; focusing its input is expected
            autoFocus
            value={value}
            placeholder="Contains…"
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === 'Escape') setOpen(false)
            }}
            className="h-7"
          />
        )}
        {type === 'enum' && options && (
          <div className="max-h-64 overflow-y-auto">
            <FilterOptionRow
              label="All"
              checked={value === ''}
              onSelect={() => { onChange(''); setOpen(false) }}
              muted
            />
            {options.map((opt) => (
              <FilterOptionRow
                key={opt}
                label={opt}
                checked={value === opt}
                onSelect={() => { onChange(opt); setOpen(false) }}
              />
            ))}
          </div>
        )}
        {type === 'boolean' && (
          <div>
            <FilterOptionRow
              label="Any"
              checked={value === ''}
              onSelect={() => { onChange(''); setOpen(false) }}
              muted
            />
            <FilterOptionRow
              label="Yes"
              checked={value === 'true'}
              onSelect={() => { onChange('true'); setOpen(false) }}
            />
            <FilterOptionRow
              label="No"
              checked={value === 'false'}
              onSelect={() => { onChange('false'); setOpen(false) }}
            />
          </div>
        )}
        {active && (
          <div className="mt-1.5 border-t pt-1.5 border-border-subtle">
            <button
              type="button"
              onClick={clear}
              className="flex w-full items-center gap-1 rounded-sm px-2 py-1 text-left text-caption hover:bg-[var(--surface-hover)] text-fg-secondary"
            >
              <X className="h-3 w-3" />
              Clear filter
            </button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

function FilterOptionRow({
  label,
  checked,
  muted = false,
  onSelect,
}: {
  label: string
  checked: boolean
  muted?: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-body-sm hover:bg-[var(--surface-hover)]"
      style={{ color: muted ? 'var(--fg-subtle)' : 'var(--fg)' }}
    >
      <span
        className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm border"
        style={{
          background: checked ? 'var(--accent-solid)' : 'transparent',
          borderColor: checked ? 'var(--accent-solid)' : 'var(--border-strong)',
        }}
      >
        {checked && <Check className="size-3 text-accent-solid-fg" />}
      </span>
      <span className="flex-1 truncate">{label}</span>
    </button>
  )
}

export function FilterableHead({
  label,
  filter,
  className,
  align = 'left',
}: {
  label: ReactNode
  filter?: ReactNode
  className?: string
  align?: 'left' | 'right'
}) {
  return (
    <TableHead className={cn('group/th', className)}>
      <div
        className={cn(
          'flex items-center gap-1.5',
          align === 'right' && 'justify-end',
        )}
      >
        <span className="truncate">{label}</span>
        {filter}
      </div>
    </TableHead>
  )
}
