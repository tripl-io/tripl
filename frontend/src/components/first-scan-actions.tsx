import { Link } from 'react-router-dom'
import { ArrowRight, Database } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { currentOrgSlug, projectPath, settingsPath } from '@/lib/navigation'

/**
 * The two first-run steps the empty states offer, one button each, so every
 * page names and draws them alike. They used to read "Connect a data source",
 * "Add connection", "Run a scan" (which ran nothing) and "Go to Scans", in
 * three button sizes. "Data source" is the glossary's noun.
 *
 * Data sources are owner-only: the caller offers the connect button only to
 * an owner and tells anyone else who can.
 */
export function ConnectDataSourceButton() {
  return (
    <Button asChild size="sm">
      <Link to={settingsPath('/settings/data-sources')} className="no-underline">
        <Database aria-hidden="true" />
        Connect a data source
      </Link>
    </Button>
  )
}

/** The step after a data source: the Scans page, where a scan is set up and run. */
export function GoToScansButton({ slug }: { slug: string }) {
  return (
    <Button asChild size="sm">
      <Link to={projectPath(currentOrgSlug(), slug, '/scans')} className="no-underline">
        Go to Scans
        <ArrowRight aria-hidden="true" />
      </Link>
    </Button>
  )
}
