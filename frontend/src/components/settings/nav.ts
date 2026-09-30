import {
  Activity,
  Building2,
  Archive,
  Building,
  Cpu,
  Database,
  Key,
  KeyRound,
  Ticket,
  Lock,
  Mail,
  RefreshCw,
  ScrollText,
  Search,
  Server,
  Shield,
  SlidersHorizontal,
  Sparkles,
  User,
  UserCog,
  UserPlus,
  Users,
  Webhook,
  type LucideIcon,
} from 'lucide-react'
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
   * (#238 JR-19). Only words the label and the other keywords do not already
   * contain: the palettes match substrings of all of them, and this list
   * ships in the entry chunk.
   */
  keywords?: readonly string[]
  /**
   * A short tag after the label for a section that is not built yet ("Soon").
   * Rail only; the section keeps its plain label everywhere else (#238 ST-5,
   * #243 PL-26).
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
        keywords: ['warehouse', 'connection', 'clickhouse', 'postgres', 'bigquery', 'credentials'],
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
        id: 'org-sso',
        label: 'Single sign-on',
        icon: KeyRound,
        path: 'organization/sso',
        // Listed for owners and admins alike; the page itself is an owner's
        // (F20): only an owner changes how the organization signs in.
        ownerOnly: true,
        keywords: ['sso', 'oidc', 'openid', 'identity provider', 'domain verification'],
      },
      {
        id: 'org-scim',
        label: 'Provisioning',
        icon: RefreshCw,
        path: 'organization/scim',
        // Like single sign-on: listed for owners and admins, the page itself
        // is an owner's (F20). It decides who is in the organization.
        ownerOnly: true,
        keywords: ['scim', 'okta', 'azure', 'entra', 'deprovision', 'user sync'],
      },
      {
        id: 'org-limits',
        label: 'Limits',
        icon: SlidersHorizontal,
        path: 'organization/limits',
        ownerOnly: true,
      },
      // Not a settings form: the organization's audit feed. Its path predates
      // the Platform group (tripl-wkwv.17); the actions it exists for — data
      // sources, member roles, API keys, and a project's own DELETION — belong
      // to the organization, not to any one project.
      {
        id: 'inst-audit',
        label: 'Audit log',
        icon: ScrollText,
        path: 'instance/audit',
        wide: true,
        ownerOnly: true,
        keywords: ['activity', 'who changed', 'log', 'export', 'csv', 'download'],
      },
      // The audit log leaving tripl (F20): every entry POSTed, signed, to the
      // organization's SIEM. Listed for owners and admins alike; the page
      // itself is an owner's, like Single sign-on.
      {
        id: 'org-audit-webhook',
        label: 'Audit webhook',
        icon: Webhook,
        path: 'organization/audit-webhook',
        ownerOnly: true,
        keywords: ['siem', 'stream', 'signature', 'hmac'],
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
      // The console (F20): every organization and account on the instance.
      // "Organizations" and "User accounts", not "Organization" / "Members": those
      // are the organization's own pages, and two items with one label read
      // as one page in the rail and in both palettes.
      {
        id: 'platform-orgs',
        label: 'Organizations',
        icon: Building,
        path: 'platform/orgs',
        wide: true,
        platformOnly: true,
        keywords: ['tenants', 'suspend', 'step in', 'support'],
      },
      {
        id: 'platform-users',
        label: 'User accounts',
        icon: UserCog,
        path: 'platform/users',
        wide: true,
        platformOnly: true,
        keywords: ['accounts', 'platform admin', 'operators'],
      },
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
 * link promising "project" that lands there was the LIVE-34 mismatch (ST-4).
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
