import { Link } from 'react-router-dom'
import { NotFoundState } from '@/components/not-found-state'
import { TrifoldMark } from '@/components/states/brand-mark'
import { projectHomePath, workspacePath } from '@/lib/navigation'
import type { Project } from '@/types'

const PROJECT_SHORTLIST = 5

/**
 * `/p/<unknown>/…`: the address names no project the viewer can see (#237).
 *
 * The project shell cannot render here (everything in it fans out requests for
 * the slug), and a bare centred 404 on a blank screen left someone who followed
 * a stale link with nothing to hold on to. This keeps the brand mark as the way
 * home and lists the projects they CAN open, so the usual next step is one
 * click.
 */
export function ProjectNotFound({ slug, projects }: { slug: string; projects: Project[] }) {
  const shortlist = projects.slice(0, PROJECT_SHORTLIST)
  return (
    <div className="flex w-full max-w-lg flex-col items-center">
      <Link
        to={workspacePath()}
        aria-label="Tripl — home"
        className="flex items-center gap-2 rounded-control px-1 py-1 no-underline"
      >
        <TrifoldMark size={24} />
        <span className="text-heading font-bold tracking-tight text-fg">
          tripl
        </span>
      </Link>
      <NotFoundState
        title="Project not found"
        description={`No project with the address “${slug}” exists, or you do not have access to it.`}
      />
      {shortlist.length > 0 && (
        <nav aria-label="Your projects" className="mb-8 w-full max-w-sm">
          <h2 className="micro-label mb-2 text-center text-fg-tertiary">Your projects</h2>
          <ul className="m-0 list-none overflow-hidden rounded-card border border-border bg-surface p-0">
            {shortlist.map((project) => (
              <li key={project.id} className="border-b border-border-subtle last:border-b-0">
                <Link
                  to={projectHomePath(project.slug)}
                  className="flex items-center gap-2.5 px-3 py-2 text-body-sm no-underline transition-colors hover:bg-surface-hover text-fg"
                >
                  <span
                    aria-hidden="true"
                    className="flex size-5 shrink-0 items-center justify-center rounded-sm bg-surface-active text-caption font-bold text-fg-secondary"
                  >
                    {project.name.trim().charAt(0).toUpperCase() || '?'}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{project.name}</span>
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      )}
    </div>
  )
}
