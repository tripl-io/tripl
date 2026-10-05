import { FlaskConical } from 'lucide-react'
import { usePublicDemo } from '@/lib/deploymentMode'

/**
 * Across the app shell on a public demo: what this instance is,
 * what it does not do, and that a workspace left unused goes away. Without
 * it the refusals (no connections of one's own, no outbound mail, no AI) read
 * as faults.
 */
export function PublicDemoBanner() {
  if (!usePublicDemo()) return null
  return (
    <div
      role="note"
      data-testid="public-demo-banner"
      className="flex items-center gap-3 border-b px-4 py-2 text-body-sm"
      style={{ background: 'var(--accent-soft)', borderColor: 'var(--border)', color: 'var(--fg)' }}
    >
      <FlaskConical aria-hidden="true" className="size-4 shrink-0 text-accent" />
      <span className="min-w-0 flex-1">
        <strong>Public demo.</strong> It runs on generated demo projects, connects to no
        warehouse of yours and sends nothing out. A workspace nobody opens for a while is
        deleted.
      </span>
    </div>
  )
}
