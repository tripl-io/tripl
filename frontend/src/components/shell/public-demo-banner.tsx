import { FlaskConical } from 'lucide-react'
import { usePublicDemo } from '@/lib/deploymentMode'
import { ShellBanner } from './shell-banner'

/**
 * Across the app shell on a public demo: what this instance is,
 * what it does not do, and that a workspace left unused goes away. Without
 * it the refusals (no connections of one's own, no outbound mail, no AI) read
 * as faults.
 */
export function PublicDemoBanner() {
  if (!usePublicDemo()) return null
  return (
    <ShellBanner tone="accent" icon={FlaskConical} role="note" data-testid="public-demo-banner">
      <strong>Public demo.</strong> It runs on generated demo projects, connects to no
      warehouse of yours and sends nothing out. A workspace nobody opens for a while is
      deleted.
    </ShellBanner>
  )
}
