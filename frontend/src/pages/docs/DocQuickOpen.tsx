import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Command } from 'cmdk'
import { Building2, FileText, Search, TextSearch } from 'lucide-react'
import { docsApi } from '@/api/docs'
import { PALETTE_ITEM_CLASS } from '@/components/palette-item'
import { Kbd } from '@/components/primitives/kbd'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { SEARCH_DEBOUNCE_MS, useDebouncedValue } from '@/hooks/useDebouncedValue'
import { docMatchScore, docRoute } from '@/lib/docTree'
import { docSearchKey } from '@/lib/docsQueryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { DocScope, DocSummary } from '@/types/docs'

const MAX_ROWS = 50

/**
 * Quick open (Ctrl/⌘+P on the docs page): fuzzy over every note's title and
 * path, both scopes, with the catalog's full-text search underneath for words
 * that are only in a note's body.
 */
export function DocQuickOpen({
  slug,
  open,
  onOpenChange,
  projectDocs,
  organizationDocs,
}: {
  slug: string
  open: boolean
  onOpenChange: (open: boolean) => void
  projectDocs: readonly DocSummary[]
  organizationDocs: readonly DocSummary[]
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl gap-0 overflow-hidden p-0" showCloseButton={false}>
        <DialogTitle className="sr-only">Open a note</DialogTitle>
        {open && (
          <QuickOpenBody
            slug={slug}
            onClose={() => onOpenChange(false)}
            projectDocs={projectDocs}
            organizationDocs={organizationDocs}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function QuickOpenBody({
  slug,
  onClose,
  projectDocs,
  organizationDocs,
}: {
  slug: string
  onClose: () => void
  projectDocs: readonly DocSummary[]
  organizationDocs: readonly DocSummary[]
}) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const debounced = useDebouncedValue(query.trim(), SEARCH_DEBOUNCE_MS)

  const matches = useMemo(() => {
    const all = [...projectDocs, ...organizationDocs]
    if (!query.trim()) {
      return [...all].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, MAX_ROWS)
    }
    return all
      .map(doc => ({ doc, score: docMatchScore(query, doc) }))
      .filter((row): row is { doc: DocSummary; score: number } => row.score !== null)
      .sort((a, b) => b.score - a.score)
      .slice(0, MAX_ROWS)
      .map(row => row.doc)
  }, [projectDocs, organizationDocs, query])

  const content = useQuery({
    queryKey: docSearchKey(slug, debounced),
    queryFn: ({ signal }) => docsApi.search(slug, { q: debounced, limit: 10 }, signal),
    enabled: debounced.length >= 2,
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })
  const shown = new Set(matches.map(doc => `${doc.scope}:${doc.path}`))
  const bodyHits = debounced.length >= 2 ? (content.data?.items ?? []).filter(hit => !shown.has(`${hit.scope}:${hit.path}`)) : []

  const go = (scope: DocScope, path: string) => {
    onClose()
    navigate(docRoute(slug, scope, path))
  }

  return (
    <Command label="Open a note" shouldFilter={false} className="flex max-h-[440px] flex-col">
      <div className="flex items-center gap-2 border-b border-border-subtle px-3.5 py-3">
        <Search className="size-3.5 text-fg-tertiary" aria-hidden />
        <Command.Input
          // eslint-disable-next-line jsx-a11y/no-autofocus -- quick open: focus on an explicit Ctrl/⌘+P is the expected UX
          autoFocus
          value={query}
          onValueChange={setQuery}
          placeholder="Open a note by title or path…"
          className="flex-1 bg-transparent text-body outline-none placeholder:text-[var(--fg-subtle)]"
        />
        <Kbd>esc</Kbd>
      </div>
      <Command.List className="flex-1 overflow-y-auto py-1.5">
        {matches.length === 0 && bodyHits.length === 0 && (
          <p className="m-0 px-4 py-6 text-center text-body-sm text-fg-tertiary">No note matches.</p>
        )}
        {matches.length > 0 && (
          <Command.Group heading={query.trim() ? 'Notes' : 'Recently updated'} className="px-1.5 [&_[cmdk-group-heading]]:micro-label [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1 [&_[cmdk-group-heading]]:text-fg-tertiary">
            {matches.map(doc => (
              <Command.Item
                key={`${doc.scope}:${doc.path}`}
                value={`note:${doc.scope}:${doc.path}`}
                onSelect={() => go(doc.scope, doc.path)}
                className={PALETTE_ITEM_CLASS}
              >
                {doc.scope === 'organization' ? (
                  <Building2 className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                ) : (
                  <FileText className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                )}
                <span className="min-w-0 flex-1 truncate">{doc.title}</span>
                <span className="mono max-w-[45%] shrink-0 truncate text-caption text-fg-tertiary">{doc.path}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}
        {bodyHits.length > 0 && (
          <Command.Group heading="In note text" className="px-1.5 [&_[cmdk-group-heading]]:micro-label [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1 [&_[cmdk-group-heading]]:text-fg-tertiary">
            {bodyHits.map(hit => (
              <Command.Item
                key={`body:${hit.scope}:${hit.path}`}
                value={`body:${hit.scope}:${hit.path}`}
                onSelect={() => go(hit.scope, hit.path)}
                className={PALETTE_ITEM_CLASS}
              >
                <TextSearch className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{hit.title}</span>
                  {hit.snippet && <span className="block truncate text-caption text-fg-tertiary">{hit.snippet}</span>}
                </span>
              </Command.Item>
            ))}
          </Command.Group>
        )}
      </Command.List>
    </Command>
  )
}
