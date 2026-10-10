import { Activity, Building, FileSearch, History, KeyRound, RefreshCw, ScrollText, ShieldCheck, Siren, UserCog, Webhook } from 'lucide-react'
import type { SettingsNavItem } from '@/components/settings/nav'
import { EDITIONS_DOCS_URL, INSTANCE_SIGN_IN_DOCS_URL } from '@/lib/docsSite'
import type { ExtensionSettingsSection } from './types'

/** Where the docs say what each edition has; defined with the other docs-site
 *  links in `lib/docsSite.ts`, and still importable from here. */
export { EDITIONS_DOCS_URL }

/**
 * A feature of the Enterprise edition, as Community shows it: a rail item
 * tagged "Enterprise" whose page says what the feature does and where it is.
 * Its id is the id of the section the Enterprise extension registers, so an
 * Enterprise build shows the real page instead.
 */
export interface EnterpriseTeaser {
  group: string
  after?: string
  before?: string
  item: SettingsNavItem
  /** One or two sentences: what the feature does for the organization. */
  summary: string
  /**
   * What Community already has of the same kind, and where the docs say how
   * to set it up: someone who searched for "OIDC" and landed on the
   * organization's single sign-on must not leave thinking the instance cannot
   * sign anyone in through their identity provider at all.
   */
  inCommunity?: { text: string; href: string }
}

const tagged = (item: Omit<SettingsNavItem, 'tag' | 'ownerOnly'>): SettingsNavItem => ({
  ...item,
  tag: 'Enterprise',
  // Organization administration: only its owners and admins see the item.
  ownerOnly: true,
})

const platformTagged = (item: Omit<SettingsNavItem, 'tag' | 'platformOnly'>): SettingsNavItem => ({
  ...item,
  tag: 'Enterprise',
  // The operator's: only platform admins see the item.
  platformOnly: true,
})

/**
 * Every feature that lives in the Enterprise edition. Shown only where no
 * installed extension provides it (`visibleTeasers`): in an Enterprise build
 * the real page wins and the teaser stays hidden.
 */
export const ENTERPRISE_TEASERS: readonly EnterpriseTeaser[] = [
  {
    group: 'Organization',
    after: 'org-general',
    item: tagged({
      id: 'org-health',
      label: 'Project health',
      icon: Activity,
      path: 'organization/health',
      keywords: ['dashboard', 'overview', 'incidents', 'failing scans', 'monitors', 'drift', 'coverage', 'trend'],
    }),
    summary:
      "Every project of the organization in one sortable table: failing scans, firing monitors, open incidents and property drifts, plan coverage, and each project's failed scan runs over the last 7 days, with the organization's totals.",
  },
  {
    group: 'Organization',
    after: 'org-health',
    item: tagged({
      id: 'org-project-search',
      label: 'Search projects',
      icon: FileSearch,
      path: 'organization/project-search',
      keywords: ['find', 'cross-project', 'all projects', 'events', 'metrics'],
    }),
    summary:
      'One search over every project of the organization: events, properties, metrics, scans, alert rules and docs notes, best matches first, each labelled with its project. Only projects the person searching can open are searched.',
  },
  {
    group: 'Organization',
    after: 'groups',
    item: tagged({
      id: 'org-access',
      label: 'Access control',
      icon: ShieldCheck,
      path: 'organization/access',
      keywords: ['rbac', 'roles', 'custom roles', 'permissions', 'group access', 'team sync', 'idp groups'],
    }),
    summary:
      "Give a group a role in a project, build custom roles that hold only some of an editor's permissions, and keep groups in step with your identity provider's groups at each single sign-on.",
  },
  {
    group: 'Organization',
    after: 'org-trackers',
    item: tagged({
      id: 'org-sso',
      label: 'Single sign-on',
      icon: KeyRound,
      path: 'organization/sso',
      keywords: ['sso', 'oidc', 'saml', 'openid', 'identity provider', 'domain verification'],
    }),
    summary:
      'Each organization signs its members in through its own identity provider (OpenID Connect or SAML 2.0), with verified email domains and single sign-on required for everyone but owners.',
    inCommunity: {
      text: 'Signing everyone on this instance in through one OpenID Connect provider (Okta, Entra ID, Keycloak and others) or Google is in Community, set with environment variables.',
      href: INSTANCE_SIGN_IN_DOCS_URL,
    },
  },
  {
    group: 'Organization',
    after: 'org-sso',
    item: tagged({
      id: 'org-scim',
      label: 'Provisioning',
      icon: RefreshCw,
      path: 'organization/scim',
      keywords: ['scim', 'okta', 'azure', 'entra', 'deprovision', 'user sync'],
    }),
    summary:
      'Your identity provider adds and removes members over SCIM 2.0, and maps its groups to organization roles.',
  },
  {
    group: 'Organization',
    after: 'org-scim',
    item: tagged({
      id: 'org-escalation',
      label: 'Escalation',
      icon: Siren,
      path: 'organization/escalation',
      keywords: ['on-call', 'escalation policy', 'paging', 'routing', 'unacknowledged', 'pagerduty'],
    }),
    summary:
      'When an alert is not acknowledged in time, notify the next destination, member or group, with routes that pick a policy across all projects.',
  },
  {
    group: 'Organization',
    after: 'org-escalation',
    item: tagged({
      id: 'org-governance',
      label: 'Plan governance',
      icon: ShieldCheck,
      path: 'organization/governance',
      keywords: ['plan rules', 'naming rules', 'policy', 'protected main', 'pii', 'sensitive fields', 'forbidden properties'],
    }),
    summary:
      'Rules every project of the organization follows: naming patterns for events and properties, required and forbidden properties, sensitive fields that need a designated group’s approval, and a protected main that takes changes only through branches. tripl check, plan validation and every merge enforce them.',
  },
  {
    group: 'Organization',
    after: 'org-limits',
    item: tagged({
      id: 'inst-audit',
      label: 'Audit log',
      icon: ScrollText,
      path: 'instance/audit',
      keywords: ['activity', 'who changed', 'log', 'export', 'csv', 'download'],
    }),
    summary:
      "The organization's whole audit log in one place: every project's changes and the actions outside projects (data sources, members and roles, API keys, deleted projects), searchable and exportable as CSV or NDJSON. Each project's own audit log stays in Community, under Govern › Audit log in that project.",
  },

  {
    group: 'Organization',
    after: 'inst-audit',
    item: tagged({
      id: 'org-audit-webhook',
      label: 'Audit webhook',
      icon: Webhook,
      path: 'organization/audit-webhook',
      keywords: ['siem', 'stream', 'signature', 'hmac'],
    }),
    summary:
      "Every entry of the organization's audit log is sent, signed, to your SIEM as it is written.",
  },
  {
    group: 'Organization',
    after: 'org-audit-webhook',
    item: tagged({
      id: 'org-audit-retention',
      label: 'Audit retention',
      icon: History,
      path: 'organization/audit-retention',
      keywords: ['legal hold', 'compliance', 'delete old entries', 'purge', 'gdpr'],
    }),
    summary:
      "How long the organization's audit log is kept, with a legal hold that keeps every entry while a dispute or investigation needs it.",
  },
  {
    group: 'Platform',
    before: 'runtime',
    item: platformTagged({
      id: 'platform-orgs',
      label: 'Organizations',
      icon: Building,
      path: 'platform/orgs',
      keywords: ['tenants', 'suspend', 'step in', 'support'],
    }),
    summary:
      'Every organization on the instance in one list: its members and projects, suspending and restoring it, and a time-limited, audited, read-only step-in to help its people.',
  },
  {
    group: 'Platform',
    before: 'runtime',
    item: platformTagged({
      id: 'platform-users',
      label: 'User accounts',
      icon: UserCog,
      path: 'platform/users',
      keywords: ['accounts', 'platform admin', 'operators'],
    }),
    summary:
      'Every account on the instance, with the organizations it belongs to, and granting or revoking platform admin. Without it, platform admins are managed with the tripl-admin command.',
  },
]

/**
 * Creating more organizations: not a page of its own but the card under
 * Organization › Details, shown to a platform admin where Community runs its
 * one organization.
 */
export const ORG_CREATION_TEASER: EnterpriseTeaser = {
  group: 'Organization',
  item: platformTagged({
    id: 'org-create',
    label: 'Creating more organizations',
    icon: Building,
    path: 'organization/general',
  }),
  summary:
    'Separate organizations on one instance, each with its own members, projects, data sources, API keys and settings: for departments, clients or teams that must not see each other’s work. Community runs one organization.',
}

/** Where an Enterprise section sits in the rail, and its rail item. */
export type EnterpriseTeaserSection = Pick<ExtensionSettingsSection, 'group' | 'after' | 'before' | 'item'>

/**
 * The Enterprise feature `id` as its real section registers: the teaser's
 * group, anchor and item (label, icon, path, keywords, who sees it) without
 * the "Enterprise" tag. The Enterprise extension builds its sections from
 * this and adds only the page and what the page needs, so the two editions
 * cannot name, place or find one feature differently. Throws on an id no
 * teaser has: that is a typo, not a feature to show nowhere.
 */
export function enterpriseTeaserSection(id: string): EnterpriseTeaserSection {
  const teaser = ENTERPRISE_TEASERS.find((entry) => entry.item.id === id)
  if (!teaser) throw new Error(`No Enterprise teaser has the id "${id}".`)
  const { tag: _tag, ...item } = teaser.item
  return {
    group: teaser.group,
    ...(teaser.after === undefined ? {} : { after: teaser.after }),
    ...(teaser.before === undefined ? {} : { before: teaser.before }),
    item,
  }
}

/** The teasers no installed extension provides a section for. */
export function visibleTeasers(
  teasers: readonly EnterpriseTeaser[],
  sections: readonly Pick<ExtensionSettingsSection, 'item'>[],
): EnterpriseTeaser[] {
  const provided = new Set(sections.map((section) => section.item.id))
  return teasers.filter((teaser) => !provided.has(teaser.item.id))
}
