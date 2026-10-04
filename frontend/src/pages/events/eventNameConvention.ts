/**
 * The naming convention an event type's existing events follow, for the Name
 * placeholder and a non-blocking warning on the single-event form.
 *
 * "e.g. checkout:completed" sat next to a catalog whose Screen View events are
 * called "Home Screen View"; nothing told the author which convention this
 * type uses. An example taken from the type's own events says so, and a name
 * written in another style is pointed out, never refused: the name has to be
 * whatever the app sends, and only the author knows that.
 */

/** How a name is put together. `word` is one lowercase word, which fits several styles. */
export type NameStyle =
  | 'word'
  | 'snake'
  | 'kebab'
  | 'colon'
  | 'dot'
  | 'camel'
  | 'pascal'
  | 'spaced'
  | 'path'

/** The styles a single lowercase word does not contradict. */
const WORD_FITS: ReadonlySet<NameStyle> = new Set(['snake', 'kebab', 'camel'])

/** How many of the type's events are sampled. */
export const NAME_SAMPLE_SIZE = 20
/** A convention is claimed only from at least this many names … */
const MIN_SAMPLE = 3
/** … when at least this share of them agree. */
const MIN_AGREEMENT = 0.8

/** The style of one name, or null when it mixes styles. */
export function nameStyle(name: string): NameStyle | null {
  const trimmed = name.trim()
  if (!trimmed) return null
  if (trimmed.startsWith('/')) return 'path'
  if (/\s/.test(trimmed)) return 'spaced'
  if (trimmed.includes(':')) return 'colon'
  if (/^[a-z0-9]+$/.test(trimmed)) return 'word'
  if (/^[a-z0-9]+(_[a-z0-9]+)+$/.test(trimmed)) return 'snake'
  if (/^[a-z0-9]+(-[a-z0-9]+)+$/.test(trimmed)) return 'kebab'
  if (/^[a-z0-9]+(\.[a-z0-9_]+)+$/.test(trimmed)) return 'dot'
  if (/^[a-z][a-zA-Z0-9]*$/.test(trimmed)) return 'camel'
  if (/^[A-Z][a-zA-Z0-9]*$/.test(trimmed)) return 'pascal'
  return null
}

export interface NameConvention {
  style: NameStyle
  /** A real name of this type in that style, for the placeholder and the warning. */
  example: string
}

/**
 * The style most of these names share, with one of them as the example; null
 * when there are too few names or they do not agree.
 */
export function inferNameConvention(names: readonly string[]): NameConvention | null {
  const styled = names
    .map(name => ({ name: name.trim(), style: nameStyle(name) }))
    .filter((item): item is { name: string; style: NameStyle } => item.style !== null)
  if (styled.length < MIN_SAMPLE) return null
  const counts = new Map<NameStyle, number>()
  for (const item of styled) counts.set(item.style, (counts.get(item.style) ?? 0) + 1)
  // A lone word agrees with snake, kebab and camel alike, so it counts towards
  // whichever of those the other names use.
  const words = counts.get('word') ?? 0
  let best: { style: NameStyle; count: number } | null = null
  for (const [style, count] of counts) {
    const total = WORD_FITS.has(style) ? count + words : count
    if (!best || total > best.count) best = { style, count: total }
  }
  if (!best || best.count / styled.length < MIN_AGREEMENT) return null
  const bestStyle = best.style
  // A multi-part name shows the convention better than a lone word does.
  const example =
    styled.find(item => item.style === bestStyle && bestStyle !== 'word')?.name
    ?? styled.find(item => item.style === bestStyle)?.name
  return example ? { style: bestStyle, example } : null
}

/** Whether a typed name is written in another style than the convention. */
export function breaksNameConvention(name: string, convention: NameConvention | null): boolean {
  if (!convention || name.trim() === '') return false
  const style = nameStyle(name)
  if (style === convention.style) return false
  // One lowercase word is a snake, kebab or camel name with a single part.
  if (style === 'word') return !WORD_FITS.has(convention.style)
  if (convention.style === 'word') return style === null || !WORD_FITS.has(style)
  // One capitalised word is a spaced Title Case name with a single part.
  if (style === 'pascal' && convention.style === 'spaced') return !/^[A-Z][a-z0-9]*$/.test(name.trim())
  return true
}
