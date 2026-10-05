import { Building, KeyRound, RefreshCw, ScrollText, UserCog, Webhook } from 'lucide-react'
import type { SettingsNavItem } from '@/components/settings/nav'
import type { ExtensionSettingsSection } from './types'

/** Where the docs say what each edition has. */
export const EDITIONS_DOCS_URL = 'https://tripl-io.github.io/tripl/editions'

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
    after: 'org-trackers',
    item: tagged({
      id: 'org-sso',
      label: 'Single sign-on',
      icon: KeyRound,
      path: 'organization/sso',
      keywords: ['sso', 'oidc', 'saml', 'openid', 'identity provider', 'domain verification'],
    }),
    summary:
      "Members sign in through your organization's identity provider (OpenID Connect or SAML 2.0), with verified email domains and single sign-on required for everyone but owners.",
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
    after: 'org-limits',
    item: tagged({
      id: 'inst-audit',
      label: 'Audit log',
      icon: ScrollText,
      path: 'instance/audit',
      keywords: ['activity', 'who changed', 'log', 'export', 'csv', 'download'],
    }),
    summary:
      "The organization's whole audit log in one place: every project's changes and the actions outside projects (data sources, members and roles, API keys, deleted projects), searchable and exportable as CSV or NDJSON. Each project's own history stays in its settings.",
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

/** The teasers no installed extension provides a section for. */
export function visibleTeasers(
  teasers: readonly EnterpriseTeaser[],
  sections: readonly Pick<ExtensionSettingsSection, 'item'>[],
): EnterpriseTeaser[] {
  const provided = new Set(sections.map((section) => section.item.id))
  return teasers.filter((teaser) => !provided.has(teaser.item.id))
}
