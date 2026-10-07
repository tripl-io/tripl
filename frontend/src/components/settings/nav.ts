import {
  Activity,
  Building2,
  Archive,
  Cpu,
  Database,
  Key,
  Ticket,
  Lock,
  Mail,
  Search,
  Server,
  Shield,
  SlidersHorizontal,
  Sparkles,
  User,
  UserCog,
  UserPlus,
  Users,
  type LucideIcon,
} from 'lucide-react'
import {
  enterpriseTeasers,
  extensionSettingsSections,
  type ExtensionSettingsSection,
} from '@/extensions'
import { stripOrgPrefix } from '@/lib/activeOrg'

/**
 * Navigation model for the full-takeover Settings area. Two top-level contexts
 * (Project / Organization). Everything functional lives in the app sidebar now;
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
  /**
   * Owner-only sections are hidden for everyone but an owner or admin of the
   * organization (`isOwner`).
   */
  ownerOnly?: boolean
  /**
   * An organization-scoped INSTANCE settings section (runtime, email, AI,
   * storage): `/settings` admits a platform admin as well as an org owner or
   * admin (backend `get_settings_admin_user`), so it shows for either. Only
   * meaningful next to `ownerOnly`.
   */
  settingsAdmin?: boolean
  /**
   * An operator-only section (security, observability, system): shown only to
   * a platform admin (backend `require_platform_admin`), whatever their
   * organization role. Wins over `ownerOnly`.
   */
  platformOnly?: boolean
  /**
   * What people type when they look for this section but do not know its name
   * ("timezone" finds General). Palettes match on these as well as the label
   * (#238). Only words the label and the other keywords do not already
   * contain: the palettes match substrings of all of them, and this list
   * ships in the entry chunk.
   */
  keywords?: readonly string[]
  /**
   * A short tag after the label for a section that is not built yet ("Soon").
   * Rail only; the section keeps its plain label everywhere else (#238,
   * #243).
   */
  tag?: string
  /**
   * A section built around a wide table (members, keys, the audit log, the
   * platform console): its content column widens to the room there is instead
   * of the narrow form width every other section shares, so columns are not
   * cut off behind a horizontal scroll on a wide screen.
   */
  wide?: boolean
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
        wide: true,
        keywords: ['members', 'people', 'team', 'roles', 'add member', 'permissions', 'who can see'],
      },
      {
        id: 'plan-rules',
        label: 'Plan rules',
        icon: Shield,
        path: 'project/plan-rules',
        // Last in the group: the page lists the gates a plan change passes in
        // this project, and where the organization's own rules are set.
        keywords: ['naming rules', 'conventions', 'policy', 'merge policy', 'governance'],
      },
    ],
  },
]

const CORE_WORKSPACE_GROUPS: SettingsNavGroup[] = [
  {
    // "Organization", not "Workspace" (F20 PR7): everything here belongs to
    // the organization the app acts in, and differs from one to the next.
    label: 'Organization',
    sub: 'Organization',
    desc: 'Shared across everyone in the organization',
    items: [
      // The organization's General page, labelled "Details" in the rail: the
      // Project group already has a "General", and two items with one label
      // read as the same page in the rail and in both palettes.
      {
        id: 'org-general',
        label: 'Details',
        icon: Building2,
        path: 'organization/general',
        keywords: ['general', 'organization', 'name', 'rename', 'slug', 'delete organization', 'create organization'],
      },
      {
        id: 'members',
        label: 'Members',
        icon: Users,
        path: 'members',
        wide: true,
        keywords: ['users', 'people', 'roles', 'team', 'remove member', 'transfer ownership'],
      },
      // Named sets of members (F20): note sharing and owner routing will name
      // them. Everyone reads them; owners and admins manage them.
      {
        id: 'groups',
        label: 'Groups',
        icon: Users,
        path: 'organization/groups',
        wide: true,
      },
      {
        id: 'invitations',
        label: 'Invitations',
        icon: UserPlus,
        path: 'invitations',
        wide: true,
        ownerOnly: true,
        keywords: ['invite', 'add member', 'revoke'],
      },
      {
        id: 'sources',
        label: 'Data sources',
        icon: Database,
        path: 'data-sources',
        wide: true,
        keywords: ['warehouse', 'connection', 'clickhouse', 'postgres', 'bigquery', 'databricks', 'snowflake', 'credentials'],
      },
      {
        id: 'apikeys',
        label: 'API keys',
        icon: Key,
        path: 'api-keys',
        wide: true,
        keywords: ['token', 'api key', 'integration'],
      },
      // The organization's own settings (F20 PR9-PR12): its mail relay, AI
      // provider, search embeddings, photo storage and row caps, each inheriting the
      // platform's value until it sets one; and the tracker defaults its
      // projects fall back to.
      {
        id: 'org-email',
        label: 'Email',
        icon: Mail,
        path: 'organization/email',
        ownerOnly: true,
      },
      {
        id: 'org-ai',
        label: 'AI',
        icon: Sparkles,
        path: 'organization/ai',
        ownerOnly: true,
      },
      {
        id: 'org-search',
        label: 'Search',
        icon: Search,
        path: 'organization/search',
        ownerOnly: true,
      },
      {
        // "Photos", not "Storage": the Platform group's "Storage" is the
        // operator's store, and two items with one label read as one page.
        id: 'org-storage',
        label: 'Photos',
        icon: Archive,
        path: 'organization/storage',
        ownerOnly: true,
      },
      {
        id: 'org-trackers',
        label: 'Trackers',
        icon: Ticket,
        path: 'organization/trackers',
        ownerOnly: true,
      },
      {
        id: 'org-limits',
        label: 'Limits',
        icon: SlidersHorizontal,
        path: 'organization/limits',
        ownerOnly: true,
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
      // read as the same page (#238).
      {
        id: 'security',
        label: 'Password & sessions',
        icon: Lock,
        path: 'security',
        keywords: ['security', 'password', 'sign out'],
      },
    ],
  },
  {
    // The operator console (F20 PR9): tripl's own infrastructure, and the mail
    // relay and AI provider every organization without its own inherits.
    // Platform admins only, whatever their organization role.
    label: 'Platform',
    sub: 'Platform admin',
    desc: 'Server-wide settings',
    items: [
      // The console (every organization and account on the instance) is the
      // Enterprise edition's: its items come before Runtime (extensions/teasers.ts).
      {
        id: 'runtime',
        label: 'Runtime',
        icon: Cpu,
        path: 'instance/runtime',
        platformOnly: true,
      },
      {
        // Named apart from Organization › Email: this one carries account mail
        // (sign-up, password reset, invitations) and is every organization's
        // default relay.
        id: 'email',
        label: 'Mail relay',
        icon: Mail,
        path: 'instance/email',
        platformOnly: true,
        keywords: ['smtp', 'email'],
      },
      {
        id: 'ai',
        label: 'AI & search',
        icon: Sparkles,
        path: 'instance/ai',
        platformOnly: true,
        keywords: ['llm', 'embeddings'],
      },
      {
        id: 'inst-security',
        label: 'Security & access',
        icon: Shield,
        path: 'instance/security',
        platformOnly: true,
        keywords: ['registration', 'sign up', 'access'],
      },
      {
        id: 'storage',
        label: 'Storage',
        icon: Archive,
        path: 'instance/storage',
        platformOnly: true,
      },
      {
        id: 'observability',
        label: 'Observability',
        icon: Activity,
        path: 'instance/observability',
        platformOnly: true,
      },
      {
        id: 'system',
        label: 'System',
        icon: Server,
        path: 'instance/system',
        platformOnly: true,
      },
    ],
  },
]

/**
 * `groups` with the extensions' settings items placed: each before or after the
 * item it names (in its group), or at the end of its group. New arrays; `groups` is
 * left as it is.
 */
export function withExtensionItems(
  groups: readonly SettingsNavGroup[],
  sections: readonly Pick<ExtensionSettingsSection, 'group' | 'after' | 'before' | 'item'>[],
): SettingsNavGroup[] {
  return groups.map((group) => {
    let items = [...group.items]
    for (const section of sections) {
      if (section.group !== group.label) continue
      const before = section.before ? items.findIndex((item) => item.id === section.before) : -1
      if (before >= 0) {
        items = [...items.slice(0, before), section.item, ...items.slice(before)]
        continue
      }
      const at = section.after ? items.findIndex((item) => item.id === section.after) : -1
      items = at < 0 ? [...items, section.item] : [...items.slice(0, at + 1), section.item, ...items.slice(at + 1)]
    }
    return { ...group, items }
  })
}

// Extension sections, then the Enterprise features this build does not have
// (tagged "Enterprise", where the real item would be).
export const WORKSPACE_GROUPS: SettingsNavGroup[] = withExtensionItems(CORE_WORKSPACE_GROUPS, [
  ...extensionSettingsSections,
  ...enterpriseTeasers,
])

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
 * disagree about where they are going.
 */
export function sectionPathForUrl(pathname: string): string | null {
  const prefix = '/settings/'
  if (!pathname.startsWith(prefix)) return null
  return pathname.slice(prefix.length).replace(/\/+$/, '') || null
}

/**
 * The rail label of a section path ('project/general' -> 'General'), or
 * `undefined` for a path the rail does not list. Lets the area name the page
 * before its lazy chunk arrives (#237) and above the owner-only and
 * pick-a-project states.
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

/** Whether the section at `path` takes the wide content column (`wide`). */
export function sectionIsWide(path: string): boolean {
  for (const groups of Object.values(SETTINGS_NAV)) {
    for (const group of groups) {
      const item = group.items.find((candidate) => candidate.path === path)
      if (item) return item.wide === true
    }
  }
  return false
}

/**
 * The words on the way out of the takeover. The label names where the link
 * really goes: with no project bound `backHref` is the workspace list, and a
 * link promising "project" that lands there was a mismatch.
 */
export function backToLabel(backHref: string, projectName?: string): string {
  if (stripOrgPrefix(backHref) === '/workspace') return 'Back to workspace'
  return projectName ? `Back to ${projectName}` : 'Back to project'
}

/** Resolve which context owns a given section path. Defaults to 'workspace'. */
export function contextForPath(path: string): SettingsContext {
  return path.startsWith('project/') ? 'project' : 'workspace'
}

/**
 * Whether a section is shown to this caller (F20 PR4).
 *
 * `isOwner` is an owner or admin of the organization; `isPlatformAdmin` the
 * operator flag. A platform-only section needs the flag; an org-scoped instance
 * section takes either; any other owner-only section takes the org role.
 */
export function itemVisible(
  item: Pick<SettingsNavItem, 'ownerOnly' | 'settingsAdmin' | 'platformOnly'>,
  isOwner: boolean,
  isPlatformAdmin = false,
): boolean {
  if (item.platformOnly) return isPlatformAdmin
  if (!item.ownerOnly) return true
  return isOwner || (item.settingsAdmin === true && isPlatformAdmin)
}

/** Group the visible workspace groups for a role (drops the Instance sections it cannot use). */
export function visibleGroups(
  ctx: SettingsContext,
  isOwner: boolean,
  isPlatformAdmin = false,
): SettingsNavGroup[] {
  return SETTINGS_NAV[ctx]
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => itemVisible(item, isOwner, isPlatformAdmin)),
    }))
    .filter((group) => group.items.length > 0)
}

/**
 * Every settings group (project + workspace) in one flat, owner-filtered list.
 * The settings nav no longer splits project vs workspace behind a segmented
 * toggle — all config lives under a single scrollable rail.
 */
export function visibleGroupsAll(isOwner: boolean, isPlatformAdmin = false): SettingsNavGroup[] {
  return [
    ...visibleGroups('project', isOwner, isPlatformAdmin),
    ...visibleGroups('workspace', isOwner, isPlatformAdmin),
  ]
}
