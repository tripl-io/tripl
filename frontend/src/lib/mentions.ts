/**
 * @mentions in comment bodies (#259).
 *
 * The composer stores a mention as `@[Name](user_id)`: the id is what the
 * server notifies, the name is what a reader sees, so a later rename does not
 * break the link and a plain "@ada" typed by hand is just text. The server
 * parses the same token; keep the two patterns in step.
 */

const UUID = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

/** One stored mention. The name may not hold `]` or a newline. */
export const MENTION_PATTERN = new RegExp(`@\\[([^\\]\\n]{1,200})\\]\\((${UUID})\\)`, 'g')

export type MentionSegment =
  | { kind: 'text'; text: string }
  | { kind: 'mention'; name: string; userId: string }

/** The name as it can be stored: no `]`, `[` or line break, never empty. */
export function sanitizeMentionName(name: string): string {
  const cleaned = name.replace(/[[\]\n\r]/g, ' ').replace(/\s+/g, ' ').trim()
  return cleaned || 'someone'
}

export function formatMention(name: string, userId: string): string {
  return `@[${sanitizeMentionName(name)}](${userId})`
}

/** The body split into plain text and mentions, in order. */
export function parseMentions(body: string): MentionSegment[] {
  const segments: MentionSegment[] = []
  let last = 0
  for (const match of body.matchAll(MENTION_PATTERN)) {
    const index = match.index ?? 0
    const name = match[1]
    const userId = match[2]
    if (name === undefined || userId === undefined) continue
    if (index > last) segments.push({ kind: 'text', text: body.slice(last, index) })
    segments.push({ kind: 'mention', name, userId: userId.toLowerCase() })
    last = index + match[0].length
  }
  if (last < body.length) segments.push({ kind: 'text', text: body.slice(last) })
  return segments
}

/** The distinct user ids a body mentions. */
export function mentionedUserIds(body: string): string[] {
  const ids = new Set<string>()
  for (const segment of parseMentions(body)) {
    if (segment.kind === 'mention') ids.add(segment.userId)
  }
  return [...ids]
}

/**
 * The mention being typed at the caret, if any: an `@` at the start of the text
 * or after whitespace, then up to 40 characters with no space, `@` or bracket.
 * `start` is the index of the `@`.
 */
export function activeMentionQuery(
  text: string,
  caret: number,
): { start: number; query: string } | null {
  const before = text.slice(0, caret)
  const match = /(^|\s)@([^\s@[\]()]{0,40})$/.exec(before)
  if (!match) return null
  const query = match[2] ?? ''
  return { start: caret - query.length - 1, query }
}

/** Replace the `@query` from `start` to `caret` with the token and a space. */
export function insertMention(
  text: string,
  start: number,
  caret: number,
  name: string,
  userId: string,
): { text: string; caret: number } {
  const token = `${formatMention(name, userId)} `
  const next = text.slice(0, start) + token + text.slice(caret)
  return { text: next, caret: start + token.length }
}

export interface MentionCandidate {
  userId: string
  name: string
  email: string
}

/** Members whose name or email contains the query, name matches first. */
export function filterMentionCandidates(
  candidates: readonly MentionCandidate[],
  query: string,
  limit = 8,
): MentionCandidate[] {
  const q = query.trim().toLowerCase()
  if (!q) return candidates.slice(0, limit)
  const byName: MentionCandidate[] = []
  const byEmail: MentionCandidate[] = []
  for (const candidate of candidates) {
    const name = candidate.name.toLowerCase()
    if (name.startsWith(q) || name.split(/\s+/).some(part => part.startsWith(q))) byName.push(candidate)
    else if (name.includes(q) || candidate.email.toLowerCase().includes(q)) byEmail.push(candidate)
  }
  return [...byName, ...byEmail].slice(0, limit)
}
