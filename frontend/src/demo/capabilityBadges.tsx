/**
 * Honest capability labels for demo mode.
 *
 * These badges keep synthetic/local/simulated artifacts visually distinct from
 * real/external ones — they use a `warning` tone (never `success`/green), so a
 * local simulated delivery is never mistaken for a real Slack/Jira/email send,
 * and a synthetic warehouse is never offered as a real connection.
 */

import { FlaskConical } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'

/** "Local synthetic data" — the top-level marker for a demo workspace. */
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

/**
 * Marks an alert delivery recorded by a local demo sink (no external send). A
 * status pill like `AlertDeliveryRow`'s, and never a success tone — a simulated
 * delivery must not read as a real one.
 */
export function LocalDeliveryBadge({ simulated }: { simulated?: boolean }) {
  return (
    <Chip
      tone="warning"
      title="Recorded locally by the demo sink — nothing was sent to an external channel"
    >
      {simulated ? 'Local · simulated' : 'Local'}
    </Chip>
  )
}
