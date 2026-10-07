/**
 * Where the last chapter leads: out of the demo, into the real product.
 *
 * On an instance of one's own that is a real project. A public demo has none
 * to make — the server refuses blank projects there — and testers found
 * "Create a real project" at the end of the tour leading nowhere, so there the
 * way on is the quick start: tripl on one's own warehouse.
 */

import { Link } from 'react-router-dom'
import { ExternalLink, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { usePublicDemo } from '@/lib/deploymentMode'
import { workspacePath } from '@/lib/navigation'

export const QUICK_START_URL = 'https://docs.tripl.io/quick-start'

export function EndOfDemoLink() {
  const publicDemo = usePublicDemo()
  if (publicDemo) {
    return (
      <Button asChild size="xs">
        <a
          href={QUICK_START_URL}
          target="_blank"
          rel="noreferrer"
          // It leaves the app, so it says so: the icon to the eye, the name
          // (which starts with the visible label) to a screen reader.
          aria-label="Run tripl yourself (opens in a new tab)"
        >
          Run tripl yourself
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
        </a>
      </Button>
    )
  }
  // The dashboard, not Data sources: creating the project comes first, and a
  // demo-scoped link straight to the global connection page was deliberately
  // removed.
  return (
    <Button asChild size="xs">
      <Link to={workspacePath()}>
        <Plus className="h-3 w-3" />
        Create a real project
      </Link>
    </Button>
  )
}
