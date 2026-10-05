import { Webhook } from 'lucide-react'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { ORG_AUDIT_WEBHOOK_PATH } from '@/pages/settings-area/org-settings/auditExportModel'
import type { FrontendExtension } from '../types'

/**
 * The enterprise UI still in this repository: the audit webhook (F20),
 * registered as a bundled extension so the app reaches it only through the
 * registry. Moving it to the enterprise package moves this directory and
 * changes nothing in the app.
 */
export const bundledEnterprise: FrontendExtension = {
  name: 'bundled-enterprise',
  settingsSections: [
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
}
