/**
 * Where the demo leads: out of it, into the real product. The last chapter
 * ends on it, and the demo welcome panel offers it from the first screen.
 *
 * On an instance of one's own that is a real project. A public demo has none
 * to make — the server refuses blank projects there — and testers found
 * "Create a real project" at the end of the tour leading nowhere, so there the
 * way on is the quick start: tripl on one's own warehouse.
 */

import type { ComponentProps } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLink, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { usePublicDemo } from '@/lib/deploymentMode'
import { QUICK_START_URL } from '@/lib/docsSite'
import { workspacePath } from '@/lib/navigation'

interface EndOfDemoLinkProps {
  /** The chapter's end is the call to action; beside other buttons it is quieter. */
  variant?: ComponentProps<typeof Button>['variant']
}

export function EndOfDemoLink({ variant }: EndOfDemoLinkProps = {}) {
  const publicDemo = usePublicDemo()
  if (publicDemo) {
    return (
      <Button asChild size="xs" variant={variant}>
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
  // All projects with its New project dialog already open (`?new=1`), not
  // Data sources: creating the project comes first, and a demo-scoped link
  // straight to the global connection page was deliberately removed. The list
  // alone held only the demo, and the button to press was left to find.
  return (
    <Button asChild size="xs" variant={variant}>
      <Link to={`${workspacePath()}?new=1`}>
        <Plus className="h-3 w-3" />
        Create a real project
      </Link>
    </Button>
  )
}
