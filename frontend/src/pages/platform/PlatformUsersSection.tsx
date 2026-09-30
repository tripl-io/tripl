import { useState } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { platformApi, type PlatformUser } from '@/api/platform'
import { useAuth } from '@/components/auth-context'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { SCard, SHeader } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useConfirm } from '@/hooks/useConfirm'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { formatDate } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { platformConsoleKey, platformUsersKey } from '@/lib/queryKeys'
import { Pager } from './PlatformShared'
import { PLATFORM_PAGE_SIZE } from './platformModel'

/**
 * Platform › Users (F20): every account on the instance, and who holds the
 * platform admin flag. Granting and revoking it is audited; the server refuses
 * to revoke the last platform admin, or your own flag.
 */
export default function PlatformUsersSection() {
  const auth = useAuth()
  const queryClient = useQueryClient()
  const { confirm, dialog } = useConfirm()
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const q = useDebouncedValue(search.trim())
  const params = { q: q || undefined, limit: PLATFORM_PAGE_SIZE, offset }

  const usersQuery = useQuery({
    queryKey: platformUsersKey(params),
    queryFn: ({ signal }) => platformApi.listUsers(params, signal),
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })

  const setAdmin = (user: PlatformUser, grant: boolean) => {
    const who = user.name ? `${user.name} (${user.email})` : user.email
    void confirm({
      title: grant ? 'Make platform admin?' : 'Revoke platform admin?',
      message: grant
        ? `${who} will run this instance: its platform settings, every organization in this console, suspensions and read-only step-ins.`
        : `${who} loses the platform settings and this console. Their organization roles stay as they are.`,
      confirmLabel: grant ? 'Make platform admin' : 'Revoke',
      variant: grant ? 'primary' : 'danger',
      errorPrefix: grant ? 'Could not grant platform admin' : 'Could not revoke platform admin',
      pendingLabel: grant ? 'Granting…' : 'Revoking…',
      action: async () => {
        await platformApi.setPlatformAdmin(user.id, grant)
        await queryClient.invalidateQueries({ queryKey: platformConsoleKey() })
      },
    })
  }

  const items = usersQuery.data?.items ?? []
  const total = usersQuery.data?.total ?? 0

  return (
    <div>
      <SHeader
        eyebrow="Platform"
        title="Users"
        description="Every account on this instance. Platform admins run the instance and this console; it grants nothing inside an organization."
      />
      <div className="mb-4">
        <Input
          type="search"
          aria-label="Search users"
          placeholder="Search by email or name"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
            setOffset(0)
          }}
          className="max-w-xs"
        />
      </div>
      {usersQuery.isPending ? (
        <SectionSkeleton variant="list" rows={5} label="Loading users…" />
      ) : usersQuery.isError ? (
        <ErrorState
          title="Users could not be loaded"
          error={usersQuery.error}
          onRetry={() => void usersQuery.refetch()}
        />
      ) : (
        <SCard>
          {items.length === 0 ? (
            <p className="m-0 px-4 py-6 text-body text-fg-tertiary">
              {q ? 'No user matches this search.' : 'There are no users yet.'}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User</TableHead>
                  <TableHead className="text-right">Organizations</TableHead>
                  <TableHead>Email status</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Platform admin</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((user) => {
                  const self = user.id === auth.user?.id
                  return (
                    <TableRow key={user.id}>
                      <TableCell>
                        <div className="font-medium">{user.name || user.email}</div>
                        {user.name && <div className="text-caption text-fg-tertiary">{user.email}</div>}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{user.org_count.toLocaleString()}</TableCell>
                      <TableCell>
                        {user.email_verified ? (
                          <Chip tone="success">Verified</Chip>
                        ) : (
                          <Chip tone="warning">Unverified</Chip>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-body-sm">{formatDate(user.created_at)}</TableCell>
                      <TableCell>
                        <span className="flex items-center gap-2">
                          {user.is_platform_admin && <Chip tone="accent">Platform admin</Chip>}
                          {user.is_platform_admin ? (
                            self ? (
                              <span className="text-caption text-fg-tertiary">You</span>
                            ) : (
                              <Button
                                type="button"
                                size="xs"
                                variant="danger"
                                aria-label={`Revoke platform admin from ${user.email}`}
                                onClick={() => setAdmin(user, false)}
                              >
                                Revoke
                              </Button>
                            )
                          ) : (
                            <Button
                              type="button"
                              size="xs"
                              variant="outline"
                              aria-label={`Make ${user.email} a platform admin`}
                              onClick={() => setAdmin(user, true)}
                            >
                              Make platform admin
                            </Button>
                          )}
                        </span>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
          <Pager label="Users pages" offset={offset} shown={items.length} total={total} onOffset={setOffset} />
        </SCard>
      )}
      {dialog}
    </div>
  )
}
