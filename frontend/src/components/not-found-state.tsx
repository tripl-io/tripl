import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { projectHomePath, workspacePath } from '@/lib/navigation'

const DEFAULT_DESCRIPTION = 'The page you’re looking for doesn’t exist or may have moved.'

/** The project a missing page sat under, when the URL named one that exists. */
interface NotFoundProject {
  slug: string
  name: string
}

interface NotFoundStateProps {
  /** Headline. Defaults to the generic route-level message. */
  title?: string
  /** One-sentence explanation under the headline. */
  description?: string
  /**
   * Offer the project as well as the portfolio. Only pass a project that is
   * known to exist — this is the way back for a mistyped sub-path, not a guess
   * at the slug in the URL.
   */
  project?: NotFoundProject
  /**
   * Where "Back to all projects" goes. Defaults to the active organization's
   * workspace; a page about an organization the user is not in points
   * elsewhere, since that workspace is the thing that is missing.
   */
  homeHref?: string
}

/**
 * The shared "there is nothing here" panel. Used by the catch-all route
 * (pages/NotFoundPage) and — with project-specific wording — by the app shell when the `:slug` in the
 * URL matches no project the viewer can see.
 */
export function NotFoundState({
  title = 'Page not found',
  description = DEFAULT_DESCRIPTION,
  project,
  homeHref,
}: NotFoundStateProps) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center px-6 text-center">
      <p className="tnum text-body-sm font-semibold tracking-wide text-fg-tertiary">
        404
      </p>
      <h1 className="mt-2 text-title font-semibold text-fg">
        {title}
      </h1>
      <p className="mt-2 max-w-sm text-body text-fg-secondary">
        {description}
      </p>
      {/* A project-scoped 404 keeps that project's sidebar and breadcrumb, so
          ejecting to the portfolio was two navigations away from where the
          reader actually was (tripl-tvqk). When we know the project, it leads
          and the portfolio stays as the secondary way out. */}
      <div className="mt-6 flex flex-wrap items-center justify-center gap-2.5">
        {/* Button, not links hand-painted in the accent: those skipped the
            primitive's hover, focus ring and dark-mode fill (DS-14, AU-7). */}
        {project && (
          <Button asChild size="lg">
            <Link to={projectHomePath(project.slug)}>Back to {project.name}</Link>
          </Button>
        )}
        <Button asChild size="lg" variant={project ? 'outline' : 'default'}>
          <Link to={homeHref ?? workspacePath()}>{project ? 'All projects' : 'Back to all projects'}</Link>
        </Button>
      </div>
    </div>
  )
}
