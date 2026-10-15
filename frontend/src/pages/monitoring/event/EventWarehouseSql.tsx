import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Copy } from 'lucide-react'
import { toast } from 'sonner'

import { eventsApi } from '@/api/events'
import { LazySqlEditor } from '@/components/sql-editor-lazy'
import { Button } from '@/components/ui/button'
import { eventWarehouseSqlKey } from '@/lib/queryKeys'

interface EventWarehouseSqlProps {
  slug: string
  branchId: string | null | undefined
  eventId: string
}

/**
 * The query that selects this event's rows from the warehouse, built from the
 * governing scan on request (nothing is stored). Fetched only once opened, so a
 * page view costs nothing, and refetched on every open, so an edited scan rule
 * shows at once.
 */
export function EventWarehouseSql({ slug, branchId, eventId }: EventWarehouseSqlProps) {
  const [open, setOpen] = useState(false)
  const query = useQuery({
    queryKey: eventWarehouseSqlKey(slug, branchId, eventId),
    queryFn: () => eventsApi.warehouseSql(slug, eventId, branchId),
    enabled: open,
  })
  const data = query.data
  return (
    <details
      className="rounded-md border"
      data-testid="event-warehouse-sql"
      onToggle={event => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer select-none px-3 py-1.5 text-body-sm font-medium text-fg-tertiary">
        Warehouse SQL
      </summary>
      <div className="space-y-2 border-t p-2">
        {query.isFetching && <p className="px-1 text-body-sm text-fg-tertiary">Loading SQL…</p>}
        {query.isError && (
          <p role="alert" className="px-1 text-body-sm text-destructive">
            Could not build the query.
          </p>
        )}
        {data && data.sql === null && (
          <p className="px-1 text-body-sm text-fg-tertiary">{data.reason}</p>
        )}
        {data?.sql && (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 px-1">
              <p className="text-caption text-fg-tertiary">
                From the scan <span className="font-medium text-foreground">{data.scan_config_name}</span>
                {' · '}{data.dialect}
                {' · '}the last day, 100 rows
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void copySql(data.sql ?? '')}
              >
                <Copy aria-hidden="true" />
                Copy SQL
              </Button>
            </div>
            {!data.exact && (
              <p className="px-1 text-body-sm text-fg-tertiary">
                Not every part of this event's identity could be written as a filter, so the
                query can return rows of other events too.
              </p>
            )}
            {data.notes.length > 0 && (
              <ul className="list-disc space-y-0.5 pl-6 text-caption text-fg-tertiary">
                {data.notes.map(note => <li key={note}>{note}</li>)}
              </ul>
            )}
            <LazySqlEditor
              value={data.sql}
              onChange={() => undefined}
              ariaLabel="Warehouse SQL"
              minHeight="120px"
              readOnly
            />
          </>
        )}
      </div>
    </details>
  )
}

async function copySql(sql: string) {
  try {
    await navigator.clipboard.writeText(sql)
    toast.success('SQL copied')
  } catch {
    toast.error('Could not copy to the clipboard')
  }
}
