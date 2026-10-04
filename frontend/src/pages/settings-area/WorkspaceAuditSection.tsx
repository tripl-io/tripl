import { useActiveOrg } from '@/components/active-org-context'
import { SHeader } from '@/components/settings/kit'
import { WorkspaceAuditLog } from '@/pages/settings/AuditTab'
import { AuditExportCard } from './AuditExportCard'

/**
 * The instance-wide audit log (owner only).
 *
 * A whole family of actions was recorded by the backend and displayed by
 * nothing: a data source connected, changed or dropped; a member invited, an
 * invitation revoked, a role changed; a workspace API key minted or revoked.
 * They carry no project, and the only audit screen in the product filtered by
 * one — so the log's most security-relevant half was written faithfully and read
 * by nobody. A project's own DELETION had the same problem from
 * the other end: the entry is written once its subject is gone, so it carries no
 * project either, and the per-project tab lives under /p/:slug, where a deleted
 * project has no page left to open it from.
 *
 * The owner gate is upstream, in ``SettingsArea``, and again on the server: the
 * whole /audit router requires an interactive owner session. Nothing here
 * re-checks it, so there is one answer to "who may read this" rather than three
 * that can drift. The Export card above the feed (F20) takes the same gate on
 * the server: the organization's owners and admins.
 */
export default function WorkspaceAuditSection() {
  const { slug } = useActiveOrg()
  return (
    <div>
      <SHeader
        title="Audit log"
        description="Every recorded action on this instance, across all projects and outside them."
      />
      {slug && <AuditExportCard key={slug} org={slug} />}
      <WorkspaceAuditLog />
    </div>
  )
}
