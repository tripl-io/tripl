import type { DocLinkKind, DocLinkResolution } from '@/types/docs'

/**
 * `[[kind:name|label]]` links in docs notes (F22), mirrored from the backend's
 * `services/docs_links.py`:
 *
 *   [[event:NAME]]  [[event-type:NAME]]  [[field:NAME]]  [[field:EVENT_TYPE/NAME]]
 *
 * with an optional `|label`. Links inside fenced or inline code are ignored.
 * This module is pure (no React, no react-markdown) so the event page's Notes
 * card can use it without pulling the Markdown renderer into its chunk.
 */

export const DOC_LINK_PATTERN = /\[\[(event|event-type|field):([^\]|\n]{1,500})(?:\|([^\]\n]{1,200}))?\]\]/g

/** The spelling used inside `[[…]]`. */
export type DocLinkSyntaxKind = 'event' | 'event-type' | 'field'

export const LINK_HREF_PREFIX = 'tripl:'

/** The server resolves at most this many refs per request. */
export const MAX_LINK_REFS = 200

export interface ParsedDocLink {
  kind: DocLinkKind
  syntaxKind: DocLinkSyntaxKind
  /** The entity name (for a qualified field, the field name alone). */
  target: string
  /** The event-type name of a `field:TYPE/NAME` link. */
  qualifier: string | null
  label: string | null
  /** The link exactly as written, `[[…]]` included. */
  raw: string
  /** `kind:target` as the `/docs/links?ref=` endpoint takes it. */
  ref: string
}

export function apiKind(kind: DocLinkSyntaxKind): DocLinkKind {
  return kind === 'event-type' ? 'event_type' : kind
}

export function syntaxKind(kind: DocLinkKind): DocLinkSyntaxKind {
  return kind === 'event_type' ? 'event-type' : kind
}

/** Split `TYPE/NAME` for fields; other kinds keep the whole name. */
export function splitTarget(kind: DocLinkSyntaxKind, written: string): { target: string; qualifier: string | null } {
  const text = written.trim()
  if (kind === 'field') {
    const cut = text.indexOf('/')
    if (cut > 0 && cut < text.length - 1) {
      return { target: text.slice(cut + 1).trim(), qualifier: text.slice(0, cut).trim() }
    }
  }
  return { target: text, qualifier: null }
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
  const { target, qualifier } = splitTarget(kind, written)
  const label = match[3]?.trim() || null
  return { kind: apiKind(kind), syntaxKind: kind, target, qualifier, label, raw: match[0], ref: `${kind}:${written}` }
}

/** Every link outside code, in order, duplicates (by ref) dropped. */
export function extractDocLinks(markdown: string): ParsedDocLink[] {
  const masked = maskCode(markdown)
  const seen = new Set<string>()
  const out: ParsedDocLink[] = []
  const re = new RegExp(DOC_LINK_PATTERN.source, 'g')
  let match: RegExpExecArray | null
  while ((match = re.exec(masked)) !== null) {
    // Re-read the original text: masking never touches a link outside code.
    const original = re.lastIndex - match[0].length
    const real = new RegExp(DOC_LINK_PATTERN.source, 'y')
    real.lastIndex = original
    const exact = real.exec(markdown)
    if (!exact) continue
    const link = parseMatch(exact)
    const key = resolutionKey(link.kind, link.target, link.qualifier)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(link)
  }
  return out
}

/** One key for a link and for the resolution the server returns for it. */
export function resolutionKey(kind: DocLinkKind, target: string, qualifier: string | null): string {
  return `${kind}\u0000${(qualifier ?? '').trim().toLowerCase()}\u0000${target.trim()}`
}

export function indexResolutions(
  resolutions: readonly DocLinkResolution[],
): Map<string, DocLinkResolution> {
  const map = new Map<string, DocLinkResolution>()
  for (const item of resolutions) map.set(resolutionKey(item.kind, item.target, item.qualifier), item)
  return map
}

/** `[[field:checkout/amount]]` → the href the renderer turns into a link. */
export function docLinkHref(kind: DocLinkSyntaxKind, written: string): string {
  return `${LINK_HREF_PREFIX}${kind}/${encodeURIComponent(written.trim())}`
}

export function parseDocLinkHref(
  href: string | undefined,
): { kind: DocLinkKind; target: string; qualifier: string | null; written: string } | null {
  if (!href?.startsWith(LINK_HREF_PREFIX)) return null
  const rest = href.slice(LINK_HREF_PREFIX.length)
  const cut = rest.indexOf('/')
  if (cut < 0) return null
  const kind = rest.slice(0, cut)
  if (kind !== 'event' && kind !== 'event-type' && kind !== 'field') return null
  let written: string
  try {
    written = decodeURIComponent(rest.slice(cut + 1))
  } catch {
    return null
  }
  const { target, qualifier } = splitTarget(kind, written)
  return { kind: apiKind(kind), target, qualifier, written }
}

/** The `[[…]]` for an entity, e.g. for "New note about this". */
export function docLinkSyntax(kind: DocLinkKind, name: string, qualifier?: string | null): string {
  const written = kind === 'field' && qualifier ? `${qualifier}/${name}` : name
  return `[[${syntaxKind(kind)}:${written}]]`
}

/** Where "New note about this" lands: the docs page opens its new-note dialog. */
export function newNoteHref(slug: string, kind: DocLinkKind, name: string, qualifier?: string | null): string {
  const sp = new URLSearchParams({ new: '1', link: docLinkSyntax(kind, name, qualifier) })
  return `/p/${slug}/docs?${sp.toString()}`
}

/** The broken/ambiguous line shown in the warning banner. */
export function describeUnresolved(link: DocLinkResolution): string {
  const noun = link.kind === 'event_type' ? 'event type' : link.kind
  const name = link.qualifier ? `${link.qualifier}/${link.target}` : link.target
  if (link.status === 'ambiguous') {
    return `${link.raw}: ${link.candidates} ${noun}s are named '${name}'; the link opens the first.`
  }
  return `${link.raw}: no ${noun} named '${name}' on the main plan.`
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
    const label = match[3]?.trim() || written
    out.push({ type: 'link', url: docLinkHref(kind, written), title: null, children: [{ type: 'text', value: label }] })
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
 * Turns `[[kind:name|label]]` in prose into link nodes with a `tripl:` href,
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
