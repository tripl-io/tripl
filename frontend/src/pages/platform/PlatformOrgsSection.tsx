import { useState } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { platformApi, type PlatformOrg, type PlatformOrgStatus } from '@/api/platform'
import { ErrorState } from '@/components/error-state'
import { NativeSelect, SCard, SHeader } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useConfirm } from '@/hooks/useConfirm'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { formatDate } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { platformConsoleKey, platformOrgsKey } from '@/lib/queryKeys'
import { StepInDialog, SuspendOrgDialog } from './PlatformDialogs'
import { OrgStatusChip, Pager } from './PlatformShared'
import { PLATFORM_PAGE_SIZE, canStepIn, canSuspend, canUnsuspend, platformOrgPath } from './platformModel'

const STATUS_OPTIONS = [
  { value: '', label: 'Any status' },
  { value: 'active', label: 'Active' },
  { value: 'suspended', label: 'Suspended' },
  { value: 'deleting', label: 'Deleting' },
] as const

type Dialog = { kind: 'suspend' | 'step-in'; org: PlatformOrg } | null

/**
 * Platform › Organizations (F20): every organization on the instance with its
 * status and counts — metadata only, never project content. From here a
 * platform admin suspends or reinstates one, or steps in to it read-only.
 */
export default function PlatformOrgsSection() {
  const queryClient = useQueryClient()
  const { confirm, dialog: confirmDialog } = useConfirm()
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'' | PlatformOrgStatus>('')
  const [offset, setOffset] = useState(0)
  const [dialog, setDialog] = useState<Dialog>(null)
  const q = useDebouncedValue(search.trim())
  const params = { q: q || undefined, status: status || undefined, limit: PLATFORM_PAGE_SIZE, offset }

  const orgsQuery = useQuery({
    queryKey: platformOrgsKey(params),
    queryFn: ({ signal }) => platformApi.listOrgs(params, signal),
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })

  const unsuspend = (org: PlatformOrg) =>
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

  const items = orgsQuery.data?.items ?? []
  const total = orgsQuery.data?.total ?? 0

  return (
    <div>
      <SHeader
        eyebrow="Platform"
        title="Organizations"
        description="Every organization on this instance, with its status and size. Metadata only: project content stays inside each organization."
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Input
          type="search"
          aria-label="Search organizations"
          placeholder="Search by name or slug"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
            setOffset(0)
          }}
          className="max-w-xs"
        />
        <NativeSelect
          aria-label="Status"
          value={status}
          options={STATUS_OPTIONS}
          onChange={(next) => {
            setStatus(next as '' | PlatformOrgStatus)
            setOffset(0)
          }}
        />
      </div>
      {orgsQuery.isPending ? (
        <SectionSkeleton variant="list" rows={5} label="Loading organizations…" />
      ) : orgsQuery.isError ? (
        <ErrorState
          title="Organizations could not be loaded"
          error={orgsQuery.error}
          onRetry={() => void orgsQuery.refetch()}
        />
      ) : (
        <SCard>
          {items.length === 0 ? (
            <p className="m-0 px-4 py-6 text-body text-fg-tertiary">
              {q || status ? 'No organization matches these filters.' : 'There are no organizations yet.'}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Organization</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Members</TableHead>
                  <TableHead className="text-right">Projects</TableHead>
                  <TableHead>Owners</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((org) => (
                  <TableRow key={org.id}>
                    <TableCell>
                      <Link to={platformOrgPath(org.slug)} className="font-medium text-fg no-underline hover:underline">
                        {org.name}
                      </Link>
                      <div className="mono text-caption text-fg-tertiary">{org.slug}</div>
                    </TableCell>
                    <TableCell>
                      <OrgStatusChip status={org.status} />
                      {org.status === 'suspended' && org.suspended_reason && (
                        <div className="mt-1 max-w-xs truncate text-caption text-fg-tertiary" title={org.suspended_reason}>
                          {org.suspended_reason}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{org.member_count.toLocaleString()}</TableCell>
                    <TableCell className="text-right tabular-nums">{org.project_count.toLocaleString()}</TableCell>
                    <TableCell className="max-w-xs truncate text-body-sm">
                      {org.owner_emails.length > 0 ? org.owner_emails.join(', ') : '—'}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-body-sm">{formatDate(org.created_at)}</TableCell>
                    <TableCell>
                      <span className="flex justify-end gap-1.5">
                        {canStepIn(org) && (
                          <Button
                            type="button"
                            size="xs"
                            variant="outline"
                            aria-label={`Step in to ${org.name} (read-only)`}
                            onClick={() => setDialog({ kind: 'step-in', org })}
                          >
                            Step in (read-only)
                          </Button>
                        )}
                        {canSuspend(org) && (
                          <Button
                            type="button"
                            size="xs"
                            variant="danger"
                            aria-label={`Suspend ${org.name}`}
                            onClick={() => setDialog({ kind: 'suspend', org })}
                          >
                            Suspend
                          </Button>
                        )}
                        {canUnsuspend(org) && (
                          <Button
                            type="button"
                            size="xs"
                            variant="outline"
                            aria-label={`Unsuspend ${org.name}`}
                            onClick={() => unsuspend(org)}
                          >
                            Unsuspend
                          </Button>
                        )}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <Pager
            label="Organizations pages"
            offset={offset}
            shown={items.length}
            total={total}
            onOffset={setOffset}
          />
        </SCard>
      )}
      {dialog?.kind === 'suspend' && <SuspendOrgDialog org={dialog.org} onClose={() => setDialog(null)} />}
      {dialog?.kind === 'step-in' && <StepInDialog org={dialog.org} onClose={() => setDialog(null)} />}
      {confirmDialog}
    </div>
  )
}
