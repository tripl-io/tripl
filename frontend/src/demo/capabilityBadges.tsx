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
