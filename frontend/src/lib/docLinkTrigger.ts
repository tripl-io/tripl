import type { DocLinkKind } from '@/types/docs'
import { apiKind, isDocLinkSyntaxKind, maskCode } from '@/lib/docLinks'

/**
 * Where the note editor's link picker opens (F24, GH #308), worked out from
 * the text before the cursor. Pure, so it is tested without CodeMirror.
 *
 *   `[[`            every kind          `[[sign`        every kind, q = "sign"
 *   `[[metric:`     metrics only        `[[metric:sig`  metrics, q = "sig"
 *   `@` after a space or at a line start: people (`@ad` → q = "ad")
 *
 * An `@` inside a word (an email address) does not open it, and neither does
 * anything inside a code span or fenced block.
 */
export interface LinkTrigger {
  mode: 'link' | 'mention'
  /** Document offset of the `[[` or the `@`: the start of what a pick replaces. */
  from: number
  /** Document offset of the cursor. */
  to: number
  /** The kind the typed prefix narrows to, or null for every kind. */
  kind: DocLinkKind | null
  query: string
}

/** Longest query the picker follows; past it the `[[` is treated as prose. */
export const MAX_TRIGGER_QUERY = 80

const LINK_OPEN = new RegExp(`\\[\\[([^\\[\\]\\n|]{0,${MAX_TRIGGER_QUERY}})$`)
const MENTION_OPEN = /(^|[\s([{>])@([\p{L}\p{N}._-]{0,40})$/u

/**
 * The trigger for a cursor at the end of `lineBefore` (the current line up to
 * the cursor), whose line starts at document offset `lineStart`. Returns null
 * when the cursor is not inside an open `[[…` or `@…`.
 */
export function findLinkTrigger(lineBefore: string, lineStart: number): LinkTrigger | null {
  const to = lineStart + lineBefore.length
  const link = LINK_OPEN.exec(lineBefore)
  if (link) {
    const inner = link[1] ?? ''
    const colon = inner.indexOf(':')
    const prefix = colon >= 0 ? inner.slice(0, colon).trim().toLowerCase() : ''
    if (colon >= 0 && isDocLinkSyntaxKind(prefix)) {
      return { mode: 'link', from: lineStart + link.index, to, kind: apiKind(prefix), query: inner.slice(colon + 1).trimStart() }
    }
    return { mode: 'link', from: lineStart + link.index, to, kind: null, query: inner.trimStart() }
  }
  const mention = MENTION_OPEN.exec(lineBefore)
  if (mention) {
    const at = lineStart + mention.index + (mention[1] ?? '').length
    return { mode: 'mention', from: at, to, kind: 'user', query: mention[2] ?? '' }
  }
  return null
}

/**
 * True when the trigger starts inside a code span or fenced block of
 * `before` (the document up to the cursor), where `[[` and `@` are literal.
 */
export function triggerInCode(before: string, trigger: Pick<LinkTrigger, 'from'>): boolean {
  if (trigger.from >= before.length) return false
  return maskCode(before)[trigger.from] !== before[trigger.from]
}

/**
 * The text a pick writes over `[from, to)` and how far past the cursor it
 * reaches: the `]]` (or `]`) an auto-closed bracket left after the cursor is
 * swallowed so the reference is not followed by a stray `]]`.
 */
export function pickReplacement(mode: LinkTrigger['mode'], after: string): { extra: number } {
  if (mode !== 'link') return { extra: 0 }
  if (after.startsWith(']]')) return { extra: 2 }
  if (after.startsWith(']')) return { extra: 1 }
  return { extra: 0 }
}

/** Splice a picked reference into `text` (the editor fallback without a view). */
export function applyPick(text: string, trigger: LinkTrigger, insert: string): { text: string; cursor: number } {
  const { extra } = pickReplacement(trigger.mode, text.slice(trigger.to))
  const next = `${text.slice(0, trigger.from)}${insert}${text.slice(trigger.to + extra)}`
  return { text: next, cursor: trigger.from + insert.length }
}
