import { useMemo, useState, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, Copy } from 'lucide-react'
import { toast } from 'sonner'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useCopyToClipboard } from '@/hooks/useCopyToClipboard'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import { resolveMetaFieldHref } from '@/lib/metaFields'
import { buildExamplePayload, buildSpecMarkdown, specIdentity, type SpecRow } from '@/lib/eventSpec'
import { nameFormatBaseColumns } from '@/pages/events/utils'
import type { Event, EventType, MetaFieldDefinition } from '@/types'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const CARD = 'overflow-hidden rounded-card border'
const CARD_STYLE = { background: 'var(--surface)', borderColor: 'var(--border)' } as const
const TH = 'h-auto px-[14px] py-2 text-left micro-label text-[var(--fg-subtle)]'
const TD = 'px-[14px] py-[9px] text-body-sm align-top'

/** The `${…}` names a field value uses. */
function templateTokens(value: string): Set<string> {
  return new Set(Array.from(value.matchAll(/\$\{([^}]+)\}/g), match => match[1] ?? ''))
}

/** Optional, unset and not part of the name: nothing to send, so folded away. */
function isUnsetOptional(row: SpecRow): boolean {
  return !row.value && !row.field.is_required && !row.namesTheEvent
}

/**
 * What a developer needs to instrument an event, on the page they are sent to.
 *
 * The monitoring detail page was built for watching a live event; a developer
 * handed a branch link found a Russian title, an identity reconstructible only
 * from the Fields table, template placeholders with no documented values in
 * sight, and half a page of "No metrics data available" for an event that is
 * not built yet. This card is the spec: identity with copy,
 * the fields with the marks that matter to an implementer, the documented
 * values inline, an example payload, and a Markdown copy for the ticket.
 */
export function EventSpecCard({
  slug,
  event,
  eventType,
  metaFieldMap,
}: {
  slug: string
  event: Event
  eventType: EventType | undefined
  metaFieldMap: Map<string, MetaFieldDefinition>
}) {
  // No fallback field to select here, so the outcome goes to a toast. The hook
  // also covers plain-HTTP instances, where navigator.clipboard is undefined.
  const { copy } = useCopyToClipboard()
  const copyText = async (text: string, what: string) => {
    if (await copy(text)) toast.success(`${what} copied`)
    else toast.error(`Could not copy ${what.toLowerCase()}`)
  }
  const branchId = useActiveBranchId()
  const branchLink = useBranchLinkProps()
  const identity = specIdentity(event)
  const rule = eventType?.event_name_format ?? null
  const rows = useMemo<SpecRow[]>(() => {
    const naming = nameFormatBaseColumns(rule)
    const valueByField = new Map(event.field_values.map(fv => [fv.field_definition_id, fv]))
    return [...(eventType?.field_definitions ?? [])]
      .sort((a, b) => a.order - b.order)
      .map(field => {
        const fv = valueByField.get(field.id)
        const value = fv?.value ?? ''
        // Only the variables this value names: a field reading `home` listed
        // `${user_id} = u_001, u_002…` from a context it does not use (EV-32).
        const tokens = templateTokens(value)
        return {
          field,
          value,
          namesTheEvent: naming.has(field.name),
          contexts: (fv?.variable_values ?? []).filter(
            context =>
              tokens.has(context.variable_name)
              || (!!context.source_column && tokens.has(context.source_column)),
          ),
        }
      })
  }, [event.field_values, eventType?.field_definitions, rule])
  // Optional fields with no value (often auto-created by accepted drift) fold
  // into one line, and the copies leave them out too: the spec to copy was
  // mostly empty rows (EV-32).
  const specRows = useMemo(() => rows.filter(row => !isUnsetOptional(row)), [rows])
  const unsetRows = useMemo(() => rows.filter(isUnsetOptional), [rows])
  const [showUnset, setShowUnset] = useState(false)
  const shownRows = showUnset ? rows : specRows
  const payload = useMemo(() => buildExamplePayload(specRows), [specRows])
  const payloadJson = JSON.stringify(payload, null, 2)
  const markdown = () =>
    buildSpecMarkdown({
      identity,
      title: event.title,
      eventTypeName: eventType?.display_name,
      description: event.description,
      rule,
      rows: specRows,
      payload,
    })

  return (
    <section className={CARD} style={CARD_STYLE} aria-label="Spec" data-testid="event-spec-card">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-3 border-border-subtle">
        <span className="text-body-sm font-semibold">Spec</span>
        <span className="text-caption text-fg-tertiary">
          what to send, and where it must land
        </span>
        <div className="flex-1" />
        <Button variant="outline" size="sm" onClick={() => copyText(payloadJson, 'JSON')}>
          <Copy className="mr-1.5 h-3.5 w-3.5" />
          Copy as JSON
        </Button>
        <Button variant="outline" size="sm" onClick={() => copyText(markdown(), 'Markdown')}>
          <Copy className="mr-1.5 h-3.5 w-3.5" />
          Copy as Markdown
        </Button>
      </div>

      <div className="space-y-2 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <code className="mono text-body font-medium" data-testid="spec-identity">{identity}</code>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-1.5"
            aria-label="Copy identity"
            onClick={() => copyText(identity, 'Identity')}
          >
            <Copy className="h-3.5 w-3.5" />
          </Button>
          {eventType && (
            <span className="text-caption text-fg-secondary">{eventType.display_name}</span>
          )}
        </div>
        {event.title && <p className="text-body">{event.title}</p>}
        {rule && (
          <p className="text-caption text-fg-tertiary">
            Named by scan rule <span className="mono">{rule}</span>. The scan matches this event on the
            identity above, so the row must carry exactly these values.
          </p>
        )}
        {event.description && (
          <p className="whitespace-pre-wrap text-body-sm text-fg-secondary">{event.description}</p>
        )}
        {(event.tags.length > 0 || event.meta_values.length > 0) && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-fg-secondary">
            {event.tags.map(tag => (
              <span key={tag.id} className="mono">#{tag.name}</span>
            ))}
            {event.meta_values.map(mv => {
              const def = metaFieldMap.get(mv.meta_field_definition_id)
              if (!def || !mv.value) return null
              const href = resolveMetaFieldHref(def, mv.value)
              return (
                <span key={mv.id}>
                  {def.display_name}:{' '}
                  {href ? (
                    <a href={href} target="_blank" rel="noopener noreferrer" className="underline text-accent">
                      {mv.value}
                    </a>
                  ) : (
                    <span className="mono">{mv.value}</span>
                  )}
                </span>
              )
            })}
          </div>
        )}
      </div>

      {shownRows.length > 0 && (
        // The design-system table scrolls itself, with the edge fade that says
        // there is more to the right on a phone (LIVE-5); the card is
        // `--surface`, so the fade's cover is set to match. A phone drops the
        // field type, the column a reader of the spec needs least.
        <div
          className="border-t"
          style={{ borderColor: 'var(--border-subtle)', '--scroll-x-bg': 'var(--surface)' } as CSSProperties}
        >
          <Table aria-label="Spec fields">
            <TableHeader>
              <TableRow className="hover:bg-transparent" style={{ background: 'var(--bg-sunken)' }}>
                <TableHead scope="col" className={TH}>Field</TableHead>
                <TableHead scope="col" className={`${TH} hidden md:table-cell`}>Type</TableHead>
                <TableHead scope="col" className={TH}>Value</TableHead>
                <TableHead scope="col" className={TH}>Documented values</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shownRows.map(row => (
                <TableRow
                  key={row.field.id}
                  className="hover:bg-transparent"
                  style={{ borderColor: 'var(--border-subtle)' }}
                >
                  <TableCell className={TD}>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="mono text-body-sm">{row.field.name}</span>
                      {(row.field.is_required || row.namesTheEvent) && (
                        <span className="text-micro text-danger">required</span>
                      )}
                      {/* A kind tag, so the outline pill (DS-6). */}
                      {row.namesTheEvent && (
                        <Chip size="xs" variant="outline">
                          names the event
                        </Chip>
                      )}
                    </div>
                    {row.field.description && (
                      <div className="mt-[2px] text-caption text-fg-tertiary">{row.field.description}</div>
                    )}
                  </TableCell>
                  <TableCell className={`${TD} mono hidden text-caption md:table-cell`}>
                    {row.field.field_type}
                  </TableCell>
                  <TableCell
                    className={`${TD} mono break-all text-caption`}
                    style={{ color: 'var(--fg-muted)' }}
                  >
                    {row.value || '—'}
                  </TableCell>
                  <TableCell className={`${TD} text-caption`}>
                    {row.contexts.length === 0 ? (
                      <span className="text-fg-tertiary">—</span>
                    ) : (
                      <ul className="space-y-1">
                        {row.contexts.map(context => {
                          const link = branchLink(projectPath(currentOrgSlug(), slug, `/variables/${context.variable_id}`), branchId)
                          return (
                            <li key={context.id}>
                              <Link to={link.to} onClick={link.onClick} className="mono underline underline-offset-2">
                                ${'{'}{context.variable_name}{'}'}
                              </Link>
                              {context.values.length > 0 ? (
                                <span className="mono text-fg-secondary">
                                  {' '}= {context.values.slice(0, 10).join(', ')}
                                  {context.values.length > 10 ? ` … +${context.values.length - 10}` : ''}
                                </span>
                              ) : (
                                <span className="text-fg-tertiary"> — no documented values yet</span>
                              )}
                            </li>
                          )
                        })}
                      </ul>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {unsetRows.length > 0 && (
        <div className="border-t px-4 py-2 border-border-subtle">
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={showUnset}
            onClick={() => setShowUnset(open => !open)}
          >
            <ChevronDown className={`transition-transform ${showUnset ? 'rotate-180' : ''}`} aria-hidden="true" />
            {showUnset
              ? 'Hide optional fields that are not set'
              : `+${unsetRows.length} optional field${unsetRows.length === 1 ? '' : 's'} not set`}
          </Button>
        </div>
      )}

      <div className="border-t px-4 py-3 border-border-subtle">
        <div className="mb-1 micro-label text-fg-tertiary">
          Example payload
        </div>
        <pre className="mono overflow-x-auto rounded-md p-3 text-caption bg-bg-sunken" data-testid="spec-payload">
          {payloadJson}
        </pre>
      </div>
    </section>
  )
}
