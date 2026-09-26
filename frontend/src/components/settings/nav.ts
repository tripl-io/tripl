import {
  Activity,
  Archive,
  Cpu,
  Database,
  Key,
  Lock,
  Mail,
  ScrollText,
  Server,
  Shield,
  SlidersHorizontal,
  Sparkles,
  User,
  UserCog,
  Users,
  type LucideIcon,
} from 'lucide-react'

/**
 * Navigation model for the full-takeover Settings area. Two top-level contexts
 * (Project / Workspace). Everything functional lives in the app sidebar now;
 * this holds only genuine configuration. Recreated from the design mockup
 * (design/tripl/project/settings-kit.jsx — SETTINGS_NAV).
 */

export type SettingsContext = 'project' | 'workspace'

export type SettingsNavItem = {
  id: string
  label: string
  icon: LucideIcon
  /** Route segment under /settings (e.g. 'project/general'). */
  path: string
  /** Owner-only sections are hidden for non-owners. */
  ownerOnly?: boolean
  /**
   * What people type when they look for this section but do not know its name
   * ("timezone" finds General). Palettes match on these as well as the label
   * (#238 JR-19).
   */
  keywords?: readonly string[]
  /**
   * A short tag after the label for a section that is not built yet ("Soon").
   * Rail only; the section keeps its plain label everywhere else (#238 ST-5,
   * #243 PL-26).
   */
  tag?: string
}

export type SettingsNavGroup = {
  label: string
  sub: string
  /** One-line descriptor framing the group's scope (rendered under the label). */
  desc: string
  items: SettingsNavItem[]
}

export const PROJECT_GROUPS: SettingsNavGroup[] = [
  {
    label: 'Project',
    sub: 'Project',
    desc: "Configuration for this project's tracking plan",
    items: [
      {
        id: 'general',
        label: 'General',
        icon: SlidersHorizontal,
        path: 'project/general',
        keywords: ['project name', 'timezone', 'slug', 'rename', 'description', 'app version', 'delete project'],
      },
      // "Access", not "Members": the Workspace group has its own Members (the
      // instance roster), and two items with one label read as the same page
      // in the rail and in both palettes.
      {
        id: 'project-members',
        label: 'Access',
        icon: UserCog,
        path: 'project/members',
        keywords: ['members', 'people', 'team', 'roles', 'add member', 'permissions', 'who can see'],
      },
      {
        id: 'plan-rules',
        label: 'Plan rules',
        icon: Shield,
        path: 'project/plan-rules',
        // Last in the group, tagged: the page only says what is coming and
        // where approvals live today (ST-5 / PL-26).
        tag: 'Soon',
        keywords: ['naming rules', 'conventions', 'policy'],
      },
    ],
  },
]

export const WORKSPACE_GROUPS: SettingsNavGroup[] = [
  {
    label: 'Workspace',
    sub: 'Workspace',
    desc: 'Shared across everyone in the workspace',
    items: [
      {
        id: 'members',
        label: 'Members',
        icon: Users,
        path: 'members',
        keywords: ['users', 'people', 'roles', 'invite', 'team'],
      },
      {
        id: 'sources',
        label: 'Data sources',
        icon: Database,
        path: 'data-sources',
        keywords: ['warehouse', 'connection', 'clickhouse', 'postgres', 'bigquery', 'credentials'],
      },
      {
        id: 'apikeys',
        label: 'API keys',
        icon: Key,
        path: 'api-keys',
        keywords: ['token', 'api key', 'integration'],
      },
    ],
  },
  {
    label: 'Account',
    sub: 'You',
    desc: 'Settings just for you',
    items: [
      {
        id: 'profile',
        label: 'Profile',
        icon: User,
        path: 'profile',
        keywords: ['name', 'email', 'account', 'appearance', 'theme', 'dark mode'],
      },
      // "Password & sessions", not "Security": the Instance group has its own
      // "Security & access", and two items called Security one group apart
      // read as the same page (#238 JR-26).
      {
        id: 'security',
        label: 'Password & sessions',
        icon: Lock,
        path: 'security',
        keywords: ['security', 'password', 'sign out', 'sessions'],
      },
    ],
  },
  {
    label: 'Instance',
    sub: 'Owner only',
    // "(owner only)" is the sub-label's job; saying it twice read as chrome (ST-7).
    desc: 'Server-wide settings',
    items: [
      {
        id: 'runtime',
        label: 'Runtime',
        icon: Cpu,
        path: 'instance/runtime',
        ownerOnly: true,
        keywords: ['workers', 'scheduler', 'retention'],
      },
      {
        id: 'email',
        label: 'Email',
        icon: Mail,
        path: 'instance/email',
        ownerOnly: true,
        keywords: ['smtp', 'mail'],
      },
      {
        id: 'ai',
        label: 'AI',
        icon: Sparkles,
        path: 'instance/ai',
        ownerOnly: true,
        keywords: ['llm', 'model', 'explanations'],
      },
      {
        id: 'inst-security',
        label: 'Security & access',
        icon: Shield,
        path: 'instance/security',
        ownerOnly: true,
        keywords: ['registration', 'sign up', 'sso', 'access'],
      },
      { id: 'storage', label: 'Storage', icon: Archive, path: 'instance/storage', ownerOnly: true },
      {
        id: 'observability',
        label: 'Observability',
        icon: Activity,
        path: 'instance/observability',
        ownerOnly: true,
      },
      { id: 'system', label: 'System', icon: Server, path: 'instance/system', ownerOnly: true },
      // The only Instance section that is not a settings form: it reads the
      // whole audit feed rather than editing configuration. It lives here
      // because the actions it exists for — data sources, member roles, API
      // keys, and a project's own DELETION, which is recorded once its subject
      // is gone — belong to no project, so the per-project tab can never show
      // them (tripl-wkwv.17).
      {
        id: 'inst-audit',
        label: 'Audit log',
        icon: ScrollText,
        path: 'instance/audit',
        ownerOnly: true,
        keywords: ['activity', 'who changed', 'log'],
      },
    ],
  },
]

export const SETTINGS_NAV: Record<SettingsContext, SettingsNavGroup[]> = {
  project: PROJECT_GROUPS,
  workspace: WORKSPACE_GROUPS,
}

export const SETTINGS_STORAGE_KEY = 'tripl.settings'

/** First section path for a context (used when switching context). */
export function firstSectionPath(ctx: SettingsContext): string {
  return SETTINGS_NAV[ctx][0]?.items[0]?.path ?? ''
}

/**
 * The settings section a URL points at, or `null` when it points outside the
 * takeover altogether.
 *
 * `null` is the answer the unsaved-work predicate treats as "leaving the area",
 * which no draft survives. A bare `/settings` counts as leaving too: it is not a
 * section, and nothing renders a draft there.
 *
 * Exists so the navigation blocker can ask about a DESTINATION the same question
 * the rail asks about a link — one parser, so a Back press and a click cannot
 * disagree about where they are going (tripl-l33u.14).
 */
export function sectionPathForUrl(pathname: string): string | null {
  const prefix = '/settings/'
  if (!pathname.startsWith(prefix)) return null
  return pathname.slice(prefix.length).replace(/\/+$/, '') || null
}

/**
 * The rail label of a section path ('project/general' -> 'General'), or
 * `undefined` for a path the rail does not list. Lets the area name the page
 * before its lazy chunk arrives (#237 ST-35) and above the owner-only and
 * pick-a-project states (ST-36).
 */
export function sectionLabel(path: string): string | undefined {
  for (const groups of Object.values(SETTINGS_NAV)) {
    for (const group of groups) {
      const item = group.items.find((candidate) => candidate.path === path)
      if (item) return item.label
    }
  }
  return undefined
}

/**
 * The words on the way out of the takeover. The label names where the link
 * really goes: with no project bound `backHref` is the workspace list, and a
 * link promising "project" that lands there was the LIVE-34 mismatch (ST-4).
 */
export function backToLabel(backHref: string, projectName?: string): string {
  if (backHref === '/workspace') return 'Back to workspace'
  return projectName ? `Back to ${projectName}` : 'Back to project'
}

/** Resolve which context owns a given section path. Defaults to 'workspace'. */
export function contextForPath(path: string): SettingsContext {
  return path.startsWith('project/') ? 'project' : 'workspace'
}

/** Group the visible workspace groups for a role (drops owner-only Instance). */
export function visibleGroups(ctx: SettingsContext, isOwner: boolean): SettingsNavGroup[] {
  return SETTINGS_NAV[ctx]
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => !item.ownerOnly || isOwner),
    }))
    .filter((group) => group.items.length > 0)
}

/**
 * Every settings group (project + workspace) in one flat, owner-filtered list.
 * The settings nav no longer splits project vs workspace behind a segmented
 * toggle — all config lives under a single scrollable rail.
 */
export function visibleGroupsAll(isOwner: boolean): SettingsNavGroup[] {
  return [...visibleGroups('project', isOwner), ...visibleGroups('workspace', isOwner)]
}
