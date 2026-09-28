import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { domainTxtName, domainTxtValue, domainVerified, ssoApi, type SsoDomain } from '@/api/sso'
import { SCard, TextInput } from '@/components/settings/kit'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { formatDateTime } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgSsoKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { domainError, normalizeDomain } from './orgSsoModel'

/**
 * The email domains an organization signs in with single sign-on (F20). Each
 * is claimed, then proved with a DNS TXT record before it counts: only a
 * verified domain's addresses are accepted from the identity provider, and
 * only then may an existing account with such an address be linked.
 */
export function OrgSsoDomainsCard({
  org,
  domains,
  enabled,
}: {
  org: string
  domains: readonly SsoDomain[]
  /** SSO is on: removing the last verified domain would lock people out. */
  enabled: boolean
}) {
  const qc = useQueryClient()
  const inputId = useId()
  const errorId = useId()
  const { confirm, dialog } = useConfirm()
  const [draft, setDraft] = useState('')
  const [submitted, setSubmitted] = useState(false)
  const [verifyNote, setVerifyNote] = useState<{ id: string; text: string; ok: boolean } | null>(null)

  const refresh = () => qc.invalidateQueries({ queryKey: orgSsoKey(org) })

  const addMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (domain: string) => ssoApi.addDomain(org, domain),
    onSuccess: () => {
      setDraft('')
      setSubmitted(false)
      void refresh()
    },
  })
  const verifyMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => ssoApi.verifyDomain(org, id),
    onSuccess: (domain, id) => {
      setVerifyNote({
        id,
        ok: domainVerified(domain),
        text:
          domainVerified(domain)
            ? 'Verified.'
            : 'The TXT record was not found yet. DNS changes can take a while to spread; try again later.',
      })
      void refresh()
    },
    onError: (error, id) => setVerifyNote({ id, ok: false, text: getErrorMessage(error) }),
  })

  const error = submitted ? domainError(draft) : null
  const verifiedCount = domains.filter(domainVerified).length

  const remove = async (domain: SsoDomain) => {
    const lastVerified = enabled && domainVerified(domain) && verifiedCount === 1
    await confirm({
      title: `Remove ${domain.domain}?`,
      message: lastVerified
        ? 'This is the only verified domain, and single sign-on is on. Nobody can sign in with single sign-on until another domain is verified.'
        : domainVerified(domain)
          ? `People with ${domain.domain} addresses can no longer sign in with single sign-on. Linked accounts keep their password sign-in.`
          : 'The claim and its verification token are removed.',
      confirmLabel: 'Remove domain',
      variant: 'danger',
      errorPrefix: 'Could not remove the domain',
      action: async () => {
        await ssoApi.deleteDomain(org, domain.id)
        await refresh()
      },
    })
  }

  return (
    <SCard
      title="Email domains"
      description="Single sign-on accepts addresses only at these domains, and each must be proved with a DNS TXT record first. A domain can belong to one organization on this instance."
    >
      {domains.length === 0 && (
        <p className="m-0 px-4 py-[15px] text-body-sm text-fg-tertiary">No domains yet.</p>
      )}
      <ul className="m-0 list-none p-0" aria-label="Email domains">
        {domains.map((domain) => (
          <li key={domain.id} className="space-y-2 border-b border-border-subtle px-4 py-[13px]">
            <div className="flex flex-wrap items-center gap-2">
              <span className="mono min-w-0 flex-1 truncate text-body font-medium">{domain.domain}</span>
              {domainVerified(domain) ? (
                <Badge variant="success">Verified</Badge>
              ) : (
                <Badge variant="warning">Not verified</Badge>
              )}
              {!domainVerified(domain) && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={verifyMut.isPending && verifyMut.variables === domain.id}
                  onClick={() => {
                    setVerifyNote(null)
                    verifyMut.mutate(domain.id)
                  }}
                >
                  {verifyMut.isPending && verifyMut.variables === domain.id ? 'Checking…' : 'Verify'}
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-label={`Remove ${domain.domain}`}
                onClick={() => void remove(domain)}
              >
                Remove
              </Button>
            </div>
            {domain.verified_at ? (
              <p className="m-0 text-caption text-fg-tertiary">
                Verified {formatDateTime(domain.verified_at)}
              </p>
            ) : (
              <TxtRecord domain={domain} />
            )}
            {verifyNote?.id === domain.id && (
              <p
                role={verifyNote.ok ? 'status' : 'alert'}
                className={verifyNote.ok ? 'm-0 text-caption text-fg-muted' : 'm-0 text-caption text-danger'}
              >
                {verifyNote.text}
              </p>
            )}
          </li>
        ))}
      </ul>
      <form
        className="flex flex-wrap items-start gap-2 px-4 py-[13px]"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          setSubmitted(true)
          if (domainError(draft)) return
          addMut.mutate(normalizeDomain(draft))
        }}
      >
        <div className="min-w-[200px] flex-1">
          <label htmlFor={inputId} className="sr-only">
            Domain to add
          </label>
          <TextInput
            id={inputId}
            value={draft}
            onChange={(next) => {
              setDraft(next)
              if (addMut.isError) addMut.reset()
            }}
            placeholder="e.g. example.com"
            mono
            aria-invalid={error !== null}
            aria-describedby={error || addMut.isError ? errorId : undefined}
          />
          {(error || addMut.isError) && (
            <p id={errorId} role="alert" className="mt-1 text-caption text-danger">
              {error ?? getErrorMessage(addMut.error)}
            </p>
          )}
        </div>
        <Button type="submit" variant="outline" disabled={addMut.isPending}>
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          {addMut.isPending ? 'Adding…' : 'Add domain'}
        </Button>
      </form>
      {dialog}
    </SCard>
  )
}

/** The record to publish, name and value, each selectable in one click. */
function TxtRecord({ domain }: { domain: SsoDomain }) {
  return (
    <div className="space-y-1 rounded-lg border border-border-subtle bg-bg-sunken px-3 py-2 text-caption">
      <p className="m-0 text-fg-tertiary">Add this DNS TXT record, then press Verify:</p>
      <RecordLine label="Name" value={domainTxtName(domain)} />
      <RecordLine label="Value" value={domainTxtValue(domain)} />
    </div>
  )
}

function RecordLine({ label, value }: { label: string; value: ReactNode }) {
  return (
    <p className="m-0 flex flex-wrap gap-2">
      <span className="w-12 shrink-0 text-fg-tertiary">{label}</span>
      <code className="mono min-w-0 select-all break-all text-fg">{value}</code>
    </p>
  )
}
