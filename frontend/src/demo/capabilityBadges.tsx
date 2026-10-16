/**
 * Honest capability labels for demo mode.
 *
 * These badges keep synthetic artifacts visually distinct from real ones: they
 * use a `warning` tone (never `success`/green), so a synthetic warehouse is
 * never offered as a real connection. A delivery recorded by the local demo
 * sink is marked the same way, inline, by `AlertDeliveryRow`.
 */

import { FlaskConical } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { demoEngineOf, type DbType } from '@/types/dataSources'

/** "Local synthetic data" — the top-level marker for a demo project. */
export function DemoDataBadge({ className }: { className?: string }) {
  return (
    <Chip tone="warning" size="xs" variant="outline" className={className} icon={<FlaskConical className="h-3 w-3" />}>
      Local synthetic data
    </Chip>
  )
}

/** Marks a data source whose warehouse is the in-memory synthetic demo source. */
export function SyntheticSourceBadge({ size = 'xs' }: { size?: 'xs' | 'sm' | 'md' }) {
  return (
    <Chip tone="warning" size={size} title="Local, in-memory synthetic warehouse — not a real connection">
      Synthetic
    </Chip>
  )
}

/** What a `demo` source's badge says on hover, by the engine that serves it. */
const DEMO_ENGINE_TITLE = {
  postgres:
    "The demo's own read-only PostgreSQL warehouse, shared by every demo — not a connection anyone configured",
  memory:
    "The demo warehouse, served in memory because this server could not create its PostgreSQL database — not a connection anyone configured",
  unknown: "The demo's own warehouse, shared by every demo — not a connection anyone configured",
} as const

/**
 * The badge for a demo source: "Demo warehouse" for today's `demo` sources,
 * naming the engine on hover, and the older "Synthetic" one for a demo built
 * before them.
 */
export function DemoSourceBadge({
  source,
  size = 'xs',
}: {
  source: { db_type: DbType }
  size?: 'xs' | 'sm' | 'md'
}) {
  if (source.db_type !== 'demo') return <SyntheticSourceBadge size={size} />
  const engine = demoEngineOf(source)
  return (
    <Chip tone="warning" size={size} title={DEMO_ENGINE_TITLE[engine ?? 'unknown']}>
      Demo warehouse
    </Chip>
  )
}
