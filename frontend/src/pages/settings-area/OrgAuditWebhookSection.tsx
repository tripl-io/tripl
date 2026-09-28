import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient, type UseMutationResult } from '@tanstack/react-query'
import {
  auditWebhookApi,
  type AuditWebhook,
  type AuditWebhookSaved,
  type AuditWebhookTestResult,
  type AuditWebhookUpdate,
} from '@/api/auditExport'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { Field, InfoRow, SCard, SHeader, SettingsSaveBar, TextInput, ToggleRow } from '@/components/settings/kit'
import { useUnsavedChanges } from '@/components/settings/unsaved-changes'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { formatTimestamp } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgAuditWebhookDeliveriesKey, orgAuditWebhookKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { AuditWebhookDeliveries } from './org-settings/AuditWebhookDeliveries'
import { AuditWebhookSecretReveal } from './org-settings/AuditWebhookSecretReveal'
import { ORG_AUDIT_WEBHOOK_PATH, webhookUrlError } from './org-settings/auditExportModel'

const UNSAVED_MESSAGE = 'Audit webhook settings you edited here have not been saved. Leaving this page drops them.'

/** Edits not yet saved. A field left out is the saved value. */
type WebhookDraft = Partial<AuditWebhookUpdate>

/**
 * Organization › Audit webhook (F20): every audit entry of the organization
 * and its projects, POSTed as signed JSON to one HTTPS endpoint (a SIEM, a log
 * pipeline). Organization OWNERS only; the area shows everyone else a notice.
 */
export default function OrgAuditWebhookSection() {
  const { slug } = useActiveOrg()
  return (
    <div>
      <SHeader
        title="Audit webhook"
        description="Send every audit entry of this organization and its projects to an HTTPS endpoint as it is recorded, signed with a secret only you and the receiver know."
      />
      {slug ? (
        <WebhookForm key={slug} org={slug} />
      ) : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
    </div>
  )
}

function WebhookForm({ org }: { org: string }) {
  const qc = useQueryClient()
  const { registerUnsaved } = useUnsavedChanges()
  const { confirm, dialog } = useConfirm()
  const [draft, setDraft] = useState<WebhookDraft>({})
  const [revealed, setRevealed] = useState<string | null>(null)

  const query = useQuery({
    queryKey: orgAuditWebhookKey(org),
    queryFn: () => auditWebhookApi.get(org),
    meta: SILENT_ERROR_META,
  })

  const refreshDeliveries = () => void qc.invalidateQueries({ queryKey: orgAuditWebhookDeliveriesKey(org) })
  const onSaved = (data: AuditWebhookSaved) => {
    // The secret is kept out of the cache: it is shown once, then gone.
    const { secret, ...webhook } = data
    qc.setQueryData<AuditWebhook | null>(orgAuditWebhookKey(org), webhook)
    if (secret) setRevealed(secret)
    refreshDeliveries()
  }

  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (update: AuditWebhookUpdate) => auditWebhookApi.save(org, update),
    onSuccess: (data) => {
      onSaved(data)
      setDraft({})
    },
  })
  const testMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => auditWebhookApi.test(org),
    onSettled: refreshDeliveries,
  })

  const webhook = query.data
  const url = draft.url ?? webhook?.url ?? ''
  const enabled = draft.enabled ?? webhook?.enabled ?? true
  const dirty = webhook
    ? (draft.url !== undefined && draft.url.trim() !== webhook.url)
      || (draft.enabled !== undefined && draft.enabled !== webhook.enabled)
    : draft.url !== undefined && draft.url.trim() !== ''
  const urlError = draft.url !== undefined ? webhookUrlError(draft.url) : null

  useEffect(() => {
    registerUnsaved(
      dirty ? { keptBy: () => false, message: UNSAVED_MESSAGE, dirtyPaths: [ORG_AUDIT_WEBHOOK_PATH] } : null,
    )
    return () => registerUnsaved(null)
  }, [dirty, registerUnsaved])

  if (query.isError && query.data === undefined) {
    return (
      <ErrorState
        title="Couldn't load the audit webhook"
        error={query.error}
        onRetry={() => void query.refetch()}
      />
    )
  }
  if (query.data === undefined) return <SectionSkeleton variant="form" label="Loading the audit webhook…" />

  const rotate = () =>
    void confirm({
      title: 'Rotate the signing secret?',
      message:
        'A new secret is generated and shown once. Deliveries are signed with it from now on, so your receiver rejects them until you give it the new secret.',
      confirmLabel: 'Rotate secret',
      variant: 'danger',
      errorPrefix: 'Could not rotate the secret',
      action: async () => {
        // The answer is the whole webhook plus the new secret; only the
        // webhook goes to the cache.
        const { secret, ...rotated } = await auditWebhookApi.rotateSecret(org)
        setRevealed(secret)
        qc.setQueryData<AuditWebhook | null>(orgAuditWebhookKey(org), rotated)
      },
    })

  const remove = () =>
    void confirm({
      title: 'Delete the audit webhook?',
      message:
        'Audit entries stop being sent to this endpoint, and the signing secret is discarded. The audit log itself is kept.',
      confirmLabel: 'Delete webhook',
      variant: 'danger',
      errorPrefix: 'Could not delete the webhook',
      action: async () => {
        await auditWebhookApi.remove(org)
        qc.setQueryData<AuditWebhook | null>(orgAuditWebhookKey(org), null)
        setDraft({})
        setRevealed(null)
        refreshDeliveries()
      },
    })

  return (
    <div className="min-w-0 space-y-5">
      <SettingsSaveBar
        note={webhook ? 'Changes apply to the next delivery.' : 'Saving creates the webhook and shows its signing secret once.'}
        error={saveMut.isError ? getErrorMessage(saveMut.error) : undefined}
        dirty={dirty}
        invalid={urlError !== null || url.trim() === ''}
        invalidMessage={urlError ?? 'Enter the URL that receives the events.'}
        pending={saveMut.isPending}
        saveLabel={webhook ? 'Save changes' : 'Create webhook'}
        onDiscard={() => setDraft({})}
        onSave={() => saveMut.mutate({ url: url.trim(), enabled })}
      />

      {revealed && <AuditWebhookSecretReveal secret={revealed} onDone={() => setRevealed(null)} />}

      <SCard
        title="Endpoint"
        description="tripl POSTs one JSON object per audit entry. Answer with any 2xx status; anything else, a redirect or no answer within 15 seconds is retried later."
      >
        <Field
          label="URL"
          htmlFor="audit-webhook-url"
          hint="https only, and it must be a public address."
          error={urlError ?? undefined}
        >
          <TextInput
            id="audit-webhook-url"
            value={url}
            onChange={(next) => setDraft((current) => ({ ...current, url: next }))}
            placeholder="https://siem.example.com/hooks/tripl"
            autoComplete="off"
            mono
          />
        </Field>
        <ToggleRow
          label="Send audit entries"
          hint={enabled ? 'New audit entries are queued for delivery.' : 'Paused: new audit entries are not sent.'}
          value={enabled}
          onChange={(next) => setDraft((current) => ({ ...current, enabled: next }))}
          last
        />
      </SCard>

      {webhook && (
        <>
          <SCard title="Status">
            <InfoRow
              label="Signing secret"
              value={webhook.secret_configured ? 'Configured' : 'Not configured'}
              mono={false}
            />
            <InfoRow
              label="Last successful delivery"
              value={webhook.last_success_at ? formatTimestamp(webhook.last_success_at, { seconds: true }) : 'Never'}
              mono={false}
            />
            <InfoRow
              label="Last error"
              value={
                webhook.last_error
                  ? `${webhook.last_error}${webhook.last_error_at ? ` (${formatTimestamp(webhook.last_error_at, { seconds: true })})` : ''}`
                  : 'None'
              }
              mono={false}
              last
            />
          </SCard>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" variant="outline" disabled={testMut.isPending || dirty} onClick={() => testMut.mutate()}>
              {testMut.isPending ? 'Sending…' : 'Send test event'}
            </Button>
            <Button type="button" variant="outline" onClick={rotate}>
              Rotate secret
            </Button>
            <Button type="button" variant="danger" onClick={remove}>
              Delete webhook
            </Button>
            {dirty && <span className="text-body-sm text-fg-tertiary">Save first to test the saved settings.</span>}
            <TestResult mutation={testMut} />
          </div>

          <AuditWebhookDeliveries org={org} />
        </>
      )}
      {dialog}
    </div>
  )
}

function TestResult({ mutation }: { mutation: UseMutationResult<AuditWebhookTestResult, Error, void> }) {
  if (mutation.isError) {
    return (
      <span role="alert" className="text-body-sm text-danger">
        {getErrorMessage(mutation.error)}
      </span>
    )
  }
  if (!mutation.isSuccess || !mutation.data) return null
  const { ok, status_code: status, error } = mutation.data
  const code = status === null ? 'no response' : `HTTP ${status}`
  return (
    <span role="status" className={ok ? 'text-body-sm text-fg-muted' : 'text-body-sm text-danger'}>
      {ok ? `Delivered: the receiver answered ${code}.` : `Not delivered (${code})${error ? `: ${error}` : '.'}`}
    </span>
  )
}
