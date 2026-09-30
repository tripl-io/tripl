import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ChevronRight, FilePlus2, FileText } from 'lucide-react'
import { docsApi } from '@/api/docs'
import { Panel } from '@/components/settings/kit'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { DOC_LINK_KIND_NOUN, newNoteHref } from '@/lib/docLinks'
import { docRoute } from '@/lib/docTree'
import { docBacklinksKey } from '@/lib/docsQueryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import type { DocBacklinkItem, DocLinkKind } from '@/types/docs'

/*
 * The "Notes" card on entity pages (F22, F24): the docs-catalog notes that
 * link here with `[[event:…]]`, `[[variable:…]]`, `[[metric:…]]` and friends —
 * event, event-type and field pages, the variable page and the metric page.
 * The server lists only notes the reader can read. A plain list on
 * purpose — it must NOT import the Markdown renderer, which stays in the docs
 * page's own chunk so the event page does not pay for it.
 */

export function DocNotesSection({
  slug,
  kind,
  name,
  qualifier = null,
  className,
}: {
  slug: string
  kind: DocLinkKind
  /** The entity's name as a note would write it (links are by name, not id). */
  name: string
  /** The event-type name, for a field. */
  qualifier?: string | null
  className?: string
}) {
  const canWrite = useCanWriteProject()
  const query = useQuery({
    queryKey: docBacklinksKey(slug, kind, name, qualifier),
    queryFn: ({ signal }) => docsApi.backlinks(slug, { kind, name, qualifier }, signal),
    enabled: Boolean(slug && name),
    // A missing Notes card is not worth a toast on someone else's page.
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
  })
  const items = query.data?.items ?? []

  // Hidden while loading, on error, and when nothing links here — except for
  // an editor, who gets one slim line with the way to write the first note.
  if (!query.isSuccess) return null
  if (items.length === 0) {
    if (!canWrite) return null
    return (
      <div className={className}>
        <p className="m-0 flex flex-wrap items-center gap-2 text-body-sm text-fg-tertiary">
          <FileText className="size-3.5" aria-hidden />
          No notes link to this {DOC_LINK_KIND_NOUN[kind]} yet.
          <Link
            to={newNoteHref(slug, kind, name, qualifier)}
            className="text-[var(--accent)] underline-offset-2 hover:underline"
          >
            New note about this
          </Link>
        </p>
      </div>
    )
  }

  return (
    <Panel
      className={className}
      title="Notes"
      subtitle={`Docs that link to this ${DOC_LINK_KIND_NOUN[kind]}`}
      right={
        canWrite ? (
          <Button asChild variant="ghost" size="sm">
            <Link to={newNoteHref(slug, kind, name, qualifier)}>
              <FilePlus2 aria-hidden />
              New note about this
            </Link>
          </Button>
        ) : undefined
      }
    >
      <BacklinkList slug={slug} items={items} />
    </Panel>
  )
}

function BacklinkList({ slug, items }: { slug: string; items: readonly DocBacklinkItem[] }) {
  return (
    <ul className="m-0 list-none divide-y divide-border-subtle p-0">
      {items.map(item => (
        <li key={`${item.scope}:${item.path}`} className="px-4 py-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Link
              to={docRoute(slug, item.scope, item.path)}
              className="min-w-0 truncate font-medium text-fg hover:text-[var(--accent)] hover:underline"
            >
              {item.title}
            </Link>
            {item.scope === 'organization' && (
              <Chip size="xs" variant="outline">
                Organization
              </Chip>
            )}
            {item.audience !== 'both' && (
              <Chip size="xs" tone={item.audience === 'agent' ? 'info' : 'neutral'}>
                For {item.audience === 'agent' ? 'agents' : 'people'}
              </Chip>
            )}
          </div>
          <div className="mono mt-0.5 truncate text-caption text-fg-tertiary">{item.path}</div>
          {item.description && (
            <p className="m-0 mt-0.5 line-clamp-2 text-body-sm text-fg-secondary">{item.description}</p>
          )}
        </li>
      ))}
    </ul>
  )
}

/**
 * Per-field notes on an event type: one backlinks request per field, so it is
 * fetched only once the reader opens it.
 */
export function DocFieldNotes({
  slug,
  eventTypeName,
  fieldNames,
  className,
}: {
  slug: string
  eventTypeName: string
  fieldNames: readonly string[]
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const queries = useQueries({
    queries: fieldNames.map(fieldName => ({
      queryKey: docBacklinksKey(slug, 'field', fieldName, eventTypeName),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        docsApi.backlinks(slug, { kind: 'field', name: fieldName, qualifier: eventTypeName }, signal),
      enabled: open && Boolean(slug),
      meta: SILENT_ERROR_META,
      staleTime: 60_000,
    })),
  })
  if (fieldNames.length === 0) return null
  const loading = open && queries.some(q => q.isPending)
  const withNotes = fieldNames
    .map((fieldName, i) => ({ fieldName, items: queries[i]?.data?.items ?? [] }))
    .filter(row => row.items.length > 0)

  return (
    <div className={className}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
        className="inline-flex items-center gap-1 text-body-sm text-fg-secondary hover:text-fg"
      >
        <ChevronRight className={`size-3.5 transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden />
        Field notes
      </button>
      {open && (
        <div className="mt-2">
          {loading ? (
            <p className="m-0 text-body-sm text-fg-tertiary" role="status">
              Looking for notes about these fields…
            </p>
          ) : withNotes.length === 0 ? (
            <p className="m-0 text-body-sm text-fg-tertiary">No notes link to a field of this event type.</p>
          ) : (
            <div className="flex flex-col gap-3">
              {withNotes.map(row => (
                <Panel key={row.fieldName} headingLevel={3} title={<span className="mono">{row.fieldName}</span>}>
                  <BacklinkList slug={slug} items={row.items} />
                </Panel>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
