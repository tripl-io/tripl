import { useState } from 'react'
import { Download } from 'lucide-react'
import { auditExportUrl, type AuditExportFormat } from '@/api/auditExport'
import { Field, NativeSelect, SCard } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  AUDIT_EXPORT_MAX_DAYS,
  defaultExportRange,
  exportRangeError,
} from './org-settings/auditExportModel'

const FORMAT_OPTIONS = [
  { value: 'csv', label: 'CSV (spreadsheets)' },
  { value: 'json', label: 'NDJSON (one JSON object per line)' },
] as const

/**
 * Organization › Audit log › Export (F20): the organization's audit entries
 * for a date range, its projects' included, as a file.
 *
 * The file is a plain link to the export endpoint with `download`, not a
 * fetch: the server streams it in chunks, and a year of entries is more than a
 * page should hold in memory to hand to a Blob. The link is only live while the
 * range is valid, so a click never saves the server's 422 as the file.
 * Owners and admins only; the gate is upstream, as for the feed below it.
 */
export function AuditExportCard({ org }: { org: string }) {
  const [format, setFormat] = useState<AuditExportFormat>('csv')
  const [range, setRange] = useState(() => defaultExportRange())
  const error = exportRangeError(range.from, range.to)
  const errorId = 'audit-export-range-error'

  return (
    <SCard
      title="Export"
      description={`Download every entry of this organization and its projects for up to ${AUDIT_EXPORT_MAX_DAYS} days at a time. Dates are in UTC, and both the first and the last day are included.`}
    >
      <Field label="Format" htmlFor="audit-export-format">
        <NativeSelect
          id="audit-export-format"
          value={format}
          onChange={(next) => setFormat(next === 'json' ? 'json' : 'csv')}
          options={FORMAT_OPTIONS}
        />
      </Field>
      <Field label="From" htmlFor="audit-export-from">
        <Input
          id="audit-export-from"
          type="date"
          value={range.from}
          max={range.to || undefined}
          aria-invalid={error !== null || undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(event) => setRange((current) => ({ ...current, from: event.target.value }))}
          className="w-auto"
        />
      </Field>
      <Field label="To" htmlFor="audit-export-to" last>
        <Input
          id="audit-export-to"
          type="date"
          value={range.to}
          min={range.from || undefined}
          aria-invalid={error !== null || undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(event) => setRange((current) => ({ ...current, to: event.target.value }))}
          className="w-auto"
        />
        {error && (
          <p id={errorId} role="alert" className="mt-1.5 text-body-sm text-danger">
            {error}
          </p>
        )}
      </Field>
      <div className="flex flex-wrap items-center gap-3 border-t border-border-subtle px-4 py-3">
        {error ? (
          <Button type="button" variant="outline" disabled>
            <Download aria-hidden="true" />
            Export
          </Button>
        ) : (
          <Button asChild variant="outline">
            <a href={auditExportUrl(org, { format, from: range.from, to: range.to })} download>
              <Download aria-hidden="true" />
              Export
            </a>
          </Button>
        )}
        <span className="text-body-sm text-fg-tertiary">
          CSV cells that a spreadsheet would run as a formula are prefixed with an apostrophe.
        </span>
      </div>
    </SCard>
  )
}
