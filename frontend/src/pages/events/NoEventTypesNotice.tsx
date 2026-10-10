import { Link } from 'react-router-dom'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * The event-type row of a create form in a project with no event types. An
 * event belongs to one, so an empty "Select type…" left Create blocked with
 * nothing saying a type has to exist first — and a new project starts here.
 * The single-event and the bulk form both show it in place of the picker.
 */
export function NoEventTypesNotice({ slug }: { slug: string | undefined }) {
  return (
    <p className="text-body-sm text-fg-secondary">
      This project has no event types yet, and an event belongs to one.{' '}
      <Link
        to={projectPath(currentOrgSlug(), slug, '/event-types')}
        className="underline underline-offset-2 text-accent"
      >
        Create an event type
      </Link>{' '}
      first, then come back here.
    </p>
  )
}
