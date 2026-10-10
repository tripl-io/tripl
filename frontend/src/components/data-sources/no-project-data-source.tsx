import { Link } from 'react-router-dom'
import { settingsPath } from '@/lib/activeOrg'
import { useIsOwner } from '@/lib/permissions'

/**
 * Stands in for a data-source select when the project has no source it may
 * use, instead of a select offering only "Select data source…". It carries the
 * select's id (and takes focus) so a "A data source is required" message still
 * has somewhere to send the reader.
 *
 * Data sources are owner-only, as on the Scans page's empty state: an owner
 * gets the way to connect one, anyone else is told who can.
 */
export function NoProjectDataSource({ id }: { id: string }) {
  const isOwner = useIsOwner()
  return (
    <p id={id} tabIndex={-1} className="text-body-sm text-fg-tertiary outline-none">
      No data source in this project yet.{' '}
      {isOwner ? (
        <Link
          to={settingsPath('/settings/data-sources')}
          className="underline underline-offset-2 text-fg"
        >
          Connect one
        </Link>
      ) : (
        'An owner has to connect one first.'
      )}
    </p>
  )
}
