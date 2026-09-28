import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  auditWebhookApi,
  type AuditDeliveryStatus,
  type AuditWebhookDelivery,
} from '@/api/auditExport'
import { ErrorState } from '@/components/error-state'
import { NativeSelect, SCard } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'
import { Badge, type BadgeVariant } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { formatTimestamp } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgAuditWebhookDeliveriesListKey } from '@/lib/queryKeys'
import { DELIVERY_STATUS_FILTERS, DELIVERY_STATUS_LABELS } from './auditExportModel'

/** How many recent deliveries the table shows. */
export const DELIVERIES_LIMIT = 50

const STATUS_VARIANT: Record<AuditDeliveryStatus, BadgeVariant> = {
  pending: 'neutral',
  sent: 'success',
  failed: 'warning',
  dead: 'danger',
}

const FILTER_OPTIONS = [
  { value: '', label: 'All statuses' },
  ...DELIVERY_STATUS_FILTERS.map((status) => ({ value: status, label: DELIVERY_STATUS_LABELS[status] })),
]

/**
 * The webhook's recent deliveries (F20): one row per audit entry on its way
 * out, with where it stands. "Retrying" is a failed attempt that will be tried
 * again; "Gave up" stopped after the last retry.
 */
export function AuditWebhookDeliveries({ org }: { org: string }) {
  const [status, setStatus] = useState<AuditDeliveryStatus | ''>('')
  const query = useQuery({
    queryKey: orgAuditWebhookDeliveriesListKey(org, status),
    queryFn: () =>
      auditWebhookApi.deliveries(org, { status: status || undefined, limit: DELIVERIES_LIMIT }),
    meta: SILENT_ERROR_META,
  })

  return (
    <SCard
      title="Recent deliveries"
      description={`The last ${DELIVERIES_LIMIT} audit entries sent, or waiting to be sent, to the webhook.`}
    >
      <div className="flex flex-wrap items-center gap-2 px-4 py-3 border-b border-border-subtle">
        <NativeSelect
          aria-label="Delivery status"
          size="sm"
          value={status}
          onChange={(next) => setStatus(next as AuditDeliveryStatus | '')}
          options={FILTER_OPTIONS}
        />
        <Button type="button" size="sm" variant="ghost" onClick={() => void query.refetch()}>
          Refresh
        </Button>
      </div>
      <DeliveriesBody
        rows={query.data}
        error={query.isError ? query.error : null}
        onRetry={() => void query.refetch()}
      />
    </SCard>
  )
}

function DeliveriesBody({
  rows,
  error,
  onRetry,
}: {
  rows: AuditWebhookDelivery[] | undefined
  error: unknown
  onRetry: () => void
}) {
  if (error && !rows) {
    return (
      <div className="p-4">
        <ErrorState compact title="Couldn't load deliveries" error={error} onRetry={onRetry} />
      </div>
    )
  }
  if (!rows) return <SectionSkeleton variant="table" label="Loading deliveries…" />
  if (rows.length === 0) {
    return <p className="m-0 px-4 py-3 text-body-sm text-fg-tertiary">No deliveries yet.</p>
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Action</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Attempts</TableHead>
          <TableHead>Recorded</TableHead>
          <TableHead>Sent or next attempt</TableHead>
          <TableHead>Last error</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.id}>
            <TableCell className="mono text-body-sm">{row.action}</TableCell>
            <TableCell>
              <Badge variant={STATUS_VARIANT[row.status]}>{DELIVERY_STATUS_LABELS[row.status]}</Badge>
            </TableCell>
            <TableCell className="tabular-nums">{row.attempts}</TableCell>
            <TableCell className="text-body-sm">{formatTimestamp(row.created_at, { seconds: true })}</TableCell>
            <TableCell className="text-body-sm">{whenCell(row)}</TableCell>
            <TableCell className="max-w-[280px] truncate text-body-sm text-fg-muted" title={row.last_error ?? undefined}>
              {row.last_error ?? '—'}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function whenCell(row: AuditWebhookDelivery): string {
  if (row.status === 'sent' && row.sent_at) return `Sent ${formatTimestamp(row.sent_at, { seconds: true })}`
  if ((row.status === 'pending' || row.status === 'failed') && row.next_attempt_at) {
    return `Next ${formatTimestamp(row.next_attempt_at, { seconds: true })}`
  }
  return '—'
}
