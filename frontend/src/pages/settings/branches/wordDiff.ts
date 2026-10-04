/**
 * Word-level diff for the before → after of a long string field.
 *
 * A description edit arrives as two whole paragraphs, and finding the one word
 * that changed meant comparing them by eye. This marks the words each side
 * does not share with the other — a plain LCS over whitespace-split tokens,
 * which is plenty for the prose these fields hold.
 */

export interface WordSegment {
  text: string
  /** Not in the other side: removed on the before side, added on the after. */
  changed: boolean
}

/** One run of the merged, single-paragraph form of the same diff:
 * shared text once, removed and added words in reading order. */
export interface InlineSegment {
  text: string
  kind: 'same' | 'removed' | 'added'
}

/** Below this, the two values are short enough to compare at a glance. */
export const WORD_DIFF_MIN_LENGTH = 40

/** The LCS table is tokens × tokens; past this the highlight is not worth it. */
const MAX_CELLS = 250_000

function tokenize(text: string): string[] {
  return text.split(/(\s+)/).filter((token) => token !== '')
}

function pushSegment(segments: WordSegment[], text: string, changed: boolean) {
  const last = segments[segments.length - 1]
  if (last && last.changed === changed) {
    segments[segments.length - 1] = { text: last.text + text, changed }
  } else {
    segments.push({ text, changed })
  }
}

/**
 * The two sides split into shared and changed runs, or null when a highlight
 * would not help: either value is short, the table would be too large, or the
 * two share no word at all (everything would be marked).
 */
export function wordDiff(
  before: string,
  after: string,
): { before: WordSegment[]; after: WordSegment[]; inline: InlineSegment[] } | null {
  if (Math.max(before.length, after.length) < WORD_DIFF_MIN_LENGTH) return null
  const a = tokenize(before)
  const b = tokenize(after)
  if (a.length * b.length > MAX_CELLS) return null

  // lcs(i, j): length of the longest common subsequence of a[i..] and b[j..],
  // kept in one flat row-major table. Row a.length and column b.length are the
  // empty-suffix boundary, which reads as 0.
  const width = b.length + 1
  const table = new Uint32Array((a.length + 1) * width)
  const lcs = (i: number, j: number): number => table[i * width + j] ?? 0
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        a[i] === b[j] ? lcs(i + 1, j + 1) + 1 : Math.max(lcs(i + 1, j), lcs(i, j + 1))
    }
  }
  // Whitespace always matches something; only shared WORDS make it readable.
  const sharedWords = (() => {
    let i = 0
    let j = 0
    let words = 0
    while (i < a.length && j < b.length) {
      const ai = a[i]
      const bj = b[j]
      if (ai === undefined || bj === undefined) break
      if (ai === bj) {
        if (ai.trim() !== '') words += 1
        i += 1
        j += 1
      } else if (lcs(i + 1, j) >= lcs(i, j + 1)) {
        i += 1
      } else {
        j += 1
      }
    }
    return words
  })()
  if (sharedWords === 0) return null

  const ops: InlineSegment[] = []
  const pushOp = (text: string, kind: InlineSegment['kind']) => {
    const last = ops[ops.length - 1]
    if (last && last.kind === kind) ops[ops.length - 1] = { text: last.text + text, kind }
    else ops.push({ text, kind })
  }
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    const ai = a[i]
    const bj = b[j]
    if (ai === undefined || bj === undefined) break
    if (ai === bj) {
      pushOp(ai, 'same')
      i += 1
      j += 1
    } else if (lcs(i + 1, j) >= lcs(i, j + 1)) {
      pushOp(ai, 'removed')
      i += 1
    } else {
      pushOp(bj, 'added')
      j += 1
    }
  }
  for (const token of a.slice(i)) pushOp(token, 'removed')
  for (const token of b.slice(j)) pushOp(token, 'added')

  const beforeSegments: WordSegment[] = []
  const afterSegments: WordSegment[] = []
  for (const op of ops) {
    if (op.kind !== 'added') pushSegment(beforeSegments, op.text, op.kind === 'removed')
    if (op.kind !== 'removed') pushSegment(afterSegments, op.text, op.kind === 'added')
  }
  return { before: beforeSegments, after: afterSegments, inline: ops }
}
