import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, ChevronsUpDown, FlaskConical, LayoutDashboard, LayoutGrid, Plus } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { countOf } from '@/lib/plural'
import type { Project } from '@/types'
import { ICON_BUTTON_CLASS } from './sidebar-style'
import { workspacePath } from '@/lib/navigation'

/** Above this many projects the switcher gets a filter field (#238). */
const PROJECT_FILTER_THRESHOLD = 6

/** The project's letter tile: the same one on the trigger and on every row. */
function ProjectTile({ name, size = 'md' }: { name: string | undefined; size?: 'sm' | 'md' }) {
  const letter = (name ?? '').trim().charAt(0).toUpperCase()
  return (
    <span
      aria-hidden="true"
      className={cn(
        'flex shrink-0 items-center justify-center rounded-sm font-bold',
        size === 'sm' ? 'size-5 text-micro' : 'h-[22px] w-[22px] text-caption',
        'bg-surface-active text-fg-secondary',
      )}
    >
      {letter || <LayoutGrid className="size-3" />}
    </span>
  )
}

/**
 * The project switcher (#238): each row carries the project's letter
 * tile (every row used to be the same folder icon) and a flask for demos; a
 * filter field appears once the list is long; "New project" sits above "View
 * all projects" for anyone who can create one. On the workspace, with nothing
 * picked, it reads "Choose a project" over a neutral grid, not "? Select
 * project / No project selected", which looked like an error.
 */
export function ProjectSwitcher({
  activeProject,
  projects,
  loading,
  onPick,
  canCreateProject,
  compact = false,
}: {
  activeProject: Project | undefined
  projects: Project[]
  loading: boolean
  onPick: (project: Project) => void
  canCreateProject: boolean
  compact?: boolean
}) {
  const [filter, setFilter] = useState('')
  const displayName = activeProject?.name ?? (loading ? 'Loading…' : 'Choose a project')
  // On a workspace route no project is active: a neutral count, NOT
  // projects[0], which showed a real project's slug as if it were selected.
  // With none at all it says so, in the words the open menu uses, rather than
  // offering a choice among "0 projects".
  const subtitle =
    activeProject?.slug ??
    (loading
      ? 'loading…'
      : projects.length === 0
        ? 'No projects yet'
        : countOf(projects.length, 'project', 'projects'))
  const showFilter = projects.length > PROJECT_FILTER_THRESHOLD
  const needle = filter.trim().toLowerCase()
  const shown = needle
    ? projects.filter(
        (project) =>
          project.name.toLowerCase().includes(needle) || project.slug.toLowerCase().includes(needle),
      )
    : projects

  return (
    <DropdownMenu onOpenChange={(open) => !open && setFilter('')}>
      <DropdownMenuTrigger asChild>
        {compact ? (
          <button
            type="button"
            aria-label={`Switch project (current: ${displayName})`}
            title={displayName}
            className={cn(ICON_BUTTON_CLASS, 'mb-1')}
          >
            <ProjectTile name={activeProject?.name} />
          </button>
        ) : (
          <button
            type="button"
            className="flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left transition-colors hover:bg-sidebar-hover bg-surface border-border-subtle"
          >
            <ProjectTile name={activeProject?.name} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-body-sm font-semibold leading-[1.1]">
                {displayName}
              </div>
              <div
                className="mt-px text-micro leading-[1.1] truncate text-fg-tertiary"
              >
                {subtitle}
              </div>
            </div>
            <ChevronsUpDown
              className="size-3 shrink-0 text-fg-tertiary"
              aria-hidden="true"
            />
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        side={compact ? 'right' : 'bottom'}
        sideOffset={6}
        className="w-[260px]"
      >
        <DropdownMenuLabel
          className="micro-label text-fg-tertiary"
        >
          Projects
        </DropdownMenuLabel>
        {showFilter && (
          <div className="px-1.5 pb-1.5">
            <input
              type="search"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              // The menu's typeahead would otherwise eat every letter.
              onKeyDown={(event) => event.stopPropagation()}
              placeholder="Filter projects…"
              aria-label="Filter projects"
              className="h-7 w-full rounded-control border px-2 text-body-sm outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] bg-surface border-border"
            />
          </div>
        )}
        {projects.length === 0 && !loading && (
          <div className="px-2 py-1.5 text-body-sm text-fg-tertiary">
            No projects yet
          </div>
        )}
        {loading && projects.length === 0 && (
          <div className="px-2 py-1.5 text-body-sm text-fg-tertiary">
            Loading…
          </div>
        )}
        {needle && shown.length === 0 && (
          <div className="px-2 py-1.5 text-body-sm text-fg-tertiary">
            No project matches “{filter.trim()}”
          </div>
        )}
        <div className="max-h-[320px] overflow-y-auto">
          {shown.map((project) => {
            const isActive = activeProject?.id === project.id
            return (
              <DropdownMenuItem
                key={project.id}
                onSelect={() => onPick(project)}
                className="flex items-center gap-2 text-body-sm"
              >
                <ProjectTile name={project.name} size="sm" />
                <div className="min-w-0 flex-1">
                  <div className="truncate">{project.name}</div>
                  <div
                    className="mono truncate text-micro text-fg-tertiary"
                  >
                    {project.slug}
                  </div>
                </div>
                {project.is_demo && (
                  <FlaskConical
                    className="size-3.5 shrink-0 text-fg-tertiary"
                    aria-label="Demo project"
                    role="img"
                  />
                )}
                {isActive && (
                  <Check className="size-3.5 shrink-0 text-accent" aria-hidden="true" />
                )}
              </DropdownMenuItem>
            )
          })}
        </div>
        <DropdownMenuSeparator />
        {canCreateProject && (
          <DropdownMenuItem asChild>
            <Link
              to={`${workspacePath()}?new=1`}
              className="flex items-center gap-2 text-body-sm no-underline text-fg"
            >
              <Plus className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
              New project
            </Link>
          </DropdownMenuItem>
        )}
        <DropdownMenuItem asChild>
          <Link
            to={workspacePath()}
            className="flex items-center gap-2 text-body-sm no-underline text-fg"
          >
            <LayoutDashboard
              className="size-3.5 shrink-0 text-fg-tertiary"
              aria-hidden="true"
            />
            View all projects
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
