import { useMemo, useState } from 'react'
import { ChevronRight, Database, Search } from 'lucide-react'
import type { TableSchema } from '@/types/dataSourceSchema'
import { SEARCH_DEBOUNCE_MS, useDebouncedValue } from '@/hooks/useDebouncedValue'
import { countOf } from '@/lib/plural'

// While searching, every matching table is force-expanded, so one keystroke on
// a warehouse with hundreds of tables used to mount thousands of column
// buttons. The filter runs on a debounced query and lists at most this
// many tables, then says how many more matched.
const SEARCH_TABLE_LIMIT = 50
// A table that matches by name is expanded with ALL its columns, so the table
// cap alone still let a one-letter query mount 50 wide tables' worth of rows.
// While searching, the column rows across every shown table share this budget;
// a table past it says how many of its columns were left out.
const SEARCH_COLUMN_LIMIT = 500

/**
 * Collapsible table/column picker rendered under {@link SqlEditor}. Lists the
 * data source's tables (introspected via the schema endpoint); expanding a table
 * reveals its columns with types. Clicking a table or column name inserts it at
 * the editor cursor via `onInsert`. A search box filters by table and column
 * name. Purely presentational — all schema data is supplied by the caller.
 */
export function SqlSchemaBrowser({
  tables,
  onInsert,
}: {
  tables: TableSchema[]
  onInsert: (text: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())

  const q = useDebouncedValue(query, SEARCH_DEBOUNCE_MS).trim().toLowerCase()

  // When searching, every shown table is force-expanded and only its matching
  // columns are listed; otherwise expansion is manual and all columns show.
  const entries = useMemo(() => {
    return tables
      .map(table => {
        const nameMatch = !q || table.name.toLowerCase().includes(q)
        const columns =
          q && !nameMatch
            ? table.columns.filter(column => column.name.toLowerCase().includes(q))
            : table.columns
        return { table, columns, visible: nameMatch || columns.length > 0 }
      })
      .filter(entry => entry.visible)
  }, [tables, q])
  const shownEntries = useMemo(() => {
    if (!q) return entries.map(entry => ({ ...entry, hiddenColumns: 0 }))
    let budget = SEARCH_COLUMN_LIMIT
    return entries.slice(0, SEARCH_TABLE_LIMIT).map(entry => {
      const columns = entry.columns.slice(0, budget)
      budget -= columns.length
      return { ...entry, columns, hiddenColumns: entry.columns.length - columns.length }
    })
  }, [entries, q])
  const hiddenMatches = entries.length - shownEntries.length

  const toggle = (name: string) =>
    setExpanded(current => {
      const next = new Set(current)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  const isOpen = (name: string) => Boolean(q) || expanded.has(name)

  return (
    <div className="rounded-control" style={{ border: '1px solid var(--border)', background: 'var(--bg)' }}>
      <button
        type="button"
        onClick={() => setOpen(value => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-caption font-medium text-fg-secondary"
      >
        <ChevronRight
          size={12}
          style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 120ms' }}
        />
        <Database size={12} />
        Tables
        <span className="text-fg-tertiary">· {tables.length}</span>
      </button>

      {open && (
        <div className="border-t border-border-subtle">
          <div className="relative px-2 pt-2">
            <Search
              size={12}
              className="pointer-events-none absolute left-[14px] top-1/2 -translate-y-1/2 text-fg-tertiary mt-1"
            />
            <input
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="Filter tables & columns…"
              aria-label="Filter tables and columns"
              className="w-full"
              style={{
                height: 28,
                borderRadius: 'var(--radius)',
                border: '1px solid var(--input)',
                background: 'var(--bg-sunken)',
                color: 'var(--fg)',
                fontSize: 'var(--text-body-sm)',
                padding: '0 8px 0 26px',
              }}
            />
          </div>

          <ul className="max-h-[220px] overflow-y-auto px-1 py-1.5" aria-label="Schema tables">
            {entries.length === 0 ? (
              <li className="px-2 py-1.5 text-body-sm text-fg-tertiary">
                No matching tables.
              </li>
            ) : (
              shownEntries.map(({ table, columns, hiddenColumns }) => (
                <li key={table.name}>
                  <div className="flex items-center">
                    <button
                      type="button"
                      onClick={() => toggle(table.name)}
                      aria-expanded={isOpen(table.name)}
                      aria-label={`Toggle columns for ${table.name}`}
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-tertiary"
                    >
                      <ChevronRight
                        size={12}
                        style={{
                          transform: isOpen(table.name) ? 'rotate(90deg)' : 'none',
                          transition: 'transform 120ms',
                        }}
                      />
                    </button>
                    <button
                      type="button"
                      onClick={() => onInsert(table.name)}
                      title={`Insert ${table.name}`}
                      className="mono min-w-0 flex-1 truncate rounded-sm px-1.5 py-1 text-left text-body-sm transition-colors hover:bg-[var(--surface-hover)] text-fg"
                    >
                      {table.name}
                    </button>
                  </div>
                  {isOpen(table.name) && (
                    <ul className="mb-1 ml-6 border-l pl-1.5 border-border-subtle">
                      {columns.map(column => (
                        <li key={column.name}>
                          <button
                            type="button"
                            onClick={() => onInsert(column.name)}
                            title={`Insert ${column.name}`}
                            className="flex w-full items-center justify-between gap-2 rounded-sm px-1.5 py-[3px] text-left transition-colors hover:bg-[var(--surface-hover)]"
                          >
                            <span className="mono truncate text-caption text-fg-secondary">
                              {column.name}
                            </span>
                            <span className="mono shrink-0 text-micro text-fg-tertiary">
                              {column.data_type}
                            </span>
                          </button>
                        </li>
                      ))}
                      {hiddenColumns > 0 && (
                        <li className="px-1.5 py-[3px] text-caption text-fg-tertiary">
                          {countOf(hiddenColumns, 'more column', 'more columns')} —
                          refine the filter to see them.
                        </li>
                      )}
                    </ul>
                  )}
                </li>
              ))
            )}
            {hiddenMatches > 0 && (
              <li className="px-2 py-1.5 text-caption text-fg-tertiary">
                {countOf(hiddenMatches, 'more matching table', 'more matching tables')} — refine the filter to see
                them.
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}
