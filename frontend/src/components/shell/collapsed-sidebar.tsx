import { useRef, type ReactElement } from 'react'
import { Link } from 'react-router-dom'
import { BookOpen, ChevronRight, Search, SlidersHorizontal, type LucideIcon } from 'lucide-react'
import { BranchSwitcher } from '@/components/branch-switcher'
import { COMMAND_PALETTE_TRIGGER_ATTR } from '@/components/command-palette-context'
import { TrifoldMark } from '@/components/states/brand-mark'
import { DropdownMenu, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import type { NavGroup } from '@/lib/navigation'
import { commandPaletteShortcutLabel } from '@/lib/platform'
import { cn } from '@/lib/utils'
import type { Project } from '@/types'
import { AccountMenuContent } from './account-menu'
import { OrgSwitcherSlot } from './org-switcher-slot'
import { ProjectSwitcher } from './project-switcher'
import {
  ACTIVE_MARKER_CLASS,
  ACTIVE_ROW_CLASS,
  ICON_BUTTON_CLASS,
  isUrgentCount,
  navLinkStyle,
  projectSettingsHref,
} from './sidebar-style'
import { currentOrgSlug, projectPath, workspacePath } from '@/lib/navigation'

/** An icon-only rail entry with its name in a visible tooltip. */
function RailTip({ label, children }: { label: string; children: ReactElement }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  )
}

function RailLink({
  to,
  label,
  icon: Icon,
  active,
  urgent = false,
}: {
  to: string
  label: string
  icon: LucideIcon
  active: boolean
  /** The collapsed twin of an urgent NavCount: a red dot. */
  urgent?: boolean
}) {
  return (
    <RailTip label={label}>
      <Link
        to={to}
        aria-label={label}
        aria-current={active ? 'page' : undefined}
        className={cn(ICON_BUTTON_CLASS, active && ACTIVE_MARKER_CLASS, active && ACTIVE_ROW_CLASS)}
        style={navLinkStyle(active)}
      >
        <Icon className="size-4" aria-hidden="true" />
        {urgent && (
          <span
            aria-hidden="true"
            className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-danger"
          />
        )}
      </Link>
    </RailTip>
  )
}

/**
 * The icon rail. It keeps every control the full sidebar has — project and
 * branch switchers, Project settings, Concepts, the account menu — because a
 * persisted collapse used to leave no way to switch project or branch, see
 * which branch the pages read, or sign out without expanding it.
 */
export function CollapsedSidebar({
  onExpand,
  navGroups,
  currentPath,
  slug,
  activeProject,
  projects,
  projectsLoading,
  onPickProject,
  canCreateProject,
  conceptsActive,
  userInitials,
  userLabel,
  isLoggingOut,
  onSignOut,
  onOpenTweaks,
  onOpenPalette,
}: {
  onExpand: () => void
  navGroups: NavGroup[]
  currentPath: string
  slug: string | undefined
  activeProject: Project | undefined
  projects: Project[]
  projectsLoading: boolean
  onPickProject: (project: Project) => void
  canCreateProject: boolean
  conceptsActive: boolean
  userInitials: string
  userLabel: string
  isLoggingOut: boolean
  onSignOut: () => void
  /** Opens Appearance hung from `anchor`, the rail's account avatar. */
  onOpenTweaks: (anchor: HTMLElement | null) => void
  onOpenPalette: () => void
}) {
  const accountRef = useRef<HTMLButtonElement | null>(null)
  return (
    <TooltipProvider delayDuration={200}>
      <nav
        aria-label="Main navigation"
        className="flex h-full w-[calc(52px+env(safe-area-inset-left))] flex-shrink-0 flex-col items-center border-r pt-2.5 pb-[calc(0.625rem+env(safe-area-inset-bottom))] pl-[env(safe-area-inset-left)] bg-bg-sunken border-border"
      >
        <RailTip label="tripl — home">
          <Link
            to={workspacePath()}
            aria-label="tripl — home"
            className="mb-1.5 flex h-8 w-8 items-center justify-center rounded-md no-underline transition-colors hover:bg-sidebar-hover"
          >
            <TrifoldMark size={22} />
          </Link>
        </RailTip>
        <OrgSwitcherSlot compact />
        <ProjectSwitcher
          compact
          activeProject={activeProject}
          projects={projects}
          loading={projectsLoading}
          onPick={onPickProject}
          canCreateProject={canCreateProject}
        />
        {slug && <BranchSwitcher slug={slug} compact />}
        <RailTip label={`Search or jump — ${commandPaletteShortcutLabel()}`}>
          <button
            type="button"
            aria-label={`Search or jump — ${commandPaletteShortcutLabel()}`}
            onClick={onOpenPalette}
            {...{ [COMMAND_PALETTE_TRIGGER_ATTR]: '' }}
            className={ICON_BUTTON_CLASS}
            style={{ color: 'var(--fg-muted)' }}
          >
            <Search className="size-3.5" aria-hidden="true" />
          </button>
        </RailTip>
        <div className="mt-1 flex min-h-0 flex-1 flex-col items-center gap-0.5 overflow-y-auto">
          {navGroups.map((group) => (
            <div key={group.label} className="flex flex-col items-center gap-0.5">
              {/* The group's initial, not a bare hairline: three 20px rules
                  were all that told Plan from Observe from Govern (#238). */}
              <div
                aria-hidden="true"
                title={group.label}
                className="micro-label mt-1.5 mb-0.5 text-fg-tertiary"
              >
                {group.label.charAt(0)}
              </div>
              {group.items.map((item) => (
                <RailLink
                  key={item.id}
                  to={item.href}
                  label={item.label}
                  icon={item.icon}
                  active={item.match(currentPath)}
                  urgent={isUrgentCount(item)}
                />
              ))}
            </div>
          ))}
          {slug && (
            <>
              <div className="my-1 h-px w-5 bg-border-subtle" />
              <RailLink
                to={projectSettingsHref(slug)}
                label="Project settings"
                icon={SlidersHorizontal}
                active={false}
              />
              <RailLink
                to={projectPath(currentOrgSlug(), slug, '/concepts')}
                label="Concepts"
                icon={BookOpen}
                active={conceptsActive}
              />
            </>
          )}
        </div>
        <RailTip label="Expand sidebar">
          <button
            type="button"
            onClick={onExpand}
            aria-label="Expand sidebar"
            className={cn(ICON_BUTTON_CLASS, 'mb-1.5', 'text-fg-tertiary')}
          >
            <ChevronRight className="size-3.5" aria-hidden="true" />
          </button>
        </RailTip>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              ref={accountRef}
              type="button"
              aria-label={`Account menu — ${userLabel}`}
              title={userLabel}
              className="flex h-[26px] w-[26px] items-center justify-center rounded-full text-micro font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
              style={{ background: 'var(--avatar-bg)' }}
            >
              {userInitials}
            </button>
          </DropdownMenuTrigger>
          <AccountMenuContent
            side="right"
            align="end"
            userLabel={userLabel}
            isLoggingOut={isLoggingOut}
            onSignOut={onSignOut}
            onOpenTweaks={() => onOpenTweaks(accountRef.current)}
          />
        </DropdownMenu>
      </nav>
    </TooltipProvider>
  )
}
