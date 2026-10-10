import { Fragment } from 'react'
import { Link } from 'react-router-dom'
import type { DataSource } from '@/types'
import { dataSourceUsedBy } from './used-by-scans-model'
import { formatNumber } from '@/lib/format'

/**
 * The card's "Used by" line: the scans reading this source, each a link
 * to its scan page, so the delete's reach and the way back to a scan are both
 * on the card. Falls back to the bare count when the server sent no names.
 */
export function UsedByScans({ ds }: { ds: DataSource }) {
  const usedBy = dataSourceUsedBy(ds)
  if (!usedBy) return null
  return (
    <span className="text-caption text-fg-secondary">
      {usedBy.label}
      {usedBy.links.length > 0 && ': '}
      {usedBy.links.map((link, index) => (
        <Fragment key={link.id}>
          {index > 0 && ', '}
          <Link to={link.href} className="hover:underline text-fg">
            {link.name}
          </Link>
          {link.projectName && (
            <>
              {' '}
              <span className="text-fg-tertiary">({link.projectName})</span>
            </>
          )}
        </Fragment>
      ))}
      {usedBy.more > 0 && ` and ${formatNumber(usedBy.more)} more`}
    </span>
  )
}
