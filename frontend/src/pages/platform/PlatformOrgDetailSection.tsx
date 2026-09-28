import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft } from 'lucide-react'
import { platformApi } from '@/api/platform'
import { ErrorState } from '@/components/error-state'
import { RoleChip } from '@/components/settings/role-chip'
import { InfoRow, SCard, SHeader } from '@/components/settings/kit'
import { EntityNotFound, SectionSkeleton, isNotFoundError } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useConfirm } from '@/hooks/useConfirm'
import { formatDate, formatDateTime } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { settingsPath } from '@/lib/navigation'
import { platformConsoleKey, platformOrgKey } from '@/lib/queryKeys'
import { StepInDialog, SuspendOrgDialog } from './PlatformDialogs'
import { OrgStatusChip } from './PlatformShared'
import { canStepIn, canSuspend, canUnsuspend, countLabel } from './platformModel'

const ORGS_PATH = '/settings/platform/orgs'

/**
 * Platform › Organizations › one organization (F20): its status, members and
 * projects — names, roles and dates only, never project content.
 */
export default function PlatformOrgDetailSection({ slug }: { slug: string }) {
  const queryClient = useQueryClient()
  const { confirm, dialog: confirmDialog } = useConfirm()
  const [dialog, setDialog] = useState<'suspend' | 'step-in' | null>(null)
  const orgQuery = useQuery({
    queryKey: platformOrgKey(slug),
    queryFn: ({ signal }) => platformApi.getOrg(slug, signal),
    meta: SILENT_ERROR_META,
  })

  const back = (
    <Link
      to={settingsPath(ORGS_PATH)}
      className="mb-3 inline-flex items-center gap-1 text-body-sm text-fg-tertiary no-underline hover:underline"
    >
      <ChevronLeft aria-hidden="true" className="size-3.5" />
      All organizations
    </Link>
  )

  if (orgQuery.isPending) {
    return (
      <div>
        {back}
        <SHeader eyebrow="Platform" title="Organization" />
        <SectionSkeleton variant="form" label="Loading organization…" />
      </div>
    )
  }
  if (orgQuery.isError) {
    if (isNotFoundError(orgQuery.error)) {
      return (
        <EntityNotFound
          title="Organization not found"
          description="It may have been deleted, or the address is wrong."
          back={{ label: 'All organizations', to: settingsPath(ORGS_PATH) }}
        />
      )
    }
    return (
      <div>
        {back}
        <ErrorState
          title="The organization could not be loaded"
          error={orgQuery.error}
          onRetry={() => void orgQuery.refetch()}
        />
      </div>
    )
  }

  const org = orgQuery.data
  const unsuspend = () =>
    void confirm({
      title: `Reinstate ${org.name}?`,
      message: 'Its members can use the organization again at once. This is recorded in its audit log.',
      confirmLabel: 'Unsuspend',
      variant: 'primary',
      errorPrefix: 'Could not unsuspend the organization',
      pendingLabel: 'Unsuspending…',
      action: async () => {
        await platformApi.unsuspendOrg(org.slug)
        await queryClient.invalidateQueries({ queryKey: platformConsoleKey() })
      },
    })

  return (
    <div>
      {back}
      <SHeader
        eyebrow="Platform"
        title={org.name}
        description={`${countLabel(org.member_count, 'member')} · ${countLabel(org.project_count, 'project')}`}
        actions={
          <span className="flex flex-wrap gap-2">
            {canStepIn(org) && (
              <Button type="button" variant="outline" onClick={() => setDialog('step-in')}>
                Step in (read-only)
              </Button>
            )}
            {canSuspend(org) && (
              <Button type="button" variant="danger" onClick={() => setDialog('suspend')}>
                Suspend
              </Button>
            )}
            {canUnsuspend(org) && (
              <Button type="button" variant="outline" onClick={unsuspend}>
                Unsuspend
              </Button>
            )}
          </span>
        }
      />
      <SCard title="Organization">
        <InfoRow label="Slug" value={org.slug} />
        <InfoRow label="Status" value={<OrgStatusChip status={org.status} />} mono={false} />
        {org.status === 'suspended' && (
          <>
            {org.suspended_at && (
              <InfoRow label="Suspended" value={formatDateTime(org.suspended_at)} mono={false} />
            )}
            <InfoRow label="Reason" value={org.suspended_reason || '—'} mono={false} />
          </>
        )}
        <InfoRow label="Created" value={formatDate(org.created_at)} mono={false} last />
      </SCard>
      <SCard title="Members" description={countLabel(org.members.length, 'member')}>
        {org.members.length === 0 ? (
          <p className="m-0 px-4 py-4 text-body text-fg-tertiary">No members.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Role</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {org.members.map((member) => (
                <TableRow key={member.email}>
                  <TableCell>{member.email}</TableCell>
                  <TableCell>{member.name || '—'}</TableCell>
                  <TableCell>
                    <RoleChip role={member.role} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </SCard>
      <SCard title="Projects" description={countLabel(org.projects.length, 'project')}>
        {org.projects.length === 0 ? (
          <p className="m-0 px-4 py-4 text-body text-fg-tertiary">No projects.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Slug</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {org.projects.map((project) => (
                <TableRow key={project.slug}>
                  <TableCell>{project.name}</TableCell>
                  <TableCell className="mono text-body-sm">{project.slug}</TableCell>
                  <TableCell className="whitespace-nowrap text-body-sm">{formatDate(project.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </SCard>
      {dialog === 'suspend' && <SuspendOrgDialog org={org} onClose={() => setDialog(null)} />}
      {dialog === 'step-in' && <StepInDialog org={org} onClose={() => setDialog(null)} />}
      {confirmDialog}
    </div>
  )
}
