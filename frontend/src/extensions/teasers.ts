import { KeyRound, RefreshCw, Webhook } from 'lucide-react'
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

/**
 * Every feature that lives in the Enterprise edition. Shown only where no
 * installed extension provides it (`visibleTeasers`): while a feature is still
 * bundled with Community its real page wins and the teaser stays hidden.
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
]

/** The teasers no installed extension provides a section for. */
export function visibleTeasers(
  teasers: readonly EnterpriseTeaser[],
  sections: readonly Pick<ExtensionSettingsSection, 'item'>[],
): EnterpriseTeaser[] {
  const provided = new Set(sections.map((section) => section.item.id))
  return teasers.filter((teaser) => !provided.has(teaser.item.id))
}
