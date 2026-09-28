import type { DocLinkKind, DocLinkResolution } from '@/types/docs'
import { currentOrgSlug, projectPath } from '@/lib/activeOrg'

/**
 * `[[kind:target|label]]` links in docs notes (F22, F24), mirrored from the
 * backend's `services/docs_links.py`:
 *
 *   by name  [[event:NAME]]  [[event-type:NAME]]  [[field:NAME]]  [[field:EVENT_TYPE/NAME]]
 *            [[variable:NAME]]  [[metric:NAME]]  [[branch:NAME]]  [[scan:NAME]]  [[data-source:NAME]]
 *   by id    [[doc:<id>]]  [[doc:<id>#heading-slug]]  [[alert-rule:<id>]]  [[user:<id>]]
 *
 * with an optional `|label`. A by-name link breaks when its target is renamed
 * (the server then suggests close current names); a by-id link survives
 * renames and moves and renders with the target's current name. `[[user:<id>]]`
 * is an @-mention and renders as `@Name`. A hand-typed `[[doc:path/to/note.md]]`
 * is accepted too: saving turns it into the id form when the path resolves.
 * Links inside fenced or inline code are ignored.
 *
 * This module is pure (no React, no react-markdown) so the entity pages' Notes
 * card can use it without pulling the Markdown renderer into their chunks.
 */

export const DOC_LINK_PATTERN =
  /\[\[(event-type|event|field|doc|variable|metric|alert-rule|branch|scan|data-source|user):([^\]|\n]{1,500})(?:\|([^\]\n]{1,200}))?\]\]/g

/** The spelling used inside `[[…]]`. */
export type DocLinkSyntaxKind =
  | 'event'
  | 'event-type'
  | 'field'
  | 'doc'
  | 'variable'
  | 'metric'
  | 'alert-rule'
  | 'branch'
  | 'scan'
  | 'data-source'
  | 'user'

const SYNTAX_BY_KIND: Record<DocLinkKind, DocLinkSyntaxKind> = {
  event: 'event',
  event_type: 'event-type',
  field: 'field',
  doc: 'doc',
  variable: 'variable',
  metric: 'metric',
  alert_rule: 'alert-rule',
  branch: 'branch',
  scan: 'scan',
  data_source: 'data-source',
  user: 'user',
}

const KIND_BY_SYNTAX = Object.fromEntries(
  Object.entries(SYNTAX_BY_KIND).map(([kind, syntax]) => [syntax, kind]),
) as Record<DocLinkSyntaxKind, DocLinkKind>

/** How a kind is named in prose ("no metric named …", "Notes that link to this …"). */
export const DOC_LINK_KIND_NOUN: Record<DocLinkKind, string> = {
  event: 'event',
  event_type: 'event type',
  field: 'field',
  doc: 'note',
  variable: 'variable',
  metric: 'metric',
  alert_rule: 'alert rule',
  branch: 'branch',
  scan: 'scan',
  data_source: 'data source',
  user: 'person',
}

/** Kinds written by id: they survive renames and render the current name. */
const ID_KINDS: ReadonlySet<DocLinkKind> = new Set<DocLinkKind>(['doc', 'alert_rule', 'user'])

const HEX32_RE = /^[0-9a-f]{32}$/i

export const LINK_HREF_PREFIX = 'tripl:'

/** The server resolves at most this many refs per request. */
export const MAX_LINK_REFS = 200

export interface ParsedDocLink {
  kind: DocLinkKind
  syntaxKind: DocLinkSyntaxKind
  /** The entity name or id (for a qualified field, the field name alone; for a note, no anchor). */
  target: string
  /** The event-type name of a `field:TYPE/NAME` link. */
  qualifier: string | null
  /** The heading slug of a `doc:<id>#slug` link, without the `#`. */
  anchor: string | null
  label: string | null
  /** The link exactly as written, `[[…]]` included. */
  raw: string
  /** `kind:target` as the `/docs/links?ref=` endpoint takes it. */
  ref: string
}

export function isDocLinkSyntaxKind(value: string): value is DocLinkSyntaxKind {
  return Object.hasOwn(KIND_BY_SYNTAX, value)
}

export function apiKind(kind: DocLinkSyntaxKind): DocLinkKind {
  return KIND_BY_SYNTAX[kind]
}

export function syntaxKind(kind: DocLinkKind): DocLinkSyntaxKind {
  return SYNTAX_BY_KIND[kind]
}

/** True for the kinds a note writes by id (a note, an alert rule, a person). */
export function isIdKind(kind: DocLinkKind): boolean {
  return ID_KINDS.has(kind)
}

/**
 * The canonical text of a UUID (lower case, hyphenated), or null. Accepts
 * what the backend's `uuid.UUID()` accepts (`docs_links.canonical_id`):
 * hyphens anywhere or none, surrounding braces and a `urn:uuid:` prefix, so a
 * link spelled either way keys to the same resolution the server returns.
 */
export function canonicalUuid(value: string): string | null {
  const hex = value
    .trim()
    .replaceAll('urn:', '')
    .replaceAll('uuid:', '')
    .replace(/^[{}]+|[{}]+$/g, '')
    .replaceAll('-', '')
  if (!HEX32_RE.test(hex)) return null
  const low = hex.toLowerCase()
  return `${low.slice(0, 8)}-${low.slice(8, 12)}-${low.slice(12, 16)}-${low.slice(16, 20)}-${low.slice(20)}`
}

/** True when a `doc:` target is a note id rather than a hand-typed path. */
export function isUuid(value: string): boolean {
  return canonicalUuid(value) !== null
}

/**
 * Split what was written after `kind:`: `TYPE/NAME` for fields, `id#anchor`
 * for notes; other kinds keep the whole text.
 */
export function splitTarget(
  kind: DocLinkSyntaxKind,
  written: string,
): { target: string; qualifier: string | null; anchor: string | null } {
  const text = written.trim()
  if (kind === 'field') {
    const cut = text.indexOf('/')
    if (cut > 0 && cut < text.length - 1) {
      return { target: text.slice(cut + 1).trim(), qualifier: text.slice(0, cut).trim(), anchor: null }
    }
  }
  if (kind === 'doc') {
    const cut = text.indexOf('#')
    if (cut > 0) {
      const anchor = text.slice(cut + 1).trim()
      return { target: text.slice(0, cut).trim(), qualifier: null, anchor: anchor || null }
    }
  }
  return { target: text, qualifier: null, anchor: null }
}

/** Blank out fenced blocks and inline code spans, keeping every offset. */
export function maskCode(markdown: string): string {
  const blank = (text: string) => text.replace(/[^\n]/g, ' ')
  const lines = markdown.split('\n')
  let fence: { char: string; size: number } | null = null
  const masked = lines.map(line => {
    const open = /^ {0,3}(`{3,}|~{3,})/.exec(line)
    const run = open?.[1] ?? ''
    const after = open ? line.slice(open[0].length) : ''
    if (fence) {
      if (run && run[0] === fence.char && run.length >= fence.size && /^\s*$/.test(after)) fence = null
      return blank(line)
    }
    if (run && !(run[0] === '`' && after.includes('`'))) {
      fence = { char: run.charAt(0), size: run.length }
      return blank(line)
    }
    return line
  })
  return masked.join('\n').replace(/(`+)(?!`)[\s\S]*?[^`]\1(?!`)/g, blank)
}

function parseMatch(match: RegExpExecArray): ParsedDocLink {
  const kind = match[1] as DocLinkSyntaxKind
  const written = (match[2] ?? '').trim()
  const { target, qualifier, anchor } = splitTarget(kind, written)
  const label = match[3]?.trim() || null
  // A note's anchor does not change what the link resolves to.
  const ref = kind === 'doc' ? `doc:${target}` : `${kind}:${written}`
  return { kind: apiKind(kind), syntaxKind: kind, target, qualifier, anchor, label, raw: match[0], ref }
}

/** Every link outside code, in order and with its offset, duplicates kept. */
function scanDocLinks(markdown: string): { link: ParsedDocLink; start: number; end: number }[] {
  const masked = maskCode(markdown)
  const out: { link: ParsedDocLink; start: number; end: number }[] = []
  const re = new RegExp(DOC_LINK_PATTERN.source, 'g')
  let match: RegExpExecArray | null
  while ((match = re.exec(masked)) !== null) {
    // Re-read the original text: masking never touches a link outside code.
    const start = re.lastIndex - match[0].length
    const real = new RegExp(DOC_LINK_PATTERN.source, 'y')
    real.lastIndex = start
    const exact = real.exec(markdown)
    if (!exact) continue
    out.push({ link: parseMatch(exact), start, end: start + exact[0].length })
  }
  return out
}

/** Every link outside code, in order, duplicates (by resolution key) dropped. */
export function extractDocLinks(markdown: string): ParsedDocLink[] {
  const seen = new Set<string>()
  const out: ParsedDocLink[] = []
  for (const { link } of scanDocLinks(markdown)) {
    const key = resolutionKey(link.kind, link.target, link.qualifier)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(link)
  }
  return out
}

/**
 * One key for a link and for the resolution the server returns for it. Ids
 * compare in their canonical form (any spelling the server accepts); a note's
 * anchor and qualifier never count, so a resolution's `route_path` must not be
 * trusted for the anchor (see {@link docLinkRoute}).
 */
export function resolutionKey(kind: DocLinkKind, target: string, qualifier: string | null): string {
  let name = target.trim()
  let qual = (qualifier ?? '').trim().toLowerCase()
  if (kind === 'doc') {
    const cut = name.indexOf('#')
    if (cut > 0) name = name.slice(0, cut)
    qual = ''
  }
  if (isIdKind(kind)) name = canonicalUuid(name) ?? name
  return `${kind}\u0000${qual}\u0000${name}`
}

/**
 * Where a resolved link opens. Links to one note with different anchors share
 * one resolution (the key ignores the anchor), and that resolution's route may
 * carry another link's `#anchor`: drop it and add this link's own.
 */
export function docLinkRoute(routePath: string, anchor: string | null): string {
  const cut = routePath.indexOf('#')
  const base = cut >= 0 ? routePath.slice(0, cut) : routePath
  return anchor ? `${base}#${encodeURIComponent(anchor)}` : base
}

export function indexResolutions(
  resolutions: readonly DocLinkResolution[],
): Map<string, DocLinkResolution> {
  const map = new Map<string, DocLinkResolution>()
  for (const item of resolutions) map.set(resolutionKey(item.kind, item.target, item.qualifier), item)
  return map
}

/**
 * `[[field:checkout/amount|Amount]]` → the href the renderer turns into a link.
 * The explicit label rides along as `?l=` (the encoded target never holds a
 * raw `?`, and Markdown's URL normalisation keeps `?` and `=` as they are) so
 * a by-id link knows whether to show the target's current name.
 */
export function docLinkHref(kind: DocLinkSyntaxKind, written: string, label?: string | null): string {
  const base = `${LINK_HREF_PREFIX}${kind}/${encodeURIComponent(written.trim())}`
  return label ? `${base}?l=${encodeURIComponent(label)}` : base
}

export interface ParsedDocLinkHref {
  kind: DocLinkKind
  target: string
  qualifier: string | null
  anchor: string | null
  written: string
  /** The `|label` the author wrote, if any. */
  label: string | null
}

export function parseDocLinkHref(href: string | undefined): ParsedDocLinkHref | null {
  if (!href?.startsWith(LINK_HREF_PREFIX)) return null
  const rest = href.slice(LINK_HREF_PREFIX.length)
  const cut = rest.indexOf('/')
  if (cut < 0) return null
  const kind = rest.slice(0, cut)
  if (!isDocLinkSyntaxKind(kind)) return null
  const tail = rest.slice(cut + 1)
  const cutLabel = tail.indexOf('?l=')
  let written: string
  let label: string | null = null
  try {
    written = decodeURIComponent(cutLabel >= 0 ? tail.slice(0, cutLabel) : tail)
    if (cutLabel >= 0) label = decodeURIComponent(tail.slice(cutLabel + 3)) || null
  } catch {
    return null
  }
  const { target, qualifier, anchor } = splitTarget(kind, written)
  return { kind: apiKind(kind), target, qualifier, anchor, written, label }
}

/** The `[[…]]` for an entity, e.g. for "New note about this". */
export function docLinkSyntax(kind: DocLinkKind, name: string, qualifier?: string | null): string {
  const written = kind === 'field' && qualifier ? `${qualifier}/${name}` : name
  return `[[${syntaxKind(kind)}:${written}]]`
}

/** Where "New note about this" lands: the docs page opens its new-note dialog. */
export function newNoteHref(slug: string, kind: DocLinkKind, name: string, qualifier?: string | null): string {
  const sp = new URLSearchParams({ new: '1', link: docLinkSyntax(kind, name, qualifier) })
  return projectPath(currentOrgSlug(), slug, `/docs?${sp.toString()}`)
}

/** Where a by-name kind is looked up, for the broken-link sentence. */
const WHERE: Partial<Record<DocLinkKind, string>> = {
  event: 'on the main plan',
  event_type: 'on the main plan',
  field: 'on the main plan',
  variable: 'on the main plan',
  metric: 'in the metric catalog',
  branch: 'in this project',
  scan: 'in this project',
  data_source: 'used by this project',
}

/**
 * True for a `[[doc:<id>]]` link that does not resolve: the note was deleted
 * or the reader may not see it. The server answers both the same way
 * (`unavailable`, no reason), and both render the same generic "unavailable
 * note", so the link never tells a reader that a hidden note exists.
 */
export function isUnavailableNote(link: Pick<DocLinkResolution, 'status'>): boolean {
  return link.status === 'unavailable'
}

/**
 * True for a hand-typed `[[doc:path]]` whose path names a note this reader can
 * read: the server offers that note's id (and its title as `label`).
 */
function isPathFormWithNote(link: Pick<DocLinkResolution, 'kind' | 'reason' | 'suggestions'>): boolean {
  return link.kind === 'doc' && link.reason === 'path_form' && (link.suggestions ?? []).length > 0
}

/**
 * How a relink suggestion reads: a note's title for a typed note path (the
 * suggestion itself is the note's id), the suggested name otherwise.
 */
export function suggestionLabel(
  link: Pick<DocLinkResolution, 'kind' | 'reason' | 'suggestions' | 'label'>,
  suggestion: string,
): string {
  return isPathFormWithNote(link) && link.label ? link.label : suggestion
}

/** A mention's display name without the `@` the server may already add. */
export function mentionName(label: string | null | undefined): string | null {
  const name = label?.replace(/^@+/, '').trim()
  return name || null
}

/** The broken/ambiguous line shown in the warning banner (suggestions apart). */
export function describeUnresolved(link: DocLinkResolution): string {
  const noun = DOC_LINK_KIND_NOUN[link.kind]
  const name = link.qualifier ? `${link.qualifier}/${link.target}` : link.target
  if (link.status === 'ambiguous') {
    return `${link.raw}: ${link.candidates} ${noun}s are named '${name}'; the link opens the first.`
  }
  if (isUnavailableNote(link)) return 'A linked note is unavailable: it was deleted, or it is not shared with you.'
  switch (link.kind) {
    case 'doc':
      if (isPathFormWithNote(link)) {
        const title = link.label ? `'${link.label}'` : 'the note at this path'
        return `${link.raw}: notes are linked by id. Save the note to link ${title} by its id.`
      }
      return `${link.raw}: no note at '${link.target}' that you can read.`
    case 'alert_rule':
      return `${link.raw}: this alert rule no longer exists.`
    case 'user':
      return `${link.raw}: this person is not a member of the organization.`
    default:
      return `${link.raw}: no ${noun} named '${name}' ${WHERE[link.kind] ?? 'in this project'}.`
  }
}

/** The relink hint for a broken link: `Did you mean: a, b?`, or null. */
export function describeSuggestions(
  link: Pick<DocLinkResolution, 'kind' | 'reason' | 'suggestions' | 'label'>,
): string | null {
  const names = (link.suggestions ?? []).map(name => suggestionLabel(link, name))
  return names.length > 0 ? `Did you mean: ${names.join(', ')}?` : null
}

/**
 * What a relink writes after `kind:` for a suggested name. A field keeps the
 * event type it was qualified with unless the suggestion names one itself.
 */
export function relinkWritten(link: Pick<DocLinkResolution, 'kind' | 'qualifier'>, suggestion: string): string {
  if (link.kind === 'field' && link.qualifier && !suggestion.includes('/')) return `${link.qualifier}/${suggestion}`
  return suggestion
}

/**
 * Re-point every occurrence (outside code) of a broken link at `written`,
 * keeping each occurrence's own `|label` and, for a note link, its own
 * `#anchor`. Used by the editor's relink buttons.
 */
export function relinkDocLinks(
  markdown: string,
  link: Pick<DocLinkResolution, 'kind' | 'target' | 'qualifier'>,
  written: string,
): string {
  const key = resolutionKey(link.kind, link.target, link.qualifier)
  const hits = scanDocLinks(markdown).filter(hit => resolutionKey(hit.link.kind, hit.link.target, hit.link.qualifier) === key)
  let out = markdown
  for (const hit of hits.reverse()) {
    const label = hit.link.label ? `|${hit.link.label}` : ''
    const anchor = hit.link.kind === 'doc' && hit.link.anchor && !written.includes('#') ? `#${hit.link.anchor}` : ''
    out = `${out.slice(0, hit.start)}[[${hit.link.syntaxKind}:${written}${anchor}${label}]]${out.slice(hit.end)}`
  }
  return out
}

// ---------------------------------------------------------------------------
// remark plugin
// ---------------------------------------------------------------------------

/** The slice of mdast this plugin touches. */
interface MdNode {
  type: string
  value?: string
  url?: string
  title?: string | null
  children?: MdNode[]
}

/** Nodes whose text is not prose: never rewrite inside them. */
const SKIP = new Set(['code', 'inlineCode', 'link', 'linkReference', 'definition', 'html'])

function splitText(value: string): MdNode[] | null {
  const re = new RegExp(DOC_LINK_PATTERN.source, 'g')
  const out: MdNode[] = []
  let last = 0
  let match: RegExpExecArray | null
  while ((match = re.exec(value)) !== null) {
    if (match.index > last) out.push({ type: 'text', value: value.slice(last, match.index) })
    const kind = match[1] as DocLinkSyntaxKind
    const written = (match[2] ?? '').trim()
    const label = match[3]?.trim() || null
    // A by-id link's text is replaced with the target's current name when it
    // renders; the written id is only the placeholder.
    out.push({
      type: 'link',
      url: docLinkHref(kind, written, label),
      title: null,
      children: [{ type: 'text', value: label ?? written }],
    })
    last = match.index + match[0].length
  }
  if (out.length === 0) return null
  if (last < value.length) out.push({ type: 'text', value: value.slice(last) })
  return out
}

function transform(node: MdNode): void {
  if (!node.children || SKIP.has(node.type)) return
  const next: MdNode[] = []
  let changed = false
  for (const child of node.children) {
    if (child.type === 'text' && typeof child.value === 'string') {
      const parts = splitText(child.value)
      if (parts) {
        next.push(...parts)
        changed = true
        continue
      }
    }
    transform(child)
    next.push(child)
  }
  if (changed) node.children = next
}

/**
 * Turns `[[kind:target|label]]` in prose into link nodes with a `tripl:` href,
 * which the renderer's `a` component maps to an in-app link or a broken chip.
 * Pass {@link docUrlTransform} too: react-markdown drops unknown protocols.
 */
export function remarkDocLinks() {
  return (tree: MdNode) => {
    transform(tree)
  }
}

// ---------------------------------------------------------------------------
// relative links between notes
// ---------------------------------------------------------------------------

/**
 * A relative `.md` link in a note (`../guides/setup.md#install`) resolved
 * against the note's own path. `null` for anything else — an absolute URL, a
 * site path, a bare anchor, a path that climbs out of the catalog root.
 */
export function resolveRelativeDocHref(
  currentPath: string,
  href: string | undefined,
): { path: string; hash: string } | null {
  if (!href || href.startsWith('#') || href.startsWith('/') || /^[a-z][a-z0-9+.-]*:/i.test(href)) return null
  const hashAt = href.indexOf('#')
  const hash = hashAt >= 0 ? href.slice(hashAt) : ''
  let target = hashAt >= 0 ? href.slice(0, hashAt) : href
  const queryAt = target.indexOf('?')
  if (queryAt >= 0) target = target.slice(0, queryAt)
  if (!/\.md$/i.test(target)) return null
  const base = currentPath.split('/').slice(0, -1)
  const parts = [...base]
  for (const raw of target.split('/')) {
    let segment: string
    try {
      segment = decodeURIComponent(raw)
    } catch {
      return null
    }
    if (segment === '' || segment === '.') continue
    if (segment === '..') {
      if (parts.length === 0) return null
      parts.pop()
      continue
    }
    parts.push(segment)
  }
  return parts.length > 0 ? { path: parts.join('/'), hash } : null
}
