import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy } from 'lucide-react'
import { orgGroupsApi, orgGroupsKey, orgMembersKey } from '@/api/orgGroups'
import {
  orgScimConfigKey,
  orgScimTokensKey,
  scimApi,
  type ScimToken,
  type ScimTokenCreated,
} from '@/api/scim'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { Field, InfoRow, NativeSelect, SCard, SHeader } from '@/components/settings/kit'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useConfirm } from '@/hooks/useConfirm'
import { useCopyToClipboard } from '@/hooks/useCopyToClipboard'
import { formatIsoDate } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { usersKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { scimBaseUrl, scimTokenLabel, sortScimTokens } from './org-settings/orgScimModel'

/**
 * Organization › Provisioning (SCIM) (F20): the SCIM 2.0 endpoint an identity
 * provider (Okta, Microsoft Entra ID, …) pushes users and groups to, its bearer
 * tokens, and which provisioned group makes its members organization admins.
 * Organization OWNERS only; the area shows everyone else a notice.
 */
export default function OrgScimSection() {
  const { slug } = useActiveOrg()
  return (
    <div>
      <SHeader
        title="Provisioning (SCIM)"
        description="Let your identity provider create, update and deactivate this organization's members and groups over SCIM 2.0."
      />
      {slug ? (
        <OrgScim key={slug} org={slug} />
      ) : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
    </div>
  )
}

function OrgScim({ org }: { org: string }) {
  const configQuery = useQuery({
    queryKey: orgScimConfigKey(org),
    queryFn: () => scimApi.getConfig(org),
    meta: SILENT_ERROR_META,
  })
  const tokensQuery = useQuery({
    queryKey: orgScimTokensKey(org),
    queryFn: () => scimApi.listTokens(org),
    meta: SILENT_ERROR_META,
  })

  const failed = configQuery.isError ? configQuery.error : tokensQuery.isError ? tokensQuery.error : null
  if (failed) {
    return (
      <ErrorState
        title="Couldn't load provisioning settings"
        error={failed}
        onRetry={() => {
          void configQuery.refetch()
          void tokensQuery.refetch()
        }}
      />
    )
  }
  if (!configQuery.data || !tokensQuery.data) {
    return <SectionSkeleton variant="form" label="Loading provisioning…" />
  }

  return (
    <div className="min-w-0">
      <SCard
        title="SCIM endpoint"
        description="In your identity provider, add a SCIM 2.0 app (Okta: SCIM provisioning; Microsoft Entra ID: enterprise application, automatic provisioning), then enter this base URL and a token from below as the Bearer token."
      >
        <InfoRow
          label="Base URL"
          value={scimBaseUrl(window.location.origin, org, configQuery.data.base_url)}
        />
        <InfoRow label="Authentication" value="HTTP header, Bearer token" mono={false} />
        <InfoRow label="User name" value="The user's email address" mono={false} last />
      </SCard>
      <ScimTokensCard org={org} tokens={tokensQuery.data} />
      <AdminGroupCard
        org={org}
        adminGroupId={configQuery.data.admin_group_id}
        adminGroupName={configQuery.data.admin_group_name}
      />
    </div>
  )
}

function ScimTokensCard({ org, tokens }: { org: string; tokens: ScimToken[] }) {
  const qc = useQueryClient()
  const { confirm, dialog } = useConfirm()
  const [revealed, setRevealed] = useState<ScimTokenCreated | null>(null)
  const tokenRef = useRef<HTMLInputElement>(null)
  const { state: copyState, copy, reset: resetCopy } = useCopyToClipboard(tokenRef)

  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => scimApi.createToken(org),
    onSuccess: (created) => {
      void qc.invalidateQueries({ queryKey: orgScimTokensKey(org) })
      resetCopy()
      setRevealed(created)
    },
  })

  const revoke = (token: ScimToken) =>
    void confirm({
      title: `Revoke ${scimTokenLabel(token)}?`,
      message:
        'Your identity provider can no longer provision with this token. Members and groups it created stay as they are.',
      confirmLabel: 'Revoke token',
      variant: 'danger',
      errorPrefix: 'Could not revoke the token',
      pendingLabel: 'Revoking…',
      action: async () => {
        await scimApi.revokeToken(org, token.id)
        await qc.invalidateQueries({ queryKey: orgScimTokensKey(org) })
      },
    })

  const sorted = sortScimTokens(tokens)

  return (
    <>
      {dialog}
      <SCard
        title="Tokens"
        description={
          sorted.length
            ? 'A token is shown in full only once, when it is created. Treat it like a password.'
            : 'No tokens yet. Create one for your identity provider.'
        }
        footer={
          <div className="flex w-full flex-wrap items-center justify-end gap-2">
            {createMut.isError && (
              <p role="alert" className="m-0 mr-auto text-body-sm text-danger">
                {getErrorMessage(createMut.error)}
              </p>
            )}
            <Button type="button" size="sm" disabled={createMut.isPending} onClick={() => createMut.mutate()}>
              {createMut.isPending ? 'Creating…' : 'Create token'}
            </Button>
          </div>
        }
      >
        {sorted.map((token, index) => (
          <div
            key={token.id}
            className="flex flex-wrap items-center gap-3 px-4 py-[11px]"
            style={{ borderBottom: index === sorted.length - 1 ? 'none' : '1px solid var(--border-subtle)' }}
          >
            <div className="min-w-0 flex-1">
              <div className="mono truncate text-body-sm text-fg">{scimTokenLabel(token)}</div>
              <div className="text-caption text-fg-tertiary">
                Created {formatIsoDate(token.created_at)}
                {token.created_by_email ? ` by ${token.created_by_email}` : ''}
                {' · '}
                {token.last_used_at ? `last used ${formatIsoDate(token.last_used_at)}` : 'never used'}
              </div>
            </div>
            {token.revoked_at ? (
              <Chip tone="neutral">Revoked {formatIsoDate(token.revoked_at)}</Chip>
            ) : (
              <Button
                type="button"
                size="sm"
                variant="danger"
                aria-label={`Revoke ${scimTokenLabel(token)}`}
                onClick={() => revoke(token)}
              >
                Revoke
              </Button>
            )}
          </div>
        ))}
      </SCard>

      {/* One-time reveal. Only the footer button closes it: Esc or a stray
          click outside would discard a token the server never returns again. */}
      <Dialog open={revealed != null}>
        <DialogContent
          showCloseButton={false}
          onEscapeKeyDown={(event) => event.preventDefault()}
          onInteractOutside={(event) => event.preventDefault()}
        >
          <DialogHeader>
            <DialogTitle>Copy your SCIM token now</DialogTitle>
            <DialogDescription>
              This token is shown only once. Paste it into your identity provider&rsquo;s SCIM settings.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-2">
            <div className="flex items-center gap-2">
              <input
                ref={tokenRef}
                readOnly
                aria-label="SCIM token"
                value={revealed?.token ?? ''}
                onFocus={(e) => e.currentTarget.select()}
                className="mono h-9 min-w-0 flex-1 rounded-md border px-2 text-body-sm border-border bg-background text-fg"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  if (revealed) void copy(revealed.token)
                }}
              >
                <Copy aria-hidden="true" className="h-3.5 w-3.5" />
                {copyState === 'copied' ? 'Copied' : 'Copy'}
              </Button>
            </div>
            <div aria-live="polite" aria-atomic="true">
              {copyState === 'copied' && <p className="text-caption text-success">Copied to the clipboard.</p>}
            </div>
            {copyState === 'failed' && (
              <p role="alert" className="text-caption text-danger">
                Couldn&rsquo;t reach the clipboard. The token above is selected — press Ctrl/⌘+C to copy it.
              </p>
            )}
          </div>
          <DialogFooter>
            <Button
              onClick={() => {
                setRevealed(null)
                resetCopy()
              }}
            >
              {copyState === 'copied' ? 'Done' : 'I’ve saved it'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function AdminGroupCard({
  org,
  adminGroupId,
  adminGroupName,
}: {
  org: string
  adminGroupId: string | null
  adminGroupName?: string | null
}) {
  const qc = useQueryClient()
  const current = adminGroupId ?? ''
  const [pick, setPick] = useState(current)
  const groupsQuery = useQuery({
    queryKey: orgGroupsKey(org),
    queryFn: () => orgGroupsApi.list(org),
    meta: SILENT_ERROR_META,
  })
  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (groupId: string | null) => scimApi.updateConfig(org, { admin_group_id: groupId }),
    onSuccess: (data) => {
      qc.setQueryData(orgScimConfigKey(org), data)
      setPick(data.admin_group_id ?? '')
      // The mapping promotes and demotes members at once: every loaded roster is stale.
      void qc.invalidateQueries({ queryKey: usersKey() })
      void qc.invalidateQueries({ queryKey: orgMembersKey(org) })
    },
  })

  const groups = groupsQuery.data ?? []
  const dirty = pick !== current
  // A mapped group that is not in the list (deleted meanwhile) still shows.
  const missing = current && !groups.some((g) => g.id === current)

  return (
    <SCard
      title="Admin group"
      description="Members of this group are made organization admins; someone removed from it goes back to member. Owners are never changed, and no group maps to owner."
      footer={
        <div className="flex w-full flex-wrap items-center justify-end gap-2">
          {saveMut.isError && (
            <p role="alert" className="m-0 mr-auto text-body-sm text-danger">
              {getErrorMessage(saveMut.error)}
            </p>
          )}
          {saveMut.isSuccess && !dirty && (
            <p role="status" className="m-0 mr-auto text-body-sm text-success">
              Saved
            </p>
          )}
          <Button
            type="button"
            size="sm"
            disabled={!dirty || saveMut.isPending}
            onClick={() => saveMut.mutate(pick || null)}
          >
            {saveMut.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      }
    >
      <Field
        label="Group"
        htmlFor="scim-admin-group"
        hint="Usually a group your identity provider pushes. Applied on every change to the group's members."
        last
      >
        <NativeSelect
          id="scim-admin-group"
          width="fill"
          value={pick}
          onChange={setPick}
          disabled={groupsQuery.isPending}
          options={[
            { value: '', label: groupsQuery.isPending ? 'Loading groups…' : 'No admin group' },
            ...(missing ? [{ value: current, label: adminGroupName ?? 'Current admin group' }] : []),
            ...groups.map((g) => ({
              value: g.id,
              label: g.managed_by_scim ? `${g.name} (SCIM)` : g.name,
            })),
          ]}
        />
        {groupsQuery.isError && (
          <p role="alert" className="mt-1 text-caption text-danger">
            Couldn&rsquo;t load the organization&rsquo;s groups: {getErrorMessage(groupsQuery.error)}
          </p>
        )}
      </Field>
    </SCard>
  )
}
