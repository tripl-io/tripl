import { type ReactNode } from 'react'
import { Database } from 'lucide-react'
import type { DbType } from '@/types'
import { cn } from '@/lib/utils'

// The mockup keys SrcIcon off a platform kind (web/ios/...) the real model lacks.
// We key off the data source's db_type instead, using one warehouse glyph with a
// per-engine accent hue so different sources stay visually distinct.
const DB_HUE: Record<DbType, string> = {
  clickhouse: 'oklch(0.62 0.17 65)',
  postgres: 'oklch(0.62 0.12 240)',
  bigquery: 'oklch(0.62 0.16 290)',
  databricks: 'oklch(0.6 0.19 30)',
  snowflake: 'oklch(0.66 0.13 220)',
  // Local demo synthetic warehouse — a distinct teal so it never reads as a
  // real engine.
  synthetic: 'oklch(0.62 0.13 175)',
}

export function SrcIcon({ dbType, size = 30 }: { dbType: DbType | null; size?: number }) {
  const bg = dbType ? DB_HUE[dbType] : 'var(--fg-faint)'
  return (
    <div
      className="flex shrink-0 items-center justify-center rounded-lg text-white"
      style={{ width: size, height: size, background: bg }}
    >
      <Database style={{ width: size * 0.5, height: size * 0.5 }} />
    </div>
  )
}


// ─── Key/value display row used in the Overview panels ───
export function KV({
  label,
  value,
  mono = false,
}: {
  label: string
  value: ReactNode
  mono?: boolean
}) {
  return (
    <div
      className="flex items-start gap-3.5 border-t px-4 py-2.5 first:border-t-0 border-border-subtle"
    >
      <span className="w-[150px] shrink-0 text-body-sm text-fg-tertiary">
        {label}
      </span>
      <span
        className={cn('min-w-0 flex-1 text-body-sm text-fg', mono && 'mono')}
      >
        {value}
      </span>
    </div>
  )
}

// Placeholder for an empty value (the mockup's NONE token).
export function NoneTag() {
  return <span className="text-fg-tertiary">none</span>
}

// Back link used on detail / create surfaces.
export function BackLink({ onClick, label = 'Scans' }: { onClick: () => void; label?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1 text-caption transition-colors text-fg-secondary"
    >
      <span aria-hidden>←</span> {label}
    </button>
  )
}
