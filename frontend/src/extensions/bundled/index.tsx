import { KeyRound, RefreshCw, Webhook } from 'lucide-react'
import { SsoRequiredState } from '@/components/states/sso-required'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { ORG_AUDIT_WEBHOOK_PATH } from '@/pages/settings-area/org-settings/auditExportModel'
import { ORG_SCIM_PATH } from '@/pages/settings-area/org-settings/orgScimModel'
import { ORG_SSO_PATH } from '@/pages/settings-area/org-settings/orgSsoModel'
import type { FrontendExtension } from '../types'
import { findSsoRequiredError, ssoStartFromError } from './ssoRefusal'

/**
 * The enterprise UI still in this repository: single sign-on, SCIM
 * provisioning and the audit webhook (F20), registered as a bundled extension
 * so the app reaches them only through the registry. Moving them to the
 * enterprise package moves this directory and changes nothing in the app.
 */
export const bundledEnterprise: FrontendExtension = {
  name: 'bundled-enterprise',
  routes: [
    {
      // Confirming that a single sign-on may attach to an existing account.
      // Public: the ticket comes from the identity provider's callback, before
      // any session exists.
      path: '/sso/link',
      key: 'sso-link',
      skeleton: 'form',
      Component: lazyWithReload(() => import('@/pages/SsoLinkPage')),
    },
  ],
  settingsSections: [
    {
      group: 'Organization',
      after: 'org-trackers',
      item: {
        id: 'org-sso',
        label: 'Single sign-on',
        icon: KeyRound,
        path: ORG_SSO_PATH,
        // Listed for owners and admins alike; the page itself is an owner's:
        // only an owner changes how the organization signs in.
        ownerOnly: true,
        keywords: ['sso', 'oidc', 'openid', 'identity provider', 'domain verification'],
      },
      access: 'orgOwner',
      deniedReason: 'single sign-on: it decides how everyone in the organization signs in',
      Component: lazyWithReload(() => import('@/pages/settings-area/OrgSsoSection')),
    },
    {
      group: 'Organization',
      after: 'org-sso',
      item: {
        id: 'org-scim',
        label: 'Provisioning',
        icon: RefreshCw,
        path: ORG_SCIM_PATH,
        // Like single sign-on: listed for owners and admins, the page itself is
        // an owner's. It decides who is in the organization.
        ownerOnly: true,
        keywords: ['scim', 'okta', 'azure', 'entra', 'deprovision', 'user sync'],
      },
      access: 'orgOwner',
      deniedReason:
        'provisioning: it decides who your identity provider adds to and removes from the organization',
      Component: lazyWithReload(() => import('@/pages/settings-area/OrgScimSection')),
    },
    {
      // The audit log leaving tripl: every entry POSTed, signed, to the
      // organization's SIEM. Right after the audit feed it exports.
      group: 'Organization',
      after: 'inst-audit',
      item: {
        id: 'org-audit-webhook',
        label: 'Audit webhook',
        icon: Webhook,
        path: ORG_AUDIT_WEBHOOK_PATH,
        ownerOnly: true,
        keywords: ['siem', 'stream', 'signature', 'hmac'],
      },
      access: 'orgOwner',
      deniedReason: "the audit webhook: it sends the organization's whole audit trail elsewhere",
      Component: lazyWithReload(() => import('@/pages/settings-area/OrgAuditWebhookSection')),
    },
  ],
  authPanels: [
    {
      id: 'sso',
      buttonLabel: 'Sign in with SSO',
      icon: KeyRound,
      title: 'Sign in with single sign-on',
      description:
        "Enter your work email and we will send you to your organization's identity provider.",
      Component: lazyWithReload(() =>
        import('@/pages/SsoSignInForm').then((module) => ({ default: module.SsoSignInForm })),
      ),
    },
  ],
  shellGates: [
    // An organization that requires single sign-on refuses a session that did
    // not come through its identity provider, on every request inside it:
    // offer the sign-in it would accept instead of a wall of failing panels.
    ({ errors, orgName, orgSlug, returnTo, otherOrgs }) => {
      const refusal = findSsoRequiredError(...errors)
      if (!refusal) return null
      return (
        <SsoRequiredState
          orgName={orgName}
          orgSlug={orgSlug}
          serverStart={ssoStartFromError(refusal)}
          returnTo={returnTo}
          otherOrgs={otherOrgs}
        />
      )
    },
  ],
}
