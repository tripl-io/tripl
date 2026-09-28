import { Suspense, useEffect, useState } from 'react'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { Link, Navigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { projectsQueryOptions } from '@/lib/queryKeys'
import { projectHomePath, workspacePath } from '@/lib/navigation'
import { OnboardingReturnBar } from '@/components/onboarding-return-bar'
import { useAuth } from '@/components/auth-context'
import { ErrorState } from '@/components/error-state'
import { SCard, SHeader } from '@/components/settings/kit'
import { SettingsLayout } from '@/components/settings/SettingsLayout'
import { SETTINGS_STORAGE_KEY, sectionLabel } from '@/components/settings/nav'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { LEAVE_CONFIRMED } from '@/components/settings/unsaved-changes'
import type { Project } from '@/types'
import { isOwner as isOwnerRole, isPlatformAdmin } from '@/lib/permissions'
import { orgStorageKey } from '@/lib/activeOrg'
import { ORG_SECTION_PATHS, orgSectionForPath } from './org-settings/orgSettingsModel'
import { ORG_TRACKERS_PATH } from './org-settings/orgTrackersModel'

const ProjectGeneralSection = lazyWithReload(() => import('./ProjectGeneralSection'))
const PlanRulesSection = lazyWithReload(() => import('./PlanRulesSection'))
const ProjectMembersSection = lazyWithReload(() => import('./ProjectMembersSection'))
const MembersSection = lazyWithReload(() => import('./MembersSection'))
const OrganizationGeneralSection = lazyWithReload(() => import('./OrganizationGeneralSection'))
const InvitationsSection = lazyWithReload(() => import('./InvitationsSection'))
const DataSourcesSection = lazyWithReload(() => import('./DataSourcesSection'))
const ApiKeysSection = lazyWithReload(() => import('./ApiKeysSection'))
const ProfileSection = lazyWithReload(() => import('./ProfileSection'))
const SecuritySection = lazyWithReload(() => import('./SecuritySection'))
const InstanceSection = lazyWithReload(() => import('./InstanceSection'))
const WorkspaceAuditSection = lazyWithReload(() => import('./WorkspaceAuditSection'))
const OrgSettingsSection = lazyWithReload(() => import('./OrgSettingsSection'))
const OrgTrackersSection = lazyWithReload(() => import('./OrgTrackersSection'))
const OrgGroupsSection = lazyWithReload(() => import('./OrgGroupsSection'))
// The platform console (F20): its own chunks, fetched by platform admins alone.
const PlatformOrgsSection = lazyWithReload(() => import('@/pages/platform/PlatformOrgsSection'))
const PlatformOrgDetailSection = lazyWithReload(() => import('@/pages/platform/PlatformOrgDetailSection'))
const PlatformUsersSection = lazyWithReload(() => import('@/pages/platform/PlatformUsersSection'))

/** The console's organization list, which its detail pages belong to in the rail. */
const PLATFORM_ORGS_SECTION = 'platform/orgs'

/**
 * The rail entry a section lights up: an organization's console page
 * (`platform/orgs/<slug>`) belongs to Organizations.
 */
function railPathFor(section: string): string {
  return section.startsWith(`${PLATFORM_ORGS_SECTION}/`) ? PLATFORM_ORGS_SECTION : section
}

const LAST_SLUG_STORAGE_KEY = 'tripl-last-project-slug'

/**
 * Resolve the project the Project-scoped settings target. Prefer the slug in the
 * URL — a route param, or the `?project=` every in-app link to these sections
 * carries — otherwise the last project visited. The last-visited key is shared
 * by every tab, so it is only the fallback for a bare address: reading it first
 * opened the OTHER tab's project, danger zone included (SHELL-20).
 * The sidebar's usePersistLastSlug writes the last-visited slug to this same
 * localStorage key on every project route; we only read it here.
 *
 * There is deliberately NO "first project" fallback: falling back to
 * `projects[0]` bound Settings to whichever project happened to sort first and
 * then offered to rename/delete it, one click after the workspace page said "no
 * project selected" (tripl-jfm3.32). With nothing chosen we return undefined
 * and the project sections ask the user to pick one.
 */
function useSettingsSlug(pickedSlug: string | null): string | undefined {
  const { slug: urlSlug } = useParams<{ slug?: string }>()
  const [searchParams] = useSearchParams()
  const queryProject = searchParams.get('project')
  const projectsQuery = useQuery(projectsQueryOptions())
  const projects = projectsQuery.data ?? []
  if (urlSlug) return urlSlug
  if (queryProject) return queryProject
  if (pickedSlug) return pickedSlug
  let last: string | null = null
  try {
    last = localStorage.getItem(orgStorageKey(LAST_SLUG_STORAGE_KEY))
  } catch {
    /* ignore */
  }
  if (last && projects.some((p) => p.slug === last)) return last
  return undefined
}

/**
 * A section's chunk loading. Every section draws its own `SHeader`, which is
 * lazy with the rest of it, so on a cold load the page had no title at all,
 * only "Loading…" at the top left (#237 ST-35). The fallback names the page
 * from the rail's label and draws the cards' shape under it.
 */
function SectionFallback({ section }: { section: string }) {
  const title = sectionLabel(section)
  return (
    <div>
      {title && <SHeader title={title} />}
      <SectionSkeleton variant="form" label={title ? `Loading ${title}…` : 'Loading…'} />
    </div>
  )
}

/** The page title above a state that is not the section itself (ST-36). */
function StateHeader({ section }: { section: string }) {
  const title = sectionLabel(section)
  return title ? <SHeader title={title} /> : null
}

/**
 * The full-takeover Settings area. A single page mounted at /settings/* routes;
 * it reads the active section from the URL, renders the matching config section
 * inside the takeover layout, persists the last section, and gates Instance
 * sections to owners.
 */
export default function SettingsArea({ section }: { section: string }) {
  const auth = useAuth()
  const isOwner = isOwnerRole(auth.user?.role)
  const platformAdmin = isPlatformAdmin(auth.user)
  const [pickedSlug, setPickedSlug] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const slug = useSettingsSlug(pickedSlug)
  const projectsQuery = useQuery(projectsQueryOptions())
  const projects = projectsQuery.data ?? []
  const projectName = projects.find((p) => p.slug === slug)?.name

  // Picking a project from the empty state writes the same last-visited key the
  // sidebar maintains, so the choice survives a reload the way one made in the
  // app does.
  const pickProject = (picked: string) => {
    try {
      localStorage.setItem(orgStorageKey(LAST_SLUG_STORAGE_KEY), picked)
    } catch {
      /* ignore */
    }
    setPickedSlug(picked)
  }

  // A save that renames the project moves it to a new address. Every source the
  // slug came from has to follow, or the section goes on requesting the old
  // one: a picked slug outranked the localStorage key General had already
  // updated, so the page fell over with "Failed to load project" and any later
  // Save or Delete targeted a project that no longer existed (WS-8). The
  // address is rewritten in place; the draft is saved, so the leave guard has
  // nothing to ask about.
  const followRename = (renamed: string) => {
    setPickedSlug(renamed)
    if (searchParams.get('project') !== null) {
      const next = new URLSearchParams(searchParams)
      next.set('project', renamed)
      setSearchParams(next, { replace: true, state: LEAVE_CONFIRMED })
    }
  }

  // Persist the last visited section so re-entering /settings lands where the
  // user left off (the /settings index redirect reads this key).
  useEffect(() => {
    try {
      localStorage.setItem(SETTINGS_STORAGE_KEY, section)
    } catch {
      /* ignore */
    }
  }, [section])

  // The project's front door, Overview, where the Get-started checklist lives:
  // a newcomer sent to Data sources by step 2 came back to an empty Events
  // table instead (#250 JR-1 / JR-3).
  const backHref = slug ? projectHomePath(slug) : workspacePath()

  return (
    <SettingsLayout
      activePath={railPathFor(section)}
      backHref={backHref}
      projectName={projectName}
      projectSlug={slug}
      projects={projects}
    >
      {/* The projects list is silent app-wide (lib/queryKeys.ts): inside the
          app shell Layout reports its failure. These routes mount outside
          Layout, so they report it here — once, and not where the no-project
          card below already says it. */}
      {/* A Get-started step (Connect a data source) lands here; the bar is
          its way back to the checklist, and renders nothing otherwise
          (#250 JR-3). */}
      <OnboardingReturnBar className="mb-4" />
      {projectsQuery.isError && !(slug === undefined && isProjectScopedSection(section)) && (
        <ErrorState
          compact
          className="mb-4"
          title="Projects could not be loaded"
          description="Project names and pickers on this page may be missing."
          error={projectsQuery.error}
          onRetry={() => {
            void projectsQuery.refetch()
          }}
        />
      )}
      <Suspense fallback={<SectionFallback section={railPathFor(section)} />}>
        {renderSection({
          section,
          slug,
          isOwner,
          platformAdmin,
          projects,
          projectsStatus: projectsQuery.status,
          onPickProject: pickProject,
          onSlugChanged: followRename,
          projectsError: projectsQuery.error,
          onRetryProjects: () => {
            void projectsQuery.refetch()
          },
        })}
      </Suspense>
    </SettingsLayout>
  )
}

/** Sections that render against one project and so need a slug bound. */
function isProjectScopedSection(section: string): boolean {
  return (
    !ACCOUNT_SECTIONS.has(section) &&
    !section.startsWith('instance/') &&
    !section.startsWith('platform/')
  )
}

const ACCOUNT_SECTIONS: ReadonlySet<string> = new Set([
  'organization/general',
  'organization/groups',
  'members',
  'invitations',
  'data-sources',
  'api-keys',
  'profile',
  'security',
  ...Object.values(ORG_SECTION_PATHS),
  ORG_TRACKERS_PATH,
])

function renderSection({
  section,
  slug,
  isOwner,
  platformAdmin,
  projects,
  projectsStatus,
  onPickProject,
  onSlugChanged,
  projectsError,
  onRetryProjects,
}: {
  section: string
  slug: string | undefined
  isOwner: boolean
  platformAdmin: boolean
  projects: Project[]
  projectsStatus: 'pending' | 'error' | 'success'
  onPickProject: (slug: string) => void
  onSlugChanged: (slug: string) => void
  projectsError: unknown
  onRetryProjects: () => void
}) {
  if (section === 'organization/general') return <OrganizationGeneralSection />
  if (section === 'organization/groups') return <OrgGroupsSection />
  if (section === 'members') return <MembersSection />
  if (section === 'invitations') return <InvitationsSection />
  if (section === 'data-sources') return <DataSourcesSection />
  if (section === 'api-keys') return <ApiKeysSection />
  if (section === 'profile') return <ProfileSection />
  if (section === 'security') return <SecuritySection />
  const orgSection = orgSectionForPath(section)
  if (orgSection) {
    // The organization's own mail, AI and limits: its owners and admins. The
    // platform flag grants nothing inside an organization.
    return isOwner ? <OrgSettingsSection section={orgSection} /> : <OwnerOnly section={section} />
  }
  if (section === ORG_TRACKERS_PATH) {
    // The Jira/Linear defaults its projects inherit (F20 PR12): the same gate.
    return isOwner ? <OrgTrackersSection /> : <OwnerOnly section={section} />
  }
  // One route serves every organization section; an unknown one is not a
  // project section to guess at.
  if (section.startsWith('organization/')) return <Navigate to="/settings/organization/general" replace />
  if (section.startsWith('instance/')) {
    // Audit is the one Instance section that is not a settings form, so it does
    // not go through InstanceSection — that component's whole job is to frame a
    // ServiceSettingsPage section, and this reads a feed instead. It is the
    // organization's feed, so it takes the org owner gate (tripl-wkwv.17).
    if (section === 'instance/audit') {
      return isOwner ? <WorkspaceAuditSection /> : <OwnerOnly section={section} />
    }
    // Every other instance/* section is the Platform console (F20 PR9): the
    // operator's, whatever the caller's organization role.
    if (!platformAdmin) return <PlatformOnly section={section} />
    return <InstanceSection section={section.slice('instance/'.length)} />
  }
  if (section.startsWith('platform/')) {
    // The platform console (F20): organizations, users, suspension and
    // read-only step-in. The operator's, whatever the caller's organization role.
    if (!platformAdmin) return <PlatformOnly section={railPathFor(section)} />
    if (section === PLATFORM_ORGS_SECTION) return <PlatformOrgsSection />
    if (section === 'platform/users') return <PlatformUsersSection />
    const orgSlug = section.slice(`${PLATFORM_ORGS_SECTION}/`.length)
    if (section.startsWith(`${PLATFORM_ORGS_SECTION}/`) && orgSlug && !orgSlug.includes('/')) {
      return <PlatformOrgDetailSection key={orgSlug} slug={orgSlug} />
    }
    return <Navigate to={`/settings/${PLATFORM_ORGS_SECTION}`} replace />
  }
  // Everything below is project-scoped. Never guess which project that is.
  if (!slug) {
    return (
      <div>
        <StateHeader section={section} />
        <NoProjectSelected
          projects={projects}
          status={projectsStatus}
          onPick={onPickProject}
          error={projectsError}
          onRetry={onRetryProjects}
        />
      </div>
    )
  }
  if (section === 'project/plan-rules') return <PlanRulesSection slug={slug} />
  if (section === 'project/members') return <ProjectMembersSection slug={slug} />
  return <ProjectGeneralSection slug={slug} onSlugChanged={onSlugChanged} />
}

/**
 * The project-scoped routes are the only ones that can render with nothing
 * bound. They used to print "pick a project first" and offer no way to do it,
 * so the user had to leave, choose a project elsewhere and navigate back
 * (tripl-kr4u). The instruction now comes with the control it asks for.
 */
function NoProjectSelected({
  projects,
  status,
  onPick,
  error,
  onRetry,
}: {
  projects: Project[]
  status: 'pending' | 'error' | 'success'
  onPick: (slug: string) => void
  error: unknown
  onRetry: () => void
}) {
  // "There is no project on this workspace yet" is a claim about the server's
  // answer, so it may not be made before the answer arrives. Nothing warms the
  // ['projects'] cache on a /settings/* route — these routes mount outside
  // Layout — so on a cold load every owner of five projects was told they had
  // none for the length of the GET, and offered "Create one in the workspace".
  if (status === 'pending') return <SectionSkeleton variant="list" rows={3} label="Loading projects…" />

  // A retry in place, not "reload the page": a reload throws away whatever
  // else the user had open for a GET the query can simply repeat.
  if (status === 'error') {
    return (
      <ErrorState
        title="No project selected"
        description="The project list could not be loaded, so these settings have nothing to bind to."
        error={error}
        onRetry={onRetry}
      />
    )
  }

  if (projects.length === 0) {
    return (
      <SCard
        title="No project selected"
        description="Project settings change one specific project's tracking plan, and there is no project on this workspace yet."
      >
        <p className="m-0 px-4 py-[15px] text-body text-fg-tertiary">
          <Link to={workspacePath()} className="underline">
            Create one in the workspace
          </Link>{' '}
          and these settings open with it.
        </p>
      </SCard>
    )
  }

  return (
    <SCard
      title="Pick a project"
      description="Project settings change one specific project's tracking plan, so they need one in context."
    >
      {projects.map((project, index) => (
        <button
          key={project.slug}
          type="button"
          onClick={() => onPick(project.slug)}
          className="flex w-full items-baseline gap-3 px-4 py-[13px] text-left transition-colors hover:bg-[var(--surface-hover)]"
          style={{
            borderBottom:
              index === projects.length - 1 ? 'none' : '1px solid var(--border-subtle)',
          }}
        >
          <span className="min-w-0 flex-1 truncate text-body font-medium">{project.name}</span>
          <span className="mono shrink-0 text-caption text-fg-tertiary">
            {project.slug}
          </span>
        </button>
      ))}
    </SCard>
  )
}

/**
 * A Platform section opened by anyone but a platform admin: the rail hides the
 * group from them, so the page names itself and says whose it is.
 */
function PlatformOnly({ section }: { section: string }) {
  return (
    <div>
      <StateHeader section={section} />
      <ReadOnlyNotice
        action={
          <Link to="/settings/profile" className="text-body-sm font-medium text-accent no-underline hover:underline">
            Go to Profile
          </Link>
        }
      >
        Platform admin is required to view or change platform settings: they configure the server
        itself and the defaults every organization inherits.
      </ReadOnlyNotice>
    </div>
  )
}

/**
 * An owner-only section opened by a non-owner (a shared link, a bookmark). The
 * rail hides the Instance group from them, so the page has to say where they
 * are itself: the section's title, the one read-only notice, and a way out
 * (#237 ST-17 / ST-36).
 */
function OwnerOnly({ section }: { section: string }) {
  return (
    <div>
      <StateHeader section={section} />
      <ReadOnlyNotice
        action={
          <Link to="/settings/profile" className="text-body-sm font-medium text-accent no-underline hover:underline">
            Go to Profile
          </Link>
        }
      >
        Owner role is required to view or change organization-level settings. Ask an owner, or go
        to Profile.
      </ReadOnlyNotice>
    </div>
  )
}
